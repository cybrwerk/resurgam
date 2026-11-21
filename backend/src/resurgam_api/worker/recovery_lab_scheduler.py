"""The out-of-process scheduler for Recovery Lab runs (manifest EPIC 10 /
§22), launched as a separate OS process from the uvicorn API server (dev:
`uv run resurgam-recovery-lab-scheduler`, run from `backend/`), matching
`collector_scheduler.py`/`file_restore_worker.py`/`file_index_worker.py`'s
own convention for periodic background work.

Raised directly by the user, in one message covering two separate gaps:

1. A Lab run only ever advanced (next member queued, guest-agent probing
   checked, aggregate status recomputed) when someone was watching
   `RecoveryLabPage.tsx` and clicking its manual "Poll now" button --
   confirmed via grep that `advance_restore_test_plan`
   (`api/v1/restore_test_plans.py`, extracted from the `poll` endpoint
   for exactly this reuse) was otherwise never called automatically
   anywhere in the codebase.
2. "the lab Scheduler (Cron Like is not there)" -- no recurring
   "run this Application Group automatically" feature existed either,
   even though it was designed and approved earlier in the same session
   (one schedule per Application Group, `ApplicationGroup.lab_schedule_*`).

Both gaps are handled by this one worker (not split into two), since
both are the same "periodic background work" concern this codebase
already gives its own process per concern (`collector_scheduler.py`,
`file_restore_worker.py`, `file_index_worker.py`).

Each tick also runs the Disk Mounts sweep (ADR 0025 §4,
`disk_mounts.lifecycle.sweep`): it expires ready mounts past their
lifetime, ends approval requests nobody acted on, and fails mounts the
agent never picked up or never finished -- so this process must run for
disk mount lifetimes to be enforced.

No lease/heartbeat mechanism, same acceptance as the other three workers:
a single-instance deployment is this codebase's current target, and one
run's (or one group's scheduled-run attempt's) failure is simply retried
next tick -- it doesn't abort the others in the same tick.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from resurgam_api.api.v1.restore_test_plans import (
    advance_restore_test_plan,
    create_plan_for_group,
    members_ordered,
    provision_and_execute_plan,
)
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_session_factory
from resurgam_api.disk_mounts.lifecycle import sweep as sweep_disk_mounts
from resurgam_api.instant_recovery.lifecycle import advance as advance_instant_recoveries
from resurgam_api.models.application_group import ApplicationGroup
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.restore_test import RestoreTestPlan, RestoreTestPlanStatus

logger = logging.getLogger("resurgam_api.worker.recovery_lab_scheduler")

# Lab members move faster than a PBS sync interval, and the frontend's own
# manual "Poll now" / `RunDetailModal` polling already re-fetches every 3s
# while a run is active -- 15s keeps this worker responsive without
# fighting that cadence or hammering PVE on every tick.
_TICK_SECONDS = 15

# `PROVISIONING_APPLIANCE` is included even though `advance_restore_test_plan`
# is currently a no-op for it (driven by `lab_agent.py`'s own callbacks
# instead) -- harmless to call, and keeps this worker's due-set in sync
# with the endpoint's own guard without duplicating that guard's logic here.
_ACTIVE_STATUSES = (RestoreTestPlanStatus.PROVISIONING_APPLIANCE.value, RestoreTestPlanStatus.RUNNING.value)


def _active_plans(db: Session) -> list[RestoreTestPlan]:
    return list(db.execute(select(RestoreTestPlan).where(RestoreTestPlan.status.in_(_ACTIVE_STATUSES))).scalars().all())


def _due_groups(db: Session) -> list[ApplicationGroup]:
    now = datetime.now(UTC)
    return list(
        db.execute(
            select(ApplicationGroup).where(
                ApplicationGroup.lab_schedule_enabled.is_(True),
                ApplicationGroup.lab_next_scheduled_run_at <= now,
            )
        )
        .scalars()
        .all()
    )


def _advance_active_plan(db: Session, settings: Settings, plan: RestoreTestPlan) -> None:
    members = members_ordered(db, plan)
    advance_restore_test_plan(db, settings, plan, members)
    db.add(
        AuditEvent(
            actor_user_id=None,
            action="restore_test_plan_polled",
            target_type="restore_test_plan",
            target_id=str(plan.id),
            customer_id=plan.customer_id,
            detail={"status": plan.status, "source": "recovery_lab_scheduler"},
        )
    )
    db.commit()


def _run_due_group(db: Session, settings: Settings, group: ApplicationGroup) -> None:
    """Creates and immediately executes one scheduled run for `group`,
    mirroring `POST /lab-runs` + `POST /lab-runs/{id}/execute` back to
    back -- a scheduled run has no human session to hand a "planned, not
    yet executed" run off to, so there's no reason to leave it PLANNED.
    `lab_next_scheduled_run_at` always advances by a full interval,
    success or failure (captured before the attempt, applied in a fresh
    statement on failure since the failed attempt's own changes are
    rolled back) -- same reasoning `file_index_worker.py`'s own scheduled
    work already uses: a persistently broken cluster must not retry every
    tick forever.
    """
    group_id = group.id
    interval_hours = group.lab_schedule_interval_hours
    now = datetime.now(UTC)
    try:
        plan, member_count = create_plan_for_group(
            db,
            settings,
            group,
            pve_cluster_id=group.lab_schedule_pve_cluster_id,
            target_node=group.lab_schedule_target_node,
            target_storage=group.lab_schedule_target_storage,
            created_by_user_id=None,
        )
        members = members_ordered(db, plan)
        provision_and_execute_plan(db, settings, plan, members)
        db.add(
            AuditEvent(
                actor_user_id=None,
                action="restore_test_plan_created",
                target_type="restore_test_plan",
                target_id=str(plan.id),
                customer_id=plan.customer_id,
                detail={
                    "application_group_id": str(group_id),
                    "member_count": member_count,
                    "source": "recovery_lab_scheduler",
                },
            )
        )
        db.add(
            AuditEvent(
                actor_user_id=None,
                action="restore_test_plan_execution_started",
                target_type="restore_test_plan",
                target_id=str(plan.id),
                customer_id=plan.customer_id,
                detail={"member_id": str(members[0].id), "source": "recovery_lab_scheduler"},
            )
        )
        group.lab_next_scheduled_run_at = now + timedelta(hours=interval_hours)
        db.commit()
    except Exception:  # one group's failure must not abort the others or crash the loop
        db.rollback()
        logger.exception("recovery lab scheduler: scheduled run failed for application group %s", group_id)
        due_group = db.get(ApplicationGroup, group_id)
        if due_group is not None:
            due_group.lab_next_scheduled_run_at = now + timedelta(hours=interval_hours)
            db.commit()


def _advance_instant_recoveries(db: Session, settings: Settings) -> None:
    """Instant Recovery (ADR 0030 §5/§6) rides on this loop too: drive Move to
    storage (one PVE `move_disk` task per disk), and fail plans the agent never
    picked up or stopped reporting on."""
    try:
        changes = advance_instant_recoveries(db, settings)
        db.commit()
        for plan_id, action in changes:
            logger.info("recovery lab scheduler: instant recovery %s -> %s", plan_id, action)
    except Exception:  # never let it take the lab scheduler down
        db.rollback()
        logger.exception("recovery lab scheduler: instant recovery pass failed")


def _sweep_disk_mounts(db: Session) -> None:
    """Disk Mounts (ADR 0025 §4) ride on this loop instead of a process of their
    own: expire ready mounts past their lifetime, end approvals nobody acted on,
    and catch mounts the agent never picked up or never finished."""
    try:
        changes = sweep_disk_mounts(db)
        db.commit()
        for mount_id, action in changes:
            logger.info("recovery lab scheduler: disk mount %s -> %s", mount_id, action)
    except Exception:  # never let the sweep take the lab scheduler down
        db.rollback()
        logger.exception("recovery lab scheduler: disk mount sweep failed")


def run_forever() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    session_factory = get_session_factory()
    logger.info("recovery lab scheduler starting (tick %ds)", _TICK_SECONDS)

    while True:
        with session_factory() as db:
            for plan in _active_plans(db):
                try:
                    _advance_active_plan(db, settings, plan)
                except Exception:  # one run's failure must not abort the others or crash the loop
                    db.rollback()
                    logger.exception("recovery lab scheduler: advance failed for run %s", plan.id)

            for group in _due_groups(db):
                _run_due_group(db, settings, group)

            _sweep_disk_mounts(db)
            _advance_instant_recoveries(db, settings)

        time.sleep(_TICK_SECONDS)


if __name__ == "__main__":
    run_forever()
