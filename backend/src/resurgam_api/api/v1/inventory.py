import re
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from resurgam_api.api.v1.datastores import get_datastore_assignment, get_datastore_in_scope_or_404
from resurgam_api.auth.dependency import get_current_user, require_role
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.scope import effective_group_customer_id, visible_customer_ids
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.inventory.file_index_defaults import default_file_index_exclude_patterns
from resurgam_api.inventory.protection import evaluate_protection_status
from resurgam_api.inventory.usage import compute_capacity_forecast, compute_usage_changes, load_daily_peaks
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.datastore import Datastore
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.inventory import BackupGroup, BackupSnapshot, ProtectionPolicy, TaskRun
from resurgam_api.models.pbs import PbsInstance
from resurgam_api.models.pve import PveCluster
from resurgam_api.models.sync import SyncJob
from resurgam_api.models.tenancy import Customer
from resurgam_api.models.usage import UsageMeasurement
from resurgam_api.schemas.customer import AssignGroupCustomerRequest
from resurgam_api.schemas.inventory import (
    BackupGroupOut,
    BackupSnapshotOut,
    FileIndexDefaultExcludePatternsOut,
    SetFileIndexExcludePatternsRequest,
    SetFileIndexMethodRequest,
    SyncJobOut,
    TaskRunOut,
)
from resurgam_api.schemas.protection_policy import AssignProtectionPolicyRequest
from resurgam_api.schemas.usage import CapacityForecastOut, DatastoreUsageOut, UsageMeasurementOut

router = APIRouter(tags=["inventory"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)


def _record_audit(db: Session, request: Request, *, actor_user_id, action: str, target_id, detail=None) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            target_type="backup_group",
            target_id=str(target_id),
            correlation_id=getattr(request.state, "correlation_id", None),
            detail=detail,
        )
    )


def group_encryption(db: Session, group_id: uuid.UUID) -> dict:
    """Encryption facts for one backup group from its synced snapshots (ADR 0031)."""
    newest_two = (
        db.query(BackupSnapshot.crypt_mode, BackupSnapshot.key_fingerprint)
        .filter(BackupSnapshot.backup_group_id == group_id)
        .order_by(BackupSnapshot.snapshot_time.desc())
        .limit(2)
        .all()
    )
    fingerprint_rows = (
        db.query(BackupSnapshot.key_fingerprint, func.min(BackupSnapshot.snapshot_time))
        .filter(BackupSnapshot.backup_group_id == group_id, BackupSnapshot.key_fingerprint.isnot(None))
        .group_by(BackupSnapshot.key_fingerprint)
        .order_by(func.min(BackupSnapshot.snapshot_time))
        .all()
    )
    latest = newest_two[0] if newest_two else (None, None)
    key_changed = (
        len(newest_two) == 2
        and None not in (newest_two[0][0], newest_two[1][0])
        and newest_two[0][1] != newest_two[1][1]
    )
    return {
        "latest_crypt_mode": latest[0],
        "latest_key_fingerprint": latest[1],
        "key_fingerprints": [row[0] for row in fingerprint_rows],
        "key_changed": key_changed,
    }


def to_backup_group_out(db: Session, group: BackupGroup) -> BackupGroupOut:
    policy = db.get(ProtectionPolicy, group.protection_policy_id) if group.protection_policy_id else None
    snapshot_count = db.query(BackupSnapshot).filter_by(backup_group_id=group.id).count()
    status = evaluate_protection_status(policy, group, snapshot_count)
    assignment = get_datastore_assignment(db, group.datastore_id)
    return BackupGroupOut(
        id=group.id,
        datastore_id=group.datastore_id,
        namespace=group.namespace,
        backup_type=group.backup_type,
        backup_id=group.backup_id,
        resolved_name=group.resolved_name,
        owner=group.owner,
        last_backup_at=group.last_backup_at,
        last_verify_status=group.last_verify_status,
        protection_policy_id=group.protection_policy_id,
        protection_status=status.value,
        customer_id=group.customer_id,
        effective_customer_id=effective_group_customer_id(group, assignment),
        file_index_enabled=group.file_index_enabled,
        file_index_exclude_patterns=group.file_index_exclude_patterns,
        file_index_method=group.file_index_method,
        guest_exec_live_pve_cluster_id=group.guest_exec_live_pve_cluster_id,
        last_synced_at=group.last_synced_at,
        superseded_at=group.superseded_at,
        removed_from_pbs_at=group.removed_from_pbs_at,
        **group_encryption(db, group.id),
    )


