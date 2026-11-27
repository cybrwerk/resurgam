"""3-2-1 gaps (ADR 0032): PVE guests no enabled backup job covers, the offsite
copy status of every backup group, and the audited exceptions that accept a
gap."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from resurgam_api.auth.dependency import get_current_user, require_role
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.scope import visible_customer_ids
from resurgam_api.collector.backup_gaps import check_cluster_backup_gaps
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.instance_settings import get_or_create_instance_settings
from resurgam_api.inventory.backup_gaps import (
    OffsiteGap,
    compute_guest_gaps,
    compute_offsite_gaps,
)
from resurgam_api.inventory.offsite import lag_thresholds, offsite_severity
from resurgam_api.inventory.workload_label import workload_label
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.backup_gaps import BackupGapException, GapExceptionKind, PveGuestBackupGap
from resurgam_api.models.datastore import Datastore
from resurgam_api.models.identity import RoleKey, User
from resurgam_api.models.pbs import PbsInstance
from resurgam_api.models.pve import PveCluster
from resurgam_api.models.tenancy import Customer
from resurgam_api.schemas.backup_gaps import (
    BackupGapsOut,
    BackupGapSummary,
    BackupGapThresholds,
    CreateGapExceptionRequest,
    GapExceptionOut,
    GuestGapOut,
    OffsiteGapOut,
    PveClusterGapCheckOut,
)

router = APIRouter(prefix="/backup-gaps", tags=["backup-gaps"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)


class _Names:
    """Display names for everything a gap or exception refers to, loaded once."""

    def __init__(self, db: Session):
        self.clusters = {c.id: c.display_name for c in db.query(PveCluster).all()}
        self.instances = {i.id: i.display_name for i in db.query(PbsInstance).all()}
        self.datastores = {d.id: d for d in db.query(Datastore).all()}
        self.customers = {c.id: c.name for c in db.query(Customer).all()}
        self.guests = {(g.pve_cluster_id, g.vmid): g.name for g in db.query(PveGuestBackupGap).all()}
        self.db = db
        self._users: dict[uuid.UUID, str | None] = {}

    def user(self, user_id: uuid.UUID) -> str | None:
        if user_id not in self._users:
            user = self.db.get(User, user_id)
            self._users[user_id] = user.display_name if user else None
        return self._users[user_id]

    def datastore(self, datastore_id: uuid.UUID | None) -> str:
        datastore = self.datastores.get(datastore_id)
        if datastore is None:
            return f"removed datastore {datastore_id}"
        return f"{self.instances.get(datastore.pbs_instance_id, '?')} / {datastore.name}"

    def exception_target(self, row: BackupGapException) -> str:
        cluster = self.clusters.get(row.pve_cluster_id, f"removed cluster {row.pve_cluster_id}")
        if row.target_kind == GapExceptionKind.PVE_GUEST.value:
            name = self.guests.get((row.pve_cluster_id, row.vmid))
            return f"{cluster} / {row.vmid}" + (f" ({name})" if name else "")
        if row.target_kind == GapExceptionKind.PVE_CLUSTER.value:
            return f"{cluster} (all guests)"
        if row.target_kind == GapExceptionKind.BACKUP_GROUP.value:
            ns = f"{row.namespace}/" if row.namespace else ""
            return f"{self.datastore(row.datastore_id)} / {ns}{row.backup_type}/{row.backup_id}"
        return f"{self.datastore(row.datastore_id)} (all backup groups)"


def _exception_out(row: BackupGapException | None, names: _Names) -> GapExceptionOut | None:
    if row is None:
        return None
    return GapExceptionOut(
        id=row.id,
        target_kind=row.target_kind,
        target_label=names.exception_target(row),
        pve_cluster_id=row.pve_cluster_id,
        vmid=row.vmid,
        datastore_id=row.datastore_id,
        namespace=row.namespace,
        backup_type=row.backup_type,
        backup_id=row.backup_id,
        active=row.active,
        reason=row.reason,
        expires_at=row.expires_at,
        created_by_user_id=row.created_by_user_id,
        created_by_name=names.user(row.created_by_user_id),
        created_at=row.created_at,
    )


def _offsite_out(gap: OffsiteGap, names: _Names, warning: int, critical: int) -> OffsiteGapOut:
    e = gap.evaluation
    group = e.group
    datastore = names.datastores.get(group.datastore_id)
    return OffsiteGapOut(
        backup_group_id=group.id,
        workload_label=workload_label(group),
        backup_type=group.backup_type,
        backup_id=group.backup_id,
        namespace=group.namespace or "",
        datastore_id=group.datastore_id,
        datastore_name=datastore.name if datastore else "?",
        pbs_instance_name=names.instances.get(datastore.pbs_instance_id, "?") if datastore else "?",
        customer_id=gap.customer_id,
        customer_name=names.customers.get(gap.customer_id) if gap.customer_id else None,
        status=e.status.value,
        raw_status=gap.raw_status.value,
        severity=offsite_severity(e.status, e.lag_hours, warning, critical, dr_required=e.dr_required),
        copy_location=e.copy_label,
        copy_backup_group_id=e.copy_backup_group_id,
        sync_jobs=e.jobs,
        latest_backup_at=e.latest_backup_at,
        latest_copy_at=e.latest_copy_at,
        oldest_uncopied_at=e.oldest_uncopied_at,
        uncopied_count=e.uncopied_count,
        lag_hours=round(e.lag_hours, 1) if e.lag_hours is not None else None,
        exception=_exception_out(gap.exception, names),
    )


def build_backup_gaps(db: Session, visible: set[uuid.UUID] | None, now: datetime | None = None) -> BackupGapsOut:
    """The whole picture for one caller. `visible=None` = platform admin;
    PVE guests have no customer, so only platform admins see them."""
    now = now or datetime.now(UTC)
    names = _Names(db)
    warning, critical = lag_thresholds(db)

    guests: list[GuestGapOut] = []
    clusters: list[PveClusterGapCheckOut] = []
    if visible is None:
        for item in compute_guest_gaps(db, now):
            guests.append(
                GuestGapOut(
                    pve_cluster_id=item.gap.pve_cluster_id,
                    pve_cluster_name=names.clusters.get(item.gap.pve_cluster_id, "?"),
                    vmid=item.gap.vmid,
                    guest_type=item.gap.guest_type,
                    name=item.gap.name,
                    reason=item.gap.reason,
                    first_seen_at=item.gap.first_seen_at,
                    last_seen_at=item.gap.last_seen_at,
                    exception=_exception_out(item.exception, names),
                )
            )
        clusters = [
            PveClusterGapCheckOut(
                pve_cluster_id=c.id,
                pve_cluster_name=c.display_name,
                checked_at=c.backup_gaps_checked_at,
                error=c.backup_gaps_error,
            )
            for c in db.query(PveCluster).filter(PveCluster.disabled.is_(False)).order_by(PveCluster.display_name)
        ]

    offsite = [_offsite_out(gap, names, warning, critical) for gap in compute_offsite_gaps(db, visible, now)]
    offsite.sort(key=lambda o: ({"critical": 0, "warning": 1}.get(o.severity or "", 2), o.workload_label.lower()))

    # Excepted and not-required groups don't count towards the lag.
    lags = [o.lag_hours for o in offsite if o.lag_hours is not None and o.status in ("ok", "lagging", "missing")]
    summary = BackupGapSummary(
        unprotected_guests=sum(1 for g in guests if g.exception is None),
        offsite_gaps=sum(1 for o in offsite if o.severity is not None),
        offsite_critical=sum(1 for o in offsite if o.severity == "critical"),
        offsite_max_lag_hours=max(lags) if lags else None,
        offsite_without_copy_job=sum(1 for o in offsite if o.status == "no_copy_job"),
        excepted=sum(1 for g in guests if g.exception is not None) + sum(1 for o in offsite if o.status == "excepted"),
    )
    return BackupGapsOut(
        summary=summary,
        thresholds=BackupGapThresholds(offsite_lag_warning_hours=warning, offsite_lag_critical_hours=critical),
        pve_clusters=clusters,
        unprotected_guests=guests,
        offsite=offsite,
    )


@router.get("", response_model=BackupGapsOut)
def get_backup_gaps(
    db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> BackupGapsOut:
    """Everything the "3-2-1 Gaps" page shows: unprotected PVE guests (platform
    admins only -- guests have no customer), the offsite copy status of every
    backup group the caller may see, the counts and the lag thresholds.
    Computed from data the collector already synced; no PBS/PVE call."""
    return build_backup_gaps(db, visible_customer_ids(user))


@router.get("/exceptions", response_model=list[GapExceptionOut])
def list_gap_exceptions(
    db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> list[GapExceptionOut]:
    """Full exception history, newest first -- every exception and revocation
    ever recorded (the table is append-only). Platform admins and global
    auditors only; everyone else sees the active exception on each gap."""
    if not (user.has_role(RoleKey.PLATFORM_ADMIN) or user.has_role(RoleKey.AUDITOR)):
        raise ResurgamApiError(
            code="forbidden",
            message="Only platform admins and auditors can read the exception history.",
            status_code=403,
        )
    names = _Names(db)
    rows = db.query(BackupGapException).order_by(BackupGapException.created_at.desc()).all()
    return [_exception_out(row, names) for row in rows]


@router.post("/exceptions", response_model=GapExceptionOut, status_code=201)
def create_gap_exception(
    body: CreateGapExceptionRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(_require_platform_admin),
) -> GapExceptionOut:
    """Accept a gap with a reason (`active: true`) or revoke the current
    exception for a target (`active: false`). `platform_admin` only. Never
    changes an earlier row: each call appends one, and writes an audit event.
    The target must exist when an exception is recorded; revoking also works
    for a target that is gone."""
    now = datetime.now(UTC)
    if body.expires_at is not None and body.expires_at <= now:
        raise ResurgamApiError(code="invalid_expiry", message="expires_at must be in the future.", status_code=400)
    if body.active:
        if body.pve_cluster_id is not None and db.get(PveCluster, body.pve_cluster_id) is None:
            raise ResurgamApiError(code="not_found", message="PVE cluster not found.", status_code=404)
        if body.datastore_id is not None and db.get(Datastore, body.datastore_id) is None:
            raise ResurgamApiError(code="not_found", message="Datastore not found.", status_code=404)

    kind = body.target_kind
    row = BackupGapException(
        target_kind=kind,
        pve_cluster_id=body.pve_cluster_id if kind in ("pve_guest", "pve_cluster") else None,
        vmid=body.vmid if kind == "pve_guest" else None,
        datastore_id=body.datastore_id if kind in ("backup_group", "datastore") else None,
        namespace=(body.namespace or "") if kind == "backup_group" else None,
        backup_type=body.backup_type if kind == "backup_group" else None,
        backup_id=body.backup_id if kind == "backup_group" else None,
        active=body.active,
        reason=body.reason.strip(),
        expires_at=body.expires_at,
        created_by_user_id=user.user_id,
        # Explicit, not the server default: Postgres `now()` is the transaction
        # start, so two rows in one transaction would tie and "newest wins"
        # would be arbitrary.
        created_at=now,
    )
    db.add(row)
    db.flush()
    names = _Names(db)
    db.add(
        AuditEvent(
            actor_user_id=user.user_id,
            action="backup_gap_exception_created" if body.active else "backup_gap_exception_revoked",
            target_type="backup_gap_exception",
            target_id=row.id,
            correlation_id=getattr(request.state, "correlation_id", None),
            detail={
                "target_kind": kind,
                "target": names.exception_target(row),
                "reason": row.reason,
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            },
        )
    )
    db.commit()
    db.refresh(row)
    return _exception_out(row, names)


@router.put("/thresholds", response_model=BackupGapThresholds)
def update_gap_thresholds(
    body: BackupGapThresholds,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(_require_platform_admin),
) -> BackupGapThresholds:
    """Set the offsite lag thresholds (hours). `platform_admin` only; audited."""
    settings = get_or_create_instance_settings(db)
    settings.offsite_lag_warning_hours = body.offsite_lag_warning_hours
    settings.offsite_lag_critical_hours = body.offsite_lag_critical_hours
    db.add(
        AuditEvent(
            actor_user_id=user.user_id,
            action="backup_gap_thresholds_updated",
            target_type="instance_settings",
            target_id=None,
            correlation_id=getattr(request.state, "correlation_id", None),
            detail=body.model_dump(),
        )
    )
    db.commit()
    return body


@router.post("/pve-clusters/{pve_cluster_id}/check", response_model=PveClusterGapCheckOut)
def check_pve_cluster_now(
    pve_cluster_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(_require_platform_admin),
) -> PveClusterGapCheckOut:
    """Check one PVE cluster for unprotected guests right now instead of
    waiting for the collector (reads PVE's backup jobs, writes nothing to
    PVE). `platform_admin` only. A failed check returns 200 with `error` set;
    the previous result stays."""
    cluster = db.get(PveCluster, pve_cluster_id)
    if cluster is None:
        raise ResurgamApiError(code="not_found", message="PVE cluster not found.", status_code=404)
    check_cluster_backup_gaps(db, settings, cluster)
    db.commit()
    return PveClusterGapCheckOut(
        pve_cluster_id=cluster.id,
        pve_cluster_name=cluster.display_name,
        checked_at=cluster.backup_gaps_checked_at,
        error=cluster.backup_gaps_error,
    )
