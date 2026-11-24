"""Instant Recovery (ADR 0030): the operator's actions on a VM running from its
backup, and the lab agent's backchannel.

Operator (`restore_operator` of the tenant or platform admin, as for Guided
Restore): **Move to storage** and **Discard**. Starting is the existing
`POST /restore-plans/{id}/execute` (a plan with `method=instant` is queued for
the agent there).

Agent (cluster-scoped agent token, `/lab-agent/*`): list jobs, claim a start,
report each resource as it is created, report the outcome, send a heartbeat
while the VM runs from the backup.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from resurgam_api.api.v1.lab_agent import _require_lab_agent, _resolve_encryption_keyfile
from resurgam_api.api.v1.restore_execution import _get_plan_with_restore_access
from resurgam_api.auth.dependency import get_current_user
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_db
from resurgam_api.disk_mounts.snapshot_disks import snapshot_path
from resurgam_api.errors import ResurgamApiError
from resurgam_api.instant_recovery import lifecycle
from resurgam_api.models.datastore import Datastore
from resurgam_api.models.inventory import BackupGroup, BackupSnapshot
from resurgam_api.models.pbs import PbsInstance
from resurgam_api.models.pve import PveCluster
from resurgam_api.models.restore import RestorePlan, RestorePlanExecutionStatus, RestorePlanMethod
from resurgam_api.schemas.instant_recovery import (
    InstantRecoveryAgentJobOut,
    InstantRecoveryClaimOut,
    InstantRecoveryHeartbeatRequest,
    InstantRecoveryMappedDevice,
    InstantRecoveryProgressRequest,
    InstantRecoveryStatusRequest,
)
from resurgam_api.schemas.restore import (
    InstantRecoveryDiscardRequest,
    InstantRecoveryMoveRequest,
    RestorePlanOut,
)

S = RestorePlanExecutionStatus

router = APIRouter(prefix="/restore-plans", tags=["instant-recovery"])
agent_router = APIRouter(prefix="/lab-agent", tags=["instant-recovery"])


def _instant_plan(db: Session, plan_id: uuid.UUID, user: AuthenticatedUser) -> RestorePlan:
    plan = _get_plan_with_restore_access(db, plan_id, user, for_update=True)
    if plan.method != RestorePlanMethod.INSTANT.value:
        raise ResurgamApiError(
            code="not_instant_recovery", message="This restore plan is not an Instant Recovery.", status_code=409
        )
    return plan


@router.post("/{plan_id}/instant-recovery/move", response_model=RestorePlanOut)
def move_instant_recovery_to_storage(
    plan_id: uuid.UUID,
    body: InstantRecoveryMoveRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> RestorePlanOut:
    """**Move to storage**: copies every disk of the VM running from the backup to
    the plan's `target_storage` while it keeps running (PVE `move_disk`, one disk
    after another, source overlay deleted after each copy). Afterwards the agent
    unmaps the backup and the VM is an ordinary VM (`completed`).

    Allowed in `running_from_backup` -- also to retry after a failed disk move
    (disks already moved are skipped). Progress: `instant_recovery.move_disks`."""
    plan = _instant_plan(db, plan_id, user)
    if plan.execution_status != S.RUNNING_FROM_BACKUP.value:
        raise ResurgamApiError(
            code="not_running_from_backup",
            message="Move to storage is only possible while the VM runs from the backup.",
            status_code=409,
        )
    if not plan.ir_overlay_volumes:
        raise ResurgamApiError(code="nothing_to_move", message="The agent reported no disks to move.", status_code=409)
    now = datetime.now(UTC)
    lifecycle.start_move(plan, bwlimit_kib=body.bwlimit_kib, user_id=user.user_id, now=now)
    lifecycle.record_audit(
        db,
        plan,
        "instant_recovery_move_requested",
        actor_user_id=user.user_id,
        correlation_id=getattr(request.state, "correlation_id", None),
        detail={"bwlimit_kib": body.bwlimit_kib, "disks": [d["slot"] for d in plan.ir_move_disks]},
    )
    db.commit()
    db.refresh(plan)
    return RestorePlanOut.model_validate(plan)


@router.post("/{plan_id}/instant-recovery/discard", response_model=RestorePlanOut)
def discard_instant_recovery(
    plan_id: uuid.UUID,
    body: InstantRecoveryDiscardRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> RestorePlanOut:
    """**Discard**: the agent stops and deletes the VM, deletes its overlays and
    unmaps the backup. Everything written since the start is lost; the backup
    itself was never written to. `confirm` must be true.

    Allowed while the VM runs from the backup (not during a disk move -- wait for
    it, or let it fail) and after a failed start, to remove what is left."""
    plan = _instant_plan(db, plan_id, user)
    if not body.confirm:
        raise ResurgamApiError(
            code="confirmation_required", message="Discard deletes the VM; confirm it explicitly.", status_code=400
        )
    allowed = (S.RUNNING_FROM_BACKUP.value, S.FAILED.value)
    if plan.execution_status not in allowed:
        raise ResurgamApiError(
            code="discard_not_possible",
            message="Discard is possible while the VM runs from the backup or after a failed start.",
            status_code=409,
        )
    if plan.execution_status == S.FAILED.value and not (plan.ir_vm_created or plan.ir_mapped_devices):
        raise ResurgamApiError(code="nothing_to_discard", message="Nothing of this plan is left.", status_code=409)
    now = datetime.now(UTC)
    plan.execution_status = S.DISCARDING.value
    plan.ir_discard_requested_at = now
    plan.ir_discard_requested_by_user_id = user.user_id
    plan.ir_next_attempt_at = None
    plan.ir_last_error = None
    lifecycle.record_audit(
        db,
        plan,
        "instant_recovery_discard_requested",
        actor_user_id=user.user_id,
        correlation_id=getattr(request.state, "correlation_id", None),
        detail={"target_vmid": plan.target_vmid},
    )
    db.commit()
    db.refresh(plan)
    return RestorePlanOut.model_validate(plan)


# --- agent backchannel ---------------------------------------------------------

_AGENT_KIND = {
    S.QUEUED.value: "start",
    S.RUNNING_FROM_BACKUP.value: "watch",
    S.MOVING_DISKS.value: "watch",
    S.FINISHING.value: "finish",
    S.DISCARDING.value: "discard",
}


def _snapshot_path_for(db: Session, plan: RestorePlan) -> str:
    group = db.get(BackupGroup, plan.backup_group_id)
    snapshot = db.get(BackupSnapshot, plan.backup_snapshot_id)
    return snapshot_path(group.backup_type, group.backup_id, snapshot.snapshot_time)


def _plan_for_agent(db: Session, plan_id: uuid.UUID, cluster: PveCluster) -> RestorePlan:
    plan = db.query(RestorePlan).filter_by(id=plan_id).with_for_update().one_or_none()
    if plan is None or plan.pve_cluster_id != cluster.id or plan.method != RestorePlanMethod.INSTANT.value:
        raise ResurgamApiError(code="not_found", message="Job not found.", status_code=404)
    return plan


@agent_router.get("/instant-recovery-jobs", response_model=list[InstantRecoveryAgentJobOut])
def list_instant_recovery_jobs(
    db: Session = Depends(get_db), authorization: str | None = Header(default=None)
) -> list[InstantRecoveryAgentJobOut]:
    """Instant Recovery work for the authenticated agent's cluster: starts to
    claim, running VMs to watch, finished moves to clean up, discards. A
    finish/discard that failed is offered again after a minute. No secrets."""
    cluster = _require_lab_agent(db, authorization)
    now = datetime.now(UTC)
    plans = (
        db.query(RestorePlan)
        .filter(
            RestorePlan.pve_cluster_id == cluster.id,
            RestorePlan.method == RestorePlanMethod.INSTANT.value,
            RestorePlan.execution_status.in_(list(_AGENT_KIND)),
        )
        .order_by(RestorePlan.created_at)
        .all()
    )
    jobs = []
    for plan in plans:
        if plan.ir_next_attempt_at is not None and plan.ir_next_attempt_at > now:
            continue
        jobs.append(
            InstantRecoveryAgentJobOut(
                id=plan.id,
                kind=_AGENT_KIND[plan.execution_status],
                target_node=plan.target_node,
                target_vmid=plan.target_vmid,
                name=plan.temp_name,
                vm_created=plan.ir_vm_created,
                mapped_devices=[InstantRecoveryMappedDevice.model_validate(d) for d in plan.ir_mapped_devices or []],
                snapshot_path=_snapshot_path_for(db, plan),
            )
        )
    return jobs


@agent_router.post("/instant-recovery-jobs/{plan_id}/claim", response_model=InstantRecoveryClaimOut)
def claim_instant_recovery_job(
    plan_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
) -> InstantRecoveryClaimOut:
    """`queued` -> `mapping` (row-locked: only one agent starts it). Returns
    everything the start needs, including the PBS encryption key if any."""
    cluster = _require_lab_agent(db, authorization)
    plan = _plan_for_agent(db, plan_id, cluster)
    if plan.execution_status != S.QUEUED.value:
        raise ResurgamApiError(code="not_queued", message="This Instant Recovery is not queued.", status_code=409)
    group = db.get(BackupGroup, plan.backup_group_id)
    datastore = db.get(Datastore, group.datastore_id)
    pbs = db.get(PbsInstance, datastore.pbs_instance_id)
    claim = InstantRecoveryClaimOut(
        id=plan.id,
        target_node=plan.target_node,
        target_vmid=plan.target_vmid,
        target_storage=plan.target_storage,
        name=plan.temp_name,
        pbs_host=pbs.host,
        pbs_port=pbs.port,
        pbs_datastore=datastore.name,
        pbs_namespace=group.namespace or "",
        snapshot_path=_snapshot_path_for(db, plan),
        network_mode=plan.network_mode,
        network_bridge=plan.network_bridge,
        keep_mac=plan.ir_keep_mac,
        min_free_bytes=lifecycle.MIN_FREE_BYTES,
        encryption_keyfile=_resolve_encryption_keyfile(db, settings, db.get(BackupSnapshot, plan.backup_snapshot_id)),
    )
    now = datetime.now(UTC)
    plan.execution_status = S.MAPPING.value
    plan.ir_claimed_at = now
    plan.ir_last_progress_at = now
    lifecycle.record_audit(db, plan, "instant_recovery_claimed", actor_user_id=None, detail={"node": plan.target_node})
    db.commit()
    return claim


@agent_router.post("/instant-recovery-jobs/{plan_id}/progress", status_code=204)
def report_instant_recovery_progress(
    plan_id: uuid.UUID,
    body: InstantRecoveryProgressRequest,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> None:
    """Records a resource the agent just created (mapping, overlay, imported
    volume, the VM itself), so a discard always knows what exists."""
    cluster = _require_lab_agent(db, authorization)
    plan = _plan_for_agent(db, plan_id, cluster)
    if plan.execution_status not in (S.MAPPING.value, S.STARTING.value):
        raise ResurgamApiError(code="not_starting", message="This Instant Recovery is not starting.", status_code=409)
    if body.mapped_device is not None:
        plan.ir_mapped_devices = [*(plan.ir_mapped_devices or []), body.mapped_device.model_dump()]
    if body.overlay_volume is not None:
        plan.ir_overlay_volumes = [*(plan.ir_overlay_volumes or []), body.overlay_volume.model_dump()]
    if body.imported_volume is not None:
        plan.ir_imported_volumes = [*(plan.ir_imported_volumes or []), body.imported_volume.model_dump()]
    if body.vm_created:
        plan.ir_vm_created = True
    if body.phase == "starting":
        plan.execution_status = S.STARTING.value
    plan.ir_last_progress_at = datetime.now(UTC)
    db.commit()


@agent_router.post("/instant-recovery-jobs/{plan_id}/status", status_code=204)
def report_instant_recovery_status(
    plan_id: uuid.UUID,
    body: InstantRecoveryStatusRequest,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> None:
    """The outcome of a start, finish or discard, or a lost mapping."""
    cluster = _require_lab_agent(db, authorization)
    plan = _plan_for_agent(db, plan_id, cluster)
    now = datetime.now(UTC)
    current = plan.execution_status
    expected = {
        "running": (S.MAPPING.value, S.STARTING.value),
        "failed": (S.MAPPING.value, S.STARTING.value),
        "lost": (S.RUNNING_FROM_BACKUP.value, S.MOVING_DISKS.value),
        "finished": (S.FINISHING.value,),
        "finish_failed": (S.FINISHING.value,),
        "discarded": (S.DISCARDING.value,),
        "discard_failed": (S.DISCARDING.value,),
    }[body.status]
    if current not in expected:
        raise ResurgamApiError(
            code="unexpected_state", message=f"Cannot report {body.status!r} while {current!r}.", status_code=409
        )

    detail: dict = {"error": body.error} if body.error else {}
    if body.status == "running":
        plan.execution_status = S.RUNNING_FROM_BACKUP.value
        plan.ir_running_since = now
        plan.ir_skipped_disks = list(body.skipped_disks)
        action = "instant_recovery_running"
        detail["skipped_devices"] = list(body.skipped_disks)
    elif body.status == "failed":
        lifecycle.fail(plan, body.error or "The start failed.", now=now)
        if body.cleaned_up:
            plan.ir_mapped_devices = []
            plan.ir_overlay_volumes = []
            plan.ir_imported_volumes = []
            plan.ir_vm_created = False
        action = "instant_recovery_failed"
        detail["cleaned_up"] = body.cleaned_up
    elif body.status == "lost":
        lifecycle.fail(plan, body.error or "The backup mapping is gone.", now=now)
        action = "instant_recovery_mapping_lost"
    elif body.status in ("finished", "discarded"):
        plan.execution_status = S.COMPLETED.value if body.status == "finished" else S.DISCARDED.value
        plan.execution_completed_at = now
        plan.ir_ended_at = now
        plan.ir_mapped_devices = []
        plan.ir_last_error = None
        plan.ir_next_attempt_at = None
        if body.status == "discarded":
            plan.ir_overlay_volumes = []
            plan.ir_imported_volumes = []
            plan.ir_vm_created = False
        action = "instant_recovery_completed" if body.status == "finished" else "instant_recovery_discarded"
    else:  # finish_failed / discard_failed: retried after the backoff
        plan.ir_last_error = body.error or "The attempt failed."
        plan.ir_next_attempt_at = now + lifecycle.RETRY_BACKOFF
        action = f"instant_recovery_{body.status}"
    lifecycle.record_audit(db, plan, action, actor_user_id=None, detail=detail or None)
    db.commit()


@agent_router.post("/instant-recovery-jobs/{plan_id}/heartbeat", status_code=204)
def report_instant_recovery_heartbeat(
    plan_id: uuid.UUID,
    body: InstantRecoveryHeartbeatRequest,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> None:
    """Overlay size and free overlay space while the VM runs from the backup --
    feeds the UI and the 80 % overlay-storage alarm."""
    cluster = _require_lab_agent(db, authorization)
    plan = _plan_for_agent(db, plan_id, cluster)
    if plan.execution_status not in lifecycle.RUNNING_FROM_BACKUP_STATUSES:
        raise ResurgamApiError(
            code="not_running_from_backup", message="This VM does not run from the backup.", status_code=409
        )
    plan.ir_last_heartbeat_at = datetime.now(UTC)
    plan.ir_overlay_bytes = body.overlay_bytes
    plan.ir_storage_total_bytes = body.storage_total_bytes
    plan.ir_storage_free_bytes = body.storage_free_bytes
    db.commit()