@router.get("/datastores/{datastore_id}/backup-groups", response_model=list[BackupGroupOut])
def list_backup_groups(
    datastore_id: uuid.UUID,
    include_superseded: bool = Query(
        default=False,
        description=(
            "When true, also include a reused VMID's closed-out prior generation(s) "
            "(BackupGroup.superseded_at set) -- read-only history, not part of the "
            "default listing."
        ),
    ),
    include_removed: bool = Query(
        default=False,
        description=(
            "When true, also include groups the Collector's most recent sync no longer "
            "found on PBS at all (BackupGroup.removed_from_pbs_at set) -- read-only "
            "history, not part of the default listing."
        ),
    ),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[BackupGroupOut]:
    """Customer-scope filtered, but not simply "is the datastore in scope"
    like most other datastore-scoped endpoints — a shared datastore can
    hold groups individually overridden to a different customer than the
    datastore's own assignment (not from the manifest, added for
    billing), so scope is resolved per group and only groups the caller
    can actually see are returned. 404s (not empty list) only when
    *nothing* in this datastore — neither the datastore's own assignment
    nor any individual group override — is visible to the caller, so
    existence still isn't confirmed for a fully out-of-scope datastore.
    """
    datastore = db.get(Datastore, datastore_id)
    if datastore is None:
        raise ResurgamApiError(code="not_found", message="Datastore not found.", status_code=404)

    query = db.query(BackupGroup).filter_by(datastore_id=datastore_id)
    if not include_superseded:
        query = query.filter(BackupGroup.superseded_at.is_(None))
    if not include_removed:
        query = query.filter(BackupGroup.removed_from_pbs_at.is_(None))
    groups = query.order_by(BackupGroup.backup_type, BackupGroup.backup_id).all()

    visible = visible_customer_ids(user)
    if visible is None:
        return [to_backup_group_out(db, g) for g in groups]

    assignment = get_datastore_assignment(db, datastore_id)
    visible_groups = [g for g in groups if effective_group_customer_id(g, assignment) in visible]
    datastore_itself_visible = assignment is not None and assignment.customer_id in visible
    if not visible_groups and not datastore_itself_visible:
        raise ResurgamApiError(code="not_found", message="Datastore not found.", status_code=404)
    return [to_backup_group_out(db, g) for g in visible_groups]


@router.get("/backup-groups/{group_id}/snapshots", response_model=list[BackupSnapshotOut])
def list_backup_group_snapshots(
    group_id: uuid.UUID, db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> list[BackupSnapshotOut]:
    group = db.get(BackupGroup, group_id)
    if group is None:
        raise ResurgamApiError(code="not_found", message="Backup group not found.", status_code=404)

    visible = visible_customer_ids(user)
    if visible is not None:
        assignment = get_datastore_assignment(db, group.datastore_id)
        if effective_group_customer_id(group, assignment) not in visible:
            # 404, not 403: don't confirm existence of an out-of-scope object.
            raise ResurgamApiError(code="not_found", message="Backup group not found.", status_code=404)

    snapshots = (
        db.query(BackupSnapshot).filter_by(backup_group_id=group_id).order_by(BackupSnapshot.snapshot_time).all()
    )
    return [BackupSnapshotOut.model_validate(s) for s in snapshots]


@router.post("/backup-groups/{group_id}/assign-customer", response_model=BackupGroupOut)
def assign_group_customer(
    group_id: uuid.UUID,
    body: AssignGroupCustomerRequest,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(_require_platform_admin),
) -> BackupGroupOut:
    """Override which customer this specific group belongs to — for a
    shared datastore that holds VMs for more than one customer. Not from
    the manifest; added for billing (manifest §9.3 discussion). Does not
    require the override to differ from the datastore's own assignment;
    setting it equal is harmless, just redundant.
    """
    group = db.get(BackupGroup, group_id)
    if group is None:
        raise ResurgamApiError(code="not_found", message="Backup group not found.", status_code=404)
    if db.get(Customer, body.customer_id) is None:
        raise ResurgamApiError(code="customer_not_found", message="Customer not found.", status_code=400)

    group.customer_id = body.customer_id
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="backup_group_customer_assigned",
        target_id=group_id,
        detail={"customer_id": str(body.customer_id)},
    )
    db.commit()
    db.refresh(group)
    return to_backup_group_out(db, group)


@router.post("/backup-groups/{group_id}/unassign-customer", response_model=BackupGroupOut)
def unassign_group_customer(
    group_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user=Depends(_require_platform_admin)
) -> BackupGroupOut:
    group = db.get(BackupGroup, group_id)
    if group is None:
        raise ResurgamApiError(code="not_found", message="Backup group not found.", status_code=404)

    group.customer_id = None
    _record_audit(
        db, request, actor_user_id=user.user_id, action="backup_group_customer_unassigned", target_id=group_id
    )
    db.commit()
    db.refresh(group)
    return to_backup_group_out(db, group)


@router.post("/backup-groups/{group_id}/assign-policy", response_model=BackupGroupOut)
def assign_protection_policy(
    group_id: uuid.UUID,
    body: AssignProtectionPolicyRequest,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(_require_platform_admin),
) -> BackupGroupOut:
    group = db.get(BackupGroup, group_id)
    if group is None:
        raise ResurgamApiError(code="not_found", message="Backup group not found.", status_code=404)

    policy = db.get(ProtectionPolicy, body.protection_policy_id)
    if policy is None:
        raise ResurgamApiError(
            code="protection_policy_not_found", message="Protection policy not found.", status_code=400
        )

    assignment = get_datastore_assignment(db, group.datastore_id)
    effective_customer = effective_group_customer_id(group, assignment)
    if effective_customer is None or effective_customer != policy.customer_id:
        raise ResurgamApiError(
            code="protection_policy_customer_mismatch",
            message="This policy belongs to a different customer than the backup group's assigned datastore.",
            status_code=409,
        )

    group.protection_policy_id = policy.id
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="protection_policy_assigned",
        target_id=group_id,
        detail={"protection_policy_id": str(policy.id)},
    )
    db.commit()
    db.refresh(group)
    return to_backup_group_out(db, group)


