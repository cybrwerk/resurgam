"""ADR 0032, part 1: PVE guests that no enabled backup job covers.

Two sources, because PVE's own list is not the whole picture:

- `GET /cluster/backup-info/not-backed-up` lists guests no job covers at all
  (`no_job`). It counts disabled jobs as coverage (pve-manager
  `PVE/API2/Cluster/BackupInfo.pm`, `get_included_vmids` ignores `enabled`).
- So guests covered only by disabled jobs (`only_disabled_jobs`) come from
  `GET /cluster/backup/{id}/included_volumes` of every job: covered by a
  disabled job and by no enabled one.

RESURGAM's own short-lived VMs (lab template, disk mount helpers, members of
recent lab runs) are left out; anything else that is fine without a backup
gets an exception with a reason (`BackupGapException`), not a code change.
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
from resurgam_api.connectivity.pve_api import (
    list_guests,
    list_job_included_vmids,
    list_not_backed_up_guests,
    list_vzdump_jobs,
)
from resurgam_api.models.backup_gaps import GuestGapReason, PveGuestBackupGap
from resurgam_api.models.disk_mount import TERMINAL_STATUSES, DiskMount
from resurgam_api.models.pbs import TlsTrustMode
from resurgam_api.models.pve import PveCluster
from resurgam_api.models.restore_test import RestoreTestPlan, RestoreTestPlanMember
from resurgam_api.models.secrets import SecretReference
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

logger = logging.getLogger(__name__)

# Members of lab runs started this recently are RESURGAM's own VMs, still
# running or about to be cleaned up. A lab VM left behind for longer is a
# real leftover worth seeing.
LAB_RUN_GRACE = timedelta(hours=24)


def _trust_and_token(db: Session, settings: Settings, cluster: PveCluster) -> tuple[TrustConfig, str]:
    secret_ref = db.get(SecretReference, cluster.api_token_secret_ref_id)
    token = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key).retrieve(secret_ref.reference_key)
    trust = TrustConfig(
        trust_mode=TlsTrustMode(cluster.tls_trust_mode), fingerprint_sha256=cluster.tls_fingerprint_sha256
    )
    return trust, token


def resurgam_owned_vmids(db: Session, cluster: PveCluster, now: datetime) -> set[int]:
    """VMIDs RESURGAM itself created on this cluster and that are not meant to
    be backed up."""
    owned = {
        vmid
        for vmid in (cluster.lab_appliance_template_vmid, cluster.disk_mount_helper_template_vmid)
        if vmid is not None
    }
    owned |= set(
        db.execute(
            select(DiskMount.helper_vmid).where(
                DiskMount.pve_cluster_id == cluster.id,
                DiskMount.helper_vmid.isnot(None),
                DiskMount.status.notin_(TERMINAL_STATUSES),
            )
        ).scalars()
    )
    owned |= set(
        db.execute(
            select(RestoreTestPlanMember.target_vmid)
            .join(RestoreTestPlan, RestoreTestPlan.id == RestoreTestPlanMember.restore_test_plan_id)
            .where(RestoreTestPlan.pve_cluster_id == cluster.id, RestoreTestPlan.created_at >= now - LAB_RUN_GRACE)
        ).scalars()
    )
    return owned


def check_cluster_backup_gaps(db: Session, settings: Settings, cluster: PveCluster) -> bool:
    """Refresh `pve_guest_backup_gaps` for one cluster. Returns whether the
    check reached PVE; on failure the previous rows stay and
    `backup_gaps_error` says why."""
    now = datetime.now(UTC)
    cluster.backup_gaps_checked_at = now
    if stored_token_is_placeholder(db, settings, cluster.api_token_secret_ref_id):
        cluster.backup_gaps_error = "PVE API token secret is not set for this cluster"
        return False
    trust, token = _trust_and_token(db, settings, cluster)
    timeout = settings.connectivity_timeout_seconds

    not_backed_up = list_not_backed_up_guests(cluster.host, cluster.port, trust, token, timeout)
    jobs = list_vzdump_jobs(cluster.host, cluster.port, trust, token, timeout)
    if not not_backed_up.reachable or not jobs.reachable:
        cluster.backup_gaps_error = not_backed_up.error or jobs.error
        return False

    gaps: dict[int, tuple[str, str | None, str]] = {
        g.vmid: (g.guest_type, g.name, GuestGapReason.NO_JOB.value) for g in not_backed_up.guests
    }

    disabled_jobs = [job for job in jobs.jobs if not job.enabled]
    if disabled_jobs:
        covered_enabled: set[int] = set()
        covered_disabled: set[int] = set()
        for job in jobs.jobs:
            included = list_job_included_vmids(cluster.host, cluster.port, trust, token, timeout, job_id=job.id)
            if not included.reachable:
                cluster.backup_gaps_error = f"backup job {job.id}: {included.error}"
                return False
            (covered_enabled if job.enabled else covered_disabled).update(included.vmids)
        only_disabled = covered_disabled - covered_enabled - set(gaps)
        if only_disabled:
            guests = list_guests(cluster.host, cluster.port, trust, token, timeout)
            known = {g.vmid: g for g in guests.guests or []} if guests.reachable else {}
            for vmid in only_disabled:
                guest = known.get(vmid)
                if guest is None:
                    # Removed but not purged from the job, or no VM.Audit on it.
                    continue
                gaps[vmid] = (guest.guest_type, guest.name, GuestGapReason.ONLY_DISABLED_JOBS.value)

    for vmid in resurgam_owned_vmids(db, cluster, now):
        gaps.pop(vmid, None)

    existing = {row.vmid: row for row in db.query(PveGuestBackupGap).filter_by(pve_cluster_id=cluster.id).all()}
    for vmid, (guest_type, name, reason) in gaps.items():
        row = existing.pop(vmid, None)
        if row is None:
            db.add(
                PveGuestBackupGap(
                    pve_cluster_id=cluster.id,
                    vmid=vmid,
                    guest_type=guest_type,
                    name=name,
                    reason=reason,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        else:
            row.guest_type, row.name, row.reason, row.last_seen_at = guest_type, name, reason, now
    for row in existing.values():
        db.delete(row)
    cluster.backup_gaps_error = None
    db.flush()
    return True


def run_due_backup_gap_checks(db: Session, settings: Settings, interval_minutes: int) -> int:
    """Check every enabled PVE cluster whose last check is older than the
    collector interval. One cluster's failure never stops the others.
    Returns how many clusters were checked."""
    cutoff = datetime.now(UTC) - timedelta(minutes=interval_minutes)
    cluster_ids: list[uuid.UUID] = list(
        db.execute(
            select(PveCluster.id).where(
                PveCluster.disabled.is_(False),
                (PveCluster.backup_gaps_checked_at.is_(None)) | (PveCluster.backup_gaps_checked_at <= cutoff),
            )
        ).scalars()
    )
    checked = 0
    for cluster_id in cluster_ids:
        try:
            cluster = db.get(PveCluster, cluster_id)
            if not check_cluster_backup_gaps(db, settings, cluster):
                logger.warning(
                    "backup gaps: check failed for PVE cluster %s: %s", cluster_id, cluster.backup_gaps_error
                )
            db.commit()
            checked += 1
        except Exception:  # one cluster's failure must not stop the others or the collector
            db.rollback()
            logger.exception("backup gaps: check raised for PVE cluster %s", cluster_id)
    return checked
