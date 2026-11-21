"""Instant Recovery state machine, outside the agent (ADR 0030 §5/§6).

The agent maps, starts, unmaps and discards; this module does what the backend
owns:

- **Move to storage**: one PVE `move_disk` task per disk, one after another,
  started and polled here (a PVE task survives any restart of the agent or the
  appliance). When the last disk is moved the plan goes to `finishing` and the
  agent unmaps.
- **Timeouts**: a plan the agent never claims, or a start that stops reporting,
  is marked failed so it doesn't sit in the UI forever.

`advance` runs on every tick of the Recovery Lab scheduler process, like the
Disk Mounts sweep.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from resurgam_api.config import Settings
from resurgam_api.connectivity.pve_api import PveWriteError, get_task_status, move_vm_disk
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.pve import PveCluster
from resurgam_api.models.restore import RestorePlan, RestorePlanExecutionStatus, RestorePlanMethod

logger = logging.getLogger("resurgam_api.instant_recovery")

S = RestorePlanExecutionStatus

QUEUE_TIMEOUT = timedelta(minutes=15)
START_TIMEOUT = timedelta(minutes=30)
# How long a failed finish/discard attempt waits before the agent retries.
RETRY_BACKOFF = timedelta(seconds=60)
# The agent refuses to start when less is free on its overlay storage: the
# guest's writes until the move finishes land there.
MIN_FREE_BYTES = 20 * 2**30
# An agent that ran a heartbeat-able plan and went quiet this long is shown
# as "not reporting" (the VM may still run fine).
HEARTBEAT_STALE_AFTER = timedelta(minutes=5)
OVERLAY_STORAGE_ALARM_PERCENT = 80.0

# The VM exists and reads from PBS through the agent's mapping.
RUNNING_FROM_BACKUP_STATUSES = frozenset({S.RUNNING_FROM_BACKUP.value, S.MOVING_DISKS.value})
# Something of the plan still exists on the node (mapping, overlay, VM).
ACTIVE_STATUSES = frozenset(
    {
        S.QUEUED.value,
        S.MAPPING.value,
        S.STARTING.value,
        S.RUNNING_FROM_BACKUP.value,
        S.MOVING_DISKS.value,
        S.FINISHING.value,
        S.DISCARDING.value,
    }
)
# Reads from PBS right now -- what maintenance mode / updates must warn about.
READS_FROM_PBS_STATUSES = frozenset(
    {S.MAPPING.value, S.STARTING.value, S.RUNNING_FROM_BACKUP.value, S.MOVING_DISKS.value}
)
TERMINAL_STATUSES = frozenset({S.COMPLETED.value, S.DISCARDED.value, S.FAILED.value})


def record_audit(
    db: Session,
    plan: RestorePlan,
    action: str,
    *,
    actor_user_id: uuid.UUID | None,
    correlation_id: str | None = None,
    detail: dict | None = None,
) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            target_type="restore_plan",
            target_id=str(plan.id),
            customer_id=plan.customer_id,
            correlation_id=correlation_id,
            detail=detail,
        )
    )


def fail(plan: RestorePlan, error: str, *, now: datetime) -> None:
    plan.execution_status = S.FAILED.value
    plan.execution_error = error[:4000]
    plan.execution_completed_at = now
    plan.ir_ended_at = now


def start_move(plan: RestorePlan, *, bwlimit_kib: int | None, user_id: uuid.UUID | None, now: datetime) -> None:
    """`running_from_backup` -> `moving_disks`. Disks already moved stay done; a
    disk whose move failed is tried again."""
    existing = {d["slot"]: d for d in plan.ir_move_disks or []}
    disks = []
    for entry in plan.ir_overlay_volumes or []:
        slot = str(entry["slot"])
        previous = existing.get(slot)
        if previous and previous.get("status") == "done":
            disks.append(previous)
        else:
            disks.append({"slot": slot, "status": "pending", "upid": None, "error": None})
    plan.ir_move_disks = disks
    plan.ir_move_bwlimit_kib = bwlimit_kib
    plan.ir_move_requested_at = now
    plan.ir_move_requested_by_user_id = user_id
    plan.ir_last_error = None
    plan.execution_status = S.MOVING_DISKS.value


def _advance_move(db: Session, settings: Settings, plan: RestorePlan, now: datetime) -> str | None:
    """One step of Move to storage. Returns an audit action when something
    changed, None when it is still waiting on PVE."""
    from resurgam_api.api.v1.pve_clusters import _cluster_trust_and_token

    cluster = db.get(PveCluster, plan.pve_cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    timeout = settings.connectivity_timeout_seconds
    disks = [dict(d) for d in plan.ir_move_disks or []]

    running = next((d for d in disks if d["status"] == "running"), None)
    if running is not None:
        task = get_task_status(
            cluster.host, cluster.port, trust, token, timeout, node=plan.target_node, upid=running["upid"]
        )
        if not task.reachable or task.status == "running":
            return None
        if task.status != "stopped":
            return None
        if task.exitstatus == "OK":
            running["status"] = "done"
            plan.ir_move_disks = disks
            action = "instant_recovery_disk_moved"
        else:
            running["status"] = "failed"
            running["error"] = task.exitstatus
            plan.ir_move_disks = disks
            plan.ir_last_error = f"Moving {running['slot']} failed: {task.exitstatus}"
            plan.execution_status = S.RUNNING_FROM_BACKUP.value
            return "instant_recovery_move_failed"
    else:
        action = None

    pending = next((d for d in disks if d["status"] == "pending"), None)
    if pending is not None:
        try:
            upid = move_vm_disk(
                cluster.host,
                cluster.port,
                trust,
                token,
                timeout,
                node=plan.target_node,
                vmid=plan.target_vmid,
                disk=pending["slot"],
                storage=plan.target_storage,
                delete_source=True,
                bwlimit_kib=plan.ir_move_bwlimit_kib,
            )
        except PveWriteError as exc:
            pending["status"] = "failed"
            pending["error"] = str(exc)[:1000]
            plan.ir_move_disks = disks
            plan.ir_last_error = f"PVE refused to move {pending['slot']}: {exc}"
            plan.execution_status = S.RUNNING_FROM_BACKUP.value
            return "instant_recovery_move_failed"
        pending["status"] = "running"
        pending["upid"] = upid
        plan.ir_move_disks = disks
        return action or "instant_recovery_disk_move_started"

    if all(d["status"] == "done" for d in disks):
        plan.execution_status = S.FINISHING.value
        plan.ir_next_attempt_at = None
        return "instant_recovery_disks_moved"
    return action


def advance(db: Session, settings: Settings, *, now: datetime | None = None) -> list[tuple[uuid.UUID, str]]:
    """One pass over every active Instant Recovery plan. Returns
    `(plan_id, action)` for each change, each audited (actor = system)."""
    now = now or datetime.now(UTC)
    changes: list[tuple[uuid.UUID, str]] = []

    plans = (
        db.query(RestorePlan)
        .filter(
            RestorePlan.method == RestorePlanMethod.INSTANT.value,
            RestorePlan.execution_status.in_([S.QUEUED.value, S.MAPPING.value, S.STARTING.value, S.MOVING_DISKS.value]),
        )
        .with_for_update(skip_locked=True)
        .all()
    )
    for plan in plans:
        action: str | None = None
        detail: dict = {"source": "instant_recovery_scheduler"}
        if plan.execution_status == S.QUEUED.value and plan.executed_at and plan.executed_at <= now - QUEUE_TIMEOUT:
            fail(
                plan,
                "The lab agent did not pick up this Instant Recovery within 15 minutes. Is resurgam-lab-agent "
                "running on the target node and paired with a PBS token?",
                now=now,
            )
            action = "instant_recovery_failed"
            detail["reason"] = "not_claimed"
        elif plan.execution_status in (S.MAPPING.value, S.STARTING.value):
            last = plan.ir_last_progress_at or plan.ir_claimed_at
            if last is not None and last <= now - START_TIMEOUT:
                fail(
                    plan,
                    "The lab agent stopped reporting while starting the VM. Use Discard to remove what it created.",
                    now=now,
                )
                action = "instant_recovery_failed"
                detail["reason"] = "start_timeout"
        elif plan.execution_status == S.MOVING_DISKS.value:
            try:
                action = _advance_move(db, settings, plan, now)
            except Exception as exc:  # one plan's PVE trouble must not stop the others
                logger.exception("instant recovery: advancing the move of plan %s failed", plan.id)
                plan.ir_last_error = f"Could not drive the disk move: {exc}"[:4000]
            if action:
                detail["move_disks"] = plan.ir_move_disks
        if action:
            record_audit(db, plan, action, actor_user_id=None, detail=detail)
            changes.append((plan.id, action))
    return changes


def overlay_storage_percent(plan: RestorePlan) -> float | None:
    if not plan.ir_storage_total_bytes or plan.ir_storage_free_bytes is None:
        return None
    used = plan.ir_storage_total_bytes - plan.ir_storage_free_bytes
    return used / plan.ir_storage_total_bytes * 100


def active_plans_reading_from(db: Session, *, pbs_instance_id: uuid.UUID, datastore_name: str | None = None):
    """Instant Recovery plans that read from this PBS instance (optionally one
    datastore) right now -- for the warnings before maintenance mode / updates."""
    from resurgam_api.models.datastore import Datastore
    from resurgam_api.models.inventory import BackupGroup

    query = (
        db.query(RestorePlan)
        .join(BackupGroup, BackupGroup.id == RestorePlan.backup_group_id)
        .join(Datastore, Datastore.id == BackupGroup.datastore_id)
        .filter(
            RestorePlan.method == RestorePlanMethod.INSTANT.value,
            RestorePlan.execution_status.in_(list(READS_FROM_PBS_STATUSES)),
            Datastore.pbs_instance_id == pbs_instance_id,
        )
    )
    if datastore_name is not None:
        query = query.filter(Datastore.name == datastore_name)
    return query.all()