@router.post("/backup-groups/{group_id}/unassign-policy", response_model=BackupGroupOut)
def unassign_protection_policy(
    group_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user=Depends(_require_platform_admin)
) -> BackupGroupOut:
    group = db.get(BackupGroup, group_id)
    if group is None:
        raise ResurgamApiError(code="not_found", message="Backup group not found.", status_code=404)

    group.protection_policy_id = None
    _record_audit(db, request, actor_user_id=user.user_id, action="protection_policy_unassigned", target_id=group_id)
    db.commit()
    db.refresh(group)
    return to_backup_group_out(db, group)


@router.post("/backup-groups/{group_id}/enable-file-index", response_model=BackupGroupOut)
def enable_backup_group_file_index(
    group_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user=Depends(_require_platform_admin)
) -> BackupGroupOut:
    """Manifest §13 (ADR 0016) per-VM opt-in -- a group is actually
    indexed only once its effective customer's
    `customer_file_index_settings` also exists and is enabled; this flag
    alone doesn't start anything on its own.
    """
    group = db.get(BackupGroup, group_id)
    if group is None:
        raise ResurgamApiError(code="not_found", message="Backup group not found.", status_code=404)

    group.file_index_enabled = True
    _record_audit(db, request, actor_user_id=user.user_id, action="backup_group_file_index_enabled", target_id=group_id)
    db.commit()
    db.refresh(group)
    return to_backup_group_out(db, group)


