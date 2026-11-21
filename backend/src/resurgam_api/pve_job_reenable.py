"""Timed automatic re-enable of disabled PVE vzdump backup jobs
("disable for N hours", ADR 0018 addendum 2026-11-11).

Disabling a backup job for a maintenance window is the common case, and
forgetting to switch it back on is the common failure: the job stays off
silently and backups stop. So the disable action can carry a duration;
the deadline is stored in `PveBackupJobReenable` and the collector
scheduler (`worker/collector_scheduler.py`, which already ticks every
minute) calls `run_due_reenables` to enable the job in PVE once it has
passed.

At fire time the job is re-read from PVE first, so the pass never acts on
stale assumptions:

- PVE unreachable: keep the row, record the error, retry next tick.
- Job no longer exists: drop the row (audited), nothing to enable.
- Job already enabled (someone enabled it in PVE directly): drop the row.
- Otherwise: enable it, audit `pve_vzdump_job_auto_reenabled`, drop the
  row. A failed write keeps the row with `last_error` for the UI and is
  retried next tick -- a pending re-enable is never silently given up.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from resurgam_api.collector.heartbeat import stored_token_is_placeholder
from resurgam_api.config import Settings
from resurgam_api.connectivity.models import TrustConfig
from resurgam_api.connectivity.pve_api import PveWriteError, list_vzdump_jobs, set_vzdump_job_enabled
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.pbs import TlsTrustMode
from resurgam_api.models.pve import PveBackupJobReenable, PveCluster
from resurgam_api.models.secrets import SecretReference
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

logger = logging.getLogger("resurgam_api.pve_job_reenable")

# One week: anything longer is not a maintenance window but a decision to
# switch the job off, which a plain (indefinite) disable expresses better.
REENABLE_MAX_HOURS = 168

AUTO_REENABLE_ACTOR_LABEL = "Scheduler (automatic re-enable)"


def get_reenable(db: Session, cluster_id: uuid.UUID, job_id: str) -> PveBackupJobReenable | None:
    return db.execute(
        select(PveBackupJobReenable).where(
            PveBackupJobReenable.pve_cluster_id == cluster_id, PveBackupJobReenable.job_id == job_id
        )
    ).scalar_one_or_none()


def schedule_reenable(
    db: Session,
    cluster_id: uuid.UUID,
    job_id: str,
    hours: int,
    requested_by_user_id: uuid.UUID | None,
    now: datetime | None = None,
) -> PveBackupJobReenable:
    """Create or replace the pending re-enable for one job. Does not touch
    PVE and does not commit."""
    reenable_at = (now or datetime.now(UTC)) + timedelta(hours=hours)
    row = get_reenable(db, cluster_id, job_id)
    if row is None:
        row = PveBackupJobReenable(pve_cluster_id=cluster_id, job_id=job_id, reenable_at=reenable_at)
        db.add(row)
    row.reenable_at = reenable_at
    row.requested_by_user_id = requested_by_user_id
    row.attempts = 0
    row.last_attempt_at = None
    row.last_error = None
    db.flush()
    return row


def clear_reenable(db: Session, cluster_id: uuid.UUID, job_id: str) -> datetime | None:
    """Drop the pending re-enable for one job, if any. Returns the
    deadline it had (for audit detail), else None. Does not commit."""
    row = get_reenable(db, cluster_id, job_id)
    if row is None:
        return None
    reenable_at = row.reenable_at
    db.delete(row)
    db.flush()
    return reenable_at


def _audit(db: Session, cluster_id: uuid.UUID, action: str, detail: dict) -> None:
    db.add(
        AuditEvent(
            actor_user_id=None,
            actor_label=AUTO_REENABLE_ACTOR_LABEL,
            action=action,
            target_type="pve_cluster",
            target_id=str(cluster_id),
            detail=detail,
        )
    )


def _trust_and_token(db: Session, settings: Settings, cluster: PveCluster) -> tuple[TrustConfig, str]:
    secret_ref = db.get(SecretReference, cluster.api_token_secret_ref_id)
    token = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key).retrieve(secret_ref.reference_key)
    trust = TrustConfig(
        trust_mode=TlsTrustMode(cluster.tls_trust_mode), fingerprint_sha256=cluster.tls_fingerprint_sha256
    )
    return trust, token


def _record_failure(row: PveBackupJobReenable, now: datetime, error: str) -> None:
    row.attempts += 1
    row.last_attempt_at = now
    row.last_error = error[:2000]


def _process(db: Session, settings: Settings, row: PveBackupJobReenable, now: datetime) -> None:
    cluster = db.get(PveCluster, row.pve_cluster_id)
    if cluster is None:  # FK cascade normally removes the row first
        db.delete(row)
        return
    if stored_token_is_placeholder(db, settings, cluster.api_token_secret_ref_id):
        _record_failure(row, now, "PVE API token secret is not set for this cluster")
        return

    trust, token = _trust_and_token(db, settings, cluster)
    timeout = settings.connectivity_timeout_seconds
    detail = {"job_id": row.job_id, "reenable_at": row.reenable_at.isoformat()}

    listing = list_vzdump_jobs(cluster.host, cluster.port, trust, token, timeout=timeout)
    if not listing.reachable:
        _record_failure(row, now, f"PVE not reachable: {listing.error}")
        return
    job = next((j for j in listing.jobs or [] if j.id == row.job_id), None)
    if job is None:
        _audit(db, cluster.id, "pve_vzdump_job_auto_reenable_dropped", {**detail, "reason": "job_not_found"})
        db.delete(row)
        return
    if job.enabled:
        _audit(db, cluster.id, "pve_vzdump_job_auto_reenable_dropped", {**detail, "reason": "already_enabled"})
        db.delete(row)
        return

    try:
        set_vzdump_job_enabled(cluster.host, cluster.port, trust, token, timeout, job_id=row.job_id, enabled=True)
    except PveWriteError as exc:
        _record_failure(row, now, str(exc))
        return

    _audit(
        db,
        cluster.id,
        "pve_vzdump_job_auto_reenabled",
        {**detail, "schedule": job.schedule, "attempts": row.attempts + 1},
    )
    db.delete(row)


def run_due_reenables(db: Session, settings: Settings, now: datetime | None = None) -> int:
    """Enable every backup job whose re-enable deadline has passed. Each
    job is isolated (own commit, own rollback); nothing here raises out.
    Returns how many rows were processed."""
    now = now or datetime.now(UTC)
    due_ids = list(
        db.execute(select(PveBackupJobReenable.id).where(PveBackupJobReenable.reenable_at <= now)).scalars().all()
    )
    for row_id in due_ids:
        try:
            row = db.get(PveBackupJobReenable, row_id)
            if row is None:  # cancelled or enabled manually meanwhile
                continue
            _process(db, settings, row, now)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("automatic re-enable failed for pending row %s", row_id)
    return len(due_ids)
