"""ADR 0032: the 3-2-1 gap picture -- unprotected PVE guests (stored by the
collector, `collector/backup_gaps.py`) plus per-group offsite status
(`inventory/offsite.py`), with operator exceptions and the customer's
"offsite" leg applicability applied. One place, shared by the API, alarms,
Zabbix and the Recovery Evidence report, so they can never disagree.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from resurgam_api.auth.scope import effective_group_customer_id
from resurgam_api.inventory.offsite import OffsiteEvaluation, OffsiteStatus, evaluate_offsite
from resurgam_api.models.backup_gaps import BackupGapException, GapExceptionKind, PveGuestBackupGap
from resurgam_api.models.datastore import DatastoreCustomerAssignment
from resurgam_api.models.inventory import BackupGroup
from resurgam_api.models.leg_applicability import ThreeTwoOneOneZeroLegSetting

OFFSITE_LEG = "offsite"


def exception_key(row: BackupGapException) -> tuple:
    kind = row.target_kind
    if kind == GapExceptionKind.PVE_GUEST.value:
        return (kind, row.pve_cluster_id, row.vmid)
    if kind == GapExceptionKind.PVE_CLUSTER.value:
        return (kind, row.pve_cluster_id)
    if kind == GapExceptionKind.BACKUP_GROUP.value:
        return (kind, row.datastore_id, row.namespace or "", row.backup_type, row.backup_id)
    return (kind, row.datastore_id)


def group_exception_keys(group: BackupGroup) -> list[tuple]:
    """Most specific first: the group itself, then its whole datastore."""
    return [
        (
            GapExceptionKind.BACKUP_GROUP.value,
            group.datastore_id,
            group.namespace or "",
            group.backup_type,
            group.backup_id,
        ),
        (GapExceptionKind.DATASTORE.value, group.datastore_id),
    ]


def guest_exception_keys(gap: PveGuestBackupGap) -> list[tuple]:
    return [
        (GapExceptionKind.PVE_GUEST.value, gap.pve_cluster_id, gap.vmid),
        (GapExceptionKind.PVE_CLUSTER.value, gap.pve_cluster_id),
    ]


def current_exceptions(db: Session, now: datetime) -> dict[tuple, BackupGapException]:
    """The newest row per target, kept only while it is active and not
    expired. The table is append-only, so older rows are history."""
    latest: dict[tuple, BackupGapException] = {}
    for row in db.query(BackupGapException).order_by(BackupGapException.created_at).all():
        latest[exception_key(row)] = row
    return {key: row for key, row in latest.items() if row.active and (row.expires_at is None or row.expires_at > now)}


def _first(exceptions: dict[tuple, BackupGapException], keys: list[tuple]) -> BackupGapException | None:
    for key in keys:
        if key in exceptions:
            return exceptions[key]
    return None


@dataclass
class GuestGap:
    gap: PveGuestBackupGap
    exception: BackupGapException | None


@dataclass
class OffsiteGap:
    evaluation: OffsiteEvaluation
    customer_id: uuid.UUID | None
    # The status before an exception or leg setting overrode it.
    raw_status: OffsiteStatus
    exception: BackupGapException | None

    @property
    def status(self) -> OffsiteStatus:
        return self.evaluation.status


# Statuses that are a gap (an alarm, a Zabbix count) unless excepted.
GAP_STATUSES = frozenset(
    {
        OffsiteStatus.LAGGING,
        OffsiteStatus.MISSING,
        OffsiteStatus.LOCAL_COPY_ONLY,
        OffsiteStatus.TARGET_NOT_CONNECTED,
        OffsiteStatus.NO_COPY_JOB,
    }
)


def compute_guest_gaps(db: Session, now: datetime) -> list[GuestGap]:
    exceptions = current_exceptions(db, now)
    rows = db.query(PveGuestBackupGap).order_by(PveGuestBackupGap.pve_cluster_id, PveGuestBackupGap.vmid).all()
    return [GuestGap(gap=row, exception=_first(exceptions, guest_exception_keys(row))) for row in rows]


def compute_offsite_gaps(
    db: Session,
    visible: set[uuid.UUID] | None,
    now: datetime,
    groups: list[BackupGroup] | None = None,
) -> list[OffsiteGap]:
    """Offsite status of every current backup group (or of `groups`), filtered
    to `visible` customers (`None` = platform admin, everything). Copy edges
    and copy detection always look at all groups, so a tenant's view doesn't
    change what counts as a copy."""
    all_current = (
        db.query(BackupGroup)
        .filter(BackupGroup.superseded_at.is_(None), BackupGroup.removed_from_pbs_at.is_(None))
        .all()
    )
    evaluations, _copies = evaluate_offsite(db, all_current, now)
    wanted = {g.id for g in groups} if groups is not None else None

    assignments = {a.datastore_id: a for a in db.query(DatastoreCustomerAssignment).all()}
    not_applicable = {
        row.customer_id
        for row in db.query(ThreeTwoOneOneZeroLegSetting).filter_by(leg=OFFSITE_LEG, applicable=False).all()
    }
    exceptions = current_exceptions(db, now)

    gaps: list[OffsiteGap] = []
    for evaluation in evaluations:
        group = evaluation.group
        if wanted is not None and group.id not in wanted:
            continue
        customer_id = effective_group_customer_id(group, assignments.get(group.datastore_id))
        if visible is not None and customer_id not in visible:
            continue
        raw_status = evaluation.status
        exception = None
        if customer_id is not None and customer_id in not_applicable:
            evaluation.status = OffsiteStatus.NOT_REQUIRED
        elif raw_status in GAP_STATUSES:
            exception = _first(exceptions, group_exception_keys(group))
            if exception is not None:
                evaluation.status = OffsiteStatus.EXCEPTED
        gaps.append(
            OffsiteGap(evaluation=evaluation, customer_id=customer_id, raw_status=raw_status, exception=exception)
        )
    return gaps