@router.post("/backup-groups/{group_id}/disable-file-index", response_model=BackupGroupOut)
def disable_backup_group_file_index(
    group_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user=Depends(_require_platform_admin)
) -> BackupGroupOut:
    group = db.get(BackupGroup, group_id)
    if group is None:
        raise ResurgamApiError(code="not_found", message="Backup group not found.", status_code=404)

    group.file_index_enabled = False
    _record_audit(
        db, request, actor_user_id=user.user_id, action="backup_group_file_index_disabled", target_id=group_id
    )
    db.commit()
    db.refresh(group)
    return to_backup_group_out(db, group)


@router.get("/file-index-default-exclude-patterns", response_model=FileIndexDefaultExcludePatternsOut)
def get_file_index_default_exclude_patterns(
    _user: AuthenticatedUser = Depends(get_current_user),
) -> FileIndexDefaultExcludePatternsOut:
    """The regex patterns a newly synced backup group's
    `file_index_exclude_patterns` starts with (see
    `models.inventory.BackupGroup`'s column default). Read-only, open to
    any authenticated user -- lets the frontend offer "reapply the
    default list" on a group whose patterns were cleared or customized,
    without hardcoding a second copy of the list.
    """
    return FileIndexDefaultExcludePatternsOut(patterns=default_file_index_exclude_patterns())


@router.post("/backup-groups/{group_id}/file-index-exclude-patterns", response_model=BackupGroupOut)
def set_backup_group_file_index_exclude_patterns(
    group_id: uuid.UUID,
    body: SetFileIndexExcludePatternsRequest,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(_require_platform_admin),
) -> BackupGroupOut:
    """Manifest §13 (ADR 0016) per-VM regex exclusions, raised directly by
    the user after watching a real VM's index job spend most of its time
    walking an irrelevant kernel-headers tree. Replaces the whole list
    (not a partial add/remove) -- matches this API's existing
    enable/disable-style convention of simple, whole-value operations.
    Rejected upfront if any pattern doesn't compile, rather than silently
    ignored later by the worker.
    """
    group = db.get(BackupGroup, group_id)
    if group is None:
        raise ResurgamApiError(code="not_found", message="Backup group not found.", status_code=404)

    for pattern in body.patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ResurgamApiError(
                code="invalid_pattern", message=f"Invalid regex pattern {pattern!r}: {exc}", status_code=400
            ) from exc

    group.file_index_exclude_patterns = body.patterns
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="backup_group_file_index_exclude_patterns_set",
        target_id=group_id,
    )
    db.commit()
    db.refresh(group)
    return to_backup_group_out(db, group)


@router.post("/backup-groups/{group_id}/file-index-method", response_model=BackupGroupOut)
def set_backup_group_file_index_method(
    group_id: uuid.UUID,
    body: SetFileIndexMethodRequest,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(_require_platform_admin),
) -> BackupGroupOut:
    """Guest-exec File Indexing (ADR 0021) -- how this group is indexed,
    alongside the existing enable/disable + exclude-patterns endpoints.
    'guest_exec_ephemeral' needs the customer's
    `CustomerFileIndexSettings.guest_exec_pve_cluster_id`/
    `guest_exec_target_node`/`guest_exec_target_storage` configured
    before any job of this method can actually run (validated at
    job-creation time, `api/v1/file_index.py::_build_file_index_job`,
    not here -- this endpoint only sets the group's own choice).
    'guest_exec_live' instead needs `guest_exec_live_pve_cluster_id`
    (the cluster hosting the *production* VM, distinct from any lab-
    compute cluster) -- required here, at method-set time, since that
    method is never manually triggered and so has no other checkpoint to
    validate it before a real backup's hookscript trigger
    (`POST /lab-agent/backup-completed`) starts relying on it to scope
    which agent may report for this group.
    """
    group = db.get(BackupGroup, group_id)
    if group is None:
        raise ResurgamApiError(code="not_found", message="Backup group not found.", status_code=404)

    cluster_id = body.guest_exec_live_pve_cluster_id or group.guest_exec_live_pve_cluster_id
    if body.method == "guest_exec_live":
        if cluster_id is None:
            raise ResurgamApiError(
                code="guest_exec_live_cluster_required",
                message="guest_exec_live_pve_cluster_id is required to select the guest_exec_live method.",
                status_code=400,
            )
        if db.get(PveCluster, cluster_id) is None:
            raise ResurgamApiError(code="not_found", message="PVE cluster not found.", status_code=404)
        group.guest_exec_live_pve_cluster_id = cluster_id
    elif body.guest_exec_live_pve_cluster_id is not None:
        group.guest_exec_live_pve_cluster_id = body.guest_exec_live_pve_cluster_id

    group.file_index_method = body.method
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="backup_group_file_index_method_set",
        target_id=group_id,
        detail={"method": body.method, "guest_exec_live_pve_cluster_id": str(cluster_id) if cluster_id else None},
    )
    db.commit()
    db.refresh(group)
    return to_backup_group_out(db, group)


