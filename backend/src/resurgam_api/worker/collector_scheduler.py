"""The out-of-process scheduler for the Collector (`collector/sync.py`).

Launched as a *separate* OS process from the uvicorn API server (dev:
`uv run resurgam-collector-scheduler`, run from `backend/`), matching
`file_restore_worker.py`/`file_index_worker.py`'s own convention for
periodic background work rather than an asyncio task folded into the API
process. Unlike those two, this worker needs no elevated privileges --
it only makes the same PBS/PVE HTTP calls the manual "Sync" button
already triggers via `sync_pbs_instance` -- so it's a third worker
purely to keep "periodic background work" in one consistent place, not
because it needs a separate privilege boundary.

Raised directly by the user after finding the Collector had no scheduler
at all (only the manual "Sync" button, `POST /pbs-instances/{id}/sync`):
this worker re-syncs every non-disabled `PbsInstance` on an interval
that defaults to 15 minutes and is editable in System Settings
(`InstanceSettings.collector_poll_interval_minutes`). The interval is
re-read from the DB on every tick (`_TICK_SECONDS`, independent of the
sync interval itself) so a change in System Settings takes effect
without restarting this process.

No lease/heartbeat mechanism, same acceptance as the other two workers:
a single-instance deployment is this codebase's current target, and one
instance's sync failing (network blip, PBS restart) is simply retried
next tick -- it doesn't abort the others in the same tick, mirroring
`sync_pbs_instance`'s own per-datastore isolation.

Each tick also runs the connection heartbeat (`collector/heartbeat.py`):
every non-disabled PBS instance *and PVE cluster* gets a health check on
its own, shorter fixed interval (`HEARTBEAT_INTERVAL_MINUTES`, 5 min),
recording `last_health_check_at`/`last_health_status` -- the "last
heartbeat" the UI shows. It runs before the inventory sync and is
isolated from it: a heartbeat failure never skips the sync, and vice
versa. Datastore discovery needs nothing here -- `sync_pbs_instance`
itself starts with it, so every scheduled pass imports new datastores.

Each tick also looks for new RESURGAM releases when an update source is
configured with automatic checks on (`update_source.run_due_check`, ADR
0026) -- at most once per its own `check_interval_hours`, isolated like
the heartbeat. It only caches what it finds; nothing is downloaded.

Each tick also enables PVE backup jobs whose timed "disable for N hours"
has run out (`pve_job_reenable.run_due_reenables`, ADR 0018 addendum
2026-11-11) -- isolated like the heartbeat, each job on its own.

After the tick, at most every `ALERT_MAILER_INTERVAL_SECONDS`, the
per-customer alert mailer (`alert_mailer.py`) mails alarm changes --
right after the sync, which is what changes the alarms. Isolated too:
mail trouble never affects the collector.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from resurgam_api.alert_mailer import ALERT_MAILER_INTERVAL_SECONDS, run_alert_mailer
from resurgam_api.collector.heartbeat import (
    HEARTBEAT_INTERVAL_MINUTES,
    run_due_heartbeats,
    stored_token_is_placeholder,
)
from resurgam_api.collector.sync import sync_pbs_instance
from resurgam_api.config import get_settings
from resurgam_api.db import get_session_factory
from resurgam_api.encryption_keys import adopt_datastore_keys
from resurgam_api.instance_settings import effective_collector_poll_interval_minutes
from resurgam_api.models.pbs import PbsInstance
from resurgam_api.pve_job_reenable import run_due_reenables
from resurgam_api.update_source import run_due_check

logger = logging.getLogger("resurgam_api.worker.collector_scheduler")

# How often the scheduler wakes up to check which instances are due --
# deliberately much shorter than the sync interval itself (which can be
# as low as 1 minute, user-configurable), so a lowered interval or a
# newly-enabled instance is picked up promptly rather than waiting out
# whatever the *previous* interval happened to be.
_TICK_SECONDS = 60


def _due_instances(db: Session, interval_minutes: int) -> list[PbsInstance]:
    cutoff = datetime.now(UTC) - timedelta(minutes=interval_minutes)
    return list(
        db.execute(
            select(PbsInstance).where(
                PbsInstance.disabled.is_(False),
                (PbsInstance.last_collector_sync_at.is_(None)) | (PbsInstance.last_collector_sync_at <= cutoff),
            )
        )
        .scalars()
        .all()
    )


def run_tick(db: Session, settings) -> None:
    """One scheduler pass: heartbeat for every due PBS/PVE target, then
    the inventory sync for every due PBS instance. Each half (and each
    target within it) is isolated -- nothing here may raise out."""
    try:
        run_due_heartbeats(db, settings, HEARTBEAT_INTERVAL_MINUTES)
    except Exception:  # e.g. the target query itself failing -- still run the sync below
        db.rollback()
        logger.exception("collector scheduler: heartbeat pass failed")

    try:
        run_due_check(db, settings)
    except Exception:  # GitHub/GitLab trouble must never hold up the collector
        db.rollback()
        logger.exception("collector scheduler: update source check failed")

    try:
        run_due_reenables(db, settings)
    except Exception:  # a PVE problem must never hold up the collector
        db.rollback()
        logger.exception("collector scheduler: automatic backup job re-enable pass failed")

    try:
        # ADR 0031: move earlier per-datastore keys (ADR 0022) into the key
        # store. A no-op once done; cheap enough to check every tick.
        if adopt_datastore_keys(db, settings):
            db.commit()
    except Exception:  # never hold up the collector
        db.rollback()
        logger.exception("collector scheduler: moving datastore encryption keys into the key store failed")

    try:
        interval_minutes = effective_collector_poll_interval_minutes(db)
        due = _due_instances(db, interval_minutes)
    except Exception:
        db.rollback()
        logger.exception("collector scheduler: could not determine due PBS instances")
        return

    for instance in due:
        instance_id = instance.id
        try:
            if stored_token_is_placeholder(db, settings, instance.api_token_secret_ref_id):
                # Imported from a configuration backup, secret not re-entered
                # yet: every PBS call would be rejected (401). The heartbeat
                # already shows "auth_failed: secret not set" for it.
                logger.info("collector scheduler: skipping PBS instance %s, API token secret not set", instance_id)
                continue
            sync_pbs_instance(db, settings, instance)
            db.commit()
        except Exception:  # one instance's failure must not abort the others or crash the loop
            db.rollback()
            logger.exception("collector scheduler: sync failed for PBS instance %s", instance_id)


def run_forever() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    session_factory = get_session_factory()
    logger.info(
        "collector scheduler starting (tick %ds, inventory interval default 15min editable in System Settings, "
        "heartbeat every %dmin)",
        _TICK_SECONDS,
        HEARTBEAT_INTERVAL_MINUTES,
    )

    last_alert_mailer_run: float | None = None
    while True:
        with session_factory() as db:
            run_tick(db, settings)

            if (
                last_alert_mailer_run is None
                or time.monotonic() - last_alert_mailer_run >= ALERT_MAILER_INTERVAL_SECONDS
            ):
                last_alert_mailer_run = time.monotonic()
                try:
                    run_alert_mailer(db, settings)
                except Exception:  # mail trouble must never stop the collector loop
                    db.rollback()
                    logger.exception("collector scheduler: alert mailer pass failed")

        time.sleep(_TICK_SECONDS)


if __name__ == "__main__":
    run_forever()