@router.get("/pbs-instances/{instance_id}/tasks", response_model=list[TaskRunOut])
def list_pbs_instance_tasks(
    instance_id: uuid.UUID,
    task_types: list[str] | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=5000),
    db: Session = Depends(get_db),
    _user=Depends(_require_platform_admin),
) -> list[TaskRunOut]:
    """Task history for one PBS instance, most recent first.

    `task_types`/`status` filter *before* `limit` is applied, not after —
    the frontend's four Jobs views each want the most recent N tasks of
    one type (backup/verify/prune/sync), and a less frequent type (e.g.
    verification) can otherwise be starved out entirely by a more
    frequent one (backup) sharing the same global recency cutoff. Task
    history is an unbounded, ever-growing log (a real instance has had
    1000+ tasks within weeks), so `limit` defaults to a bounded window
    rather than returning everything on every call.
    """
    if db.get(PbsInstance, instance_id) is None:
        raise ResurgamApiError(code="not_found", message="PBS instance not found.", status_code=404)

    query = db.query(TaskRun).filter_by(pbs_instance_id=instance_id)
    if task_types:
        query = query.filter(TaskRun.task_type.in_(task_types))
    if status:
        query = query.filter(TaskRun.status == status)
    tasks = query.order_by(TaskRun.started_at.desc()).limit(limit).all()
    return [TaskRunOut.model_validate(t) for t in tasks]


@router.get("/tasks", response_model=list[TaskRunOut])
def list_tasks(
    pbs_instance_ids: list[uuid.UUID] = Query(..., min_length=1),
    task_types: list[str] | None = Query(default=None),
    status: str | None = Query(default=None),
    started_after: datetime | None = Query(default=None),
    started_before: datetime | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=5000),
    db: Session = Depends(get_db),
    _user=Depends(_require_platform_admin),
) -> list[TaskRunOut]:
    """Task history across *several* PBS instances at once, most recent
    first — the cross-instance counterpart to `list_pbs_instance_tasks`
    below (left as-is; nothing here depends on removing it). `task_types`/
    `status`/`started_after`/`started_before` all filter *before* `limit`,
    same discipline as the single-instance endpoint's `task_types`
    handling — a less-frequent type or an older date range must not be
    starved out by a global recency cutoff shared with a much larger
    instance/type. `started_after`/`started_before` apply to `started_at`
    only (not `ended_at`); `started_before` is inclusive.
    """
    query = db.query(TaskRun).filter(TaskRun.pbs_instance_id.in_(pbs_instance_ids))
    if task_types:
        query = query.filter(TaskRun.task_type.in_(task_types))
    if status:
        query = query.filter(TaskRun.status == status)
    if started_after is not None:
        query = query.filter(TaskRun.started_at >= started_after)
    if started_before is not None:
        query = query.filter(TaskRun.started_at <= started_before)
    tasks = query.order_by(TaskRun.started_at.desc()).limit(limit).all()
    return [TaskRunOut.model_validate(t) for t in tasks]


@router.get("/pbs-instances/{instance_id}/sync-jobs", response_model=list[SyncJobOut])
def list_pbs_instance_sync_jobs(
    instance_id: uuid.UUID, db: Session = Depends(get_db), _user=Depends(_require_platform_admin)
) -> list[SyncJobOut]:
    """Manifest §8's `sync_jobs` — configured PBS Sync Jobs (DR/replication
    posture), refreshed by the Collector alongside task history and
    inventory. Field shapes are unverified against a real PBS (see
    connectivity.pbs_api.list_sync_jobs's docstring).
    """
    if db.get(PbsInstance, instance_id) is None:
        raise ResurgamApiError(code="not_found", message="PBS instance not found.", status_code=404)

    jobs = db.query(SyncJob).filter_by(pbs_instance_id=instance_id).order_by(SyncJob.job_id).all()
    return [SyncJobOut.model_validate(j) for j in jobs]


def to_usage_measurement_out(measurement: UsageMeasurement) -> UsageMeasurementOut:
    usage_percent = (measurement.used_bytes / measurement.total_bytes * 100) if measurement.total_bytes else 0.0
    return UsageMeasurementOut(
        id=measurement.id,
        datastore_id=measurement.datastore_id,
        captured_at=measurement.captured_at,
        used_bytes=measurement.used_bytes,
        available_bytes=measurement.available_bytes,
        total_bytes=measurement.total_bytes,
        logical_bytes=measurement.logical_bytes,
        usage_percent=round(usage_percent, 2),
        group_count_vm=measurement.group_count_vm,
        group_count_ct=measurement.group_count_ct,
        group_count_host=measurement.group_count_host,
        snapshot_count=measurement.snapshot_count,
        oldest_snapshot_at=measurement.oldest_snapshot_at,
        newest_snapshot_at=measurement.newest_snapshot_at,
        last_successful_gc_at=measurement.last_successful_gc_at,
    )


@router.get("/datastores/{datastore_id}/usage", response_model=DatastoreUsageOut)
def get_datastore_usage(
    datastore_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> DatastoreUsageOut:
    """Manifest §9.3: capacity history for one datastore. Same customer
    scope as `/datastores/{id}` itself — reuses the same 404-not-403 check.
    """
    get_datastore_in_scope_or_404(db, datastore_id, user)

    now = datetime.now(UTC)
    # Newest 500, not oldest 500: at a 15-minute sync cadence 500 rows are
    # about five days, and ascending-then-limit froze `latest` there.
    history = list(
        reversed(
            db.query(UsageMeasurement)
            .filter_by(datastore_id=datastore_id)
            .order_by(UsageMeasurement.captured_at.desc())
            .limit(500)
            .all()
        )
    )
    latest = history[-1] if history else None

    # The 24 h / 30 d baselines are looked up on their own -- the 500 rows
    # above rarely reach back 30 days.
    def closest_before(cutoff: datetime) -> UsageMeasurement | None:
        return (
            db.query(UsageMeasurement)
            .filter(UsageMeasurement.datastore_id == datastore_id, UsageMeasurement.captured_at <= cutoff)
            .order_by(UsageMeasurement.captured_at.desc())
            .first()
        )

    points: list[UsageMeasurement] = []
    if latest is not None:
        baselines = {closest_before(now - timedelta(days=30)), closest_before(now - timedelta(hours=24))}
        points = sorted((b for b in baselines if b is not None and b.id != latest.id), key=lambda m: m.captured_at)
        points.append(latest)
    daily_change, monthly_change = compute_usage_changes(points, now)

    forecast = compute_capacity_forecast(
        load_daily_peaks(db, datastore_id, now), latest.available_bytes if latest else None, now
    )

    return DatastoreUsageOut(
        latest=to_usage_measurement_out(latest) if latest else None,
        history=[to_usage_measurement_out(m) for m in history],
        daily_change_bytes=daily_change,
        monthly_change_bytes=monthly_change,
        forecast=CapacityForecastOut(
            status=forecast.status.value,
            basis_days=forecast.basis_days,
            growth_bytes_per_day=forecast.growth_bytes_per_day,
            days_until_full=forecast.days_until_full,
            full_at=forecast.full_at,
        ),
    )
