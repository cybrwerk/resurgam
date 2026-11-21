import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import case, exists
from sqlalchemy.orm import Session

from resurgam_api.api.v1._connection_helpers import upstream_error
from resurgam_api.api.v1.customers import get_customer_in_scope_or_404
from resurgam_api.api.v1.restore_test_plans import _cluster_trust_and_token
from resurgam_api.auth.dependency import get_current_user, require_role
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.scope import effective_group_customer_id, require_restore_access, visible_customer_ids
from resurgam_api.config import Settings, get_settings
from resurgam_api.connectivity.pve_api import next_free_vmid
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.inventory.workload_label import workload_label
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.customer_file_index_settings import CustomerFileIndexSettings
from resurgam_api.models.datastore import DatastoreCustomerAssignment
from resurgam_api.models.file_index_entry import FileIndexEntry
from resurgam_api.models.file_index_job import FileIndexJob
from resurgam_api.models.identity import RoleKey, ScopeType
from resurgam_api.models.inventory import BackupGroup, BackupSnapshot
from resurgam_api.models.pve import PveCluster
from resurgam_api.schemas.file_index import (
    BulkFileIndexJobResult,
    CustomerFileIndexSettingsCreateRequest,
    CustomerFileIndexSettingsOut,
    CustomerFileIndexSettingsUpdateRequest,
    FileIndexEntryOut,
    FileIndexJobOut,
    FileIndexSearchResponse,
    SnapshotIndexStatusOut,
)

router = APIRouter(tags=["file-index"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)

_NON_TERMINAL_JOB_STATUSES = ("pending", "running", "paused")


def _record_audit(
    db: Session,
    request: Request,
    *,
    actor_user_id,
    action: str,
    target_type: str,
    target_id,
    customer_id=None,
    detail=None,
) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            customer_id=customer_id,
            correlation_id=getattr(request.state, "correlation_id", None),
            detail=detail,
        )
    )


def _get_group_and_snapshot_or_404(
    db: Session, group_id: uuid.UUID, snapshot_id: uuid.UUID
) -> tuple[BackupGroup, BackupSnapshot]:
    group = db.get(BackupGroup, group_id)
    if group is None:
        raise ResurgamApiError(code="not_found", message="Backup group not found.", status_code=404)
    snapshot = db.get(BackupSnapshot, snapshot_id)
    if snapshot is None or snapshot.backup_group_id != group_id:
        raise ResurgamApiError(code="not_found", message="Snapshot not found.", status_code=404)
    return group, snapshot


def _resolve_customer_id(db: Session, group: BackupGroup) -> uuid.UUID | None:
    assignment = db.query(DatastoreCustomerAssignment).filter_by(datastore_id=group.datastore_id).one_or_none()
    return effective_group_customer_id(group, assignment)


def _require_indexing_enabled(
    db: Session, group: BackupGroup, customer_id: uuid.UUID | None
) -> CustomerFileIndexSettings:
    """Two-level AND gate (manifest §13, ADR 0016): the customer's own
    settings stay a fast "pause everything for this customer" kill
    switch, while `group.file_index_enabled` is the actual per-VM opt-in
    raised directly by the user in place of a customer-wide-only switch.
    """
    settings = (
        db.query(CustomerFileIndexSettings).filter_by(customer_id=customer_id).one_or_none()
        if customer_id is not None
        else None
    )
    if settings is None or not settings.enabled:
        raise ResurgamApiError(
            code="index_not_enabled",
            message="File indexing is not enabled for this customer.",
            status_code=409,
        )
    if not group.file_index_enabled:
        raise ResurgamApiError(
            code="index_not_enabled_for_group",
            message="File indexing is not enabled for this backup group.",
            status_code=409,
        )
    return settings


def _snapshot_has_in_flight_job(db: Session, snapshot_id: uuid.UUID) -> bool:
    return db.query(
        exists().where(
            FileIndexJob.backup_snapshot_id == snapshot_id,
            FileIndexJob.status.in_(_NON_TERMINAL_JOB_STATUSES),
        )
    ).scalar()


def _snapshot_has_live_index(db: Session, snapshot_id: uuid.UUID) -> bool:
    return db.query(exists().where(FileIndexEntry.backup_snapshot_id == snapshot_id)).scalar()


def _build_file_index_job(
    db: Session,
    settings: Settings,
    *,
    group: BackupGroup,
    snapshot: BackupSnapshot,
    customer_id: uuid.UUID | None,
    index_settings: CustomerFileIndexSettings | None,
    trigger: str,
    actor_user_id: uuid.UUID | None,
    correlation_id: str | None,
) -> FileIndexJob:
    """Guest-exec File Indexing (ADR 0021): `file_index_method` is
    snapshotted from the group here, not re-read later -- matches
    `RestoreTestPlan.customer_id`'s own "snapshot at creation" precedent,
    so the agent's dispatch and the UI's rendering never need a join
    back to `BackupGroup`. `guest_exec_ephemeral` allocates a real VMID
    for its disposable clone right now (same `next_free_vmid` call
    Recovery Lab plan creation already makes) -- `external_mount` and
    `guest_exec_live` need no such allocation (the latter targets an
    already-running production VM, set directly from the hookscript
    trigger payload, never from this endpoint).
    """
    method = group.file_index_method
    if method == "guest_exec_live":
        raise ResurgamApiError(
            code="guest_exec_live_not_manually_triggerable",
            message="This group indexes live at its own real backup's completion (a PVE hookscript), not on manual/bulk demand.",
            status_code=409,
        )

    target_vmid: int | None = None
    target_node: str | None = None
    if method == "guest_exec_ephemeral":
        if index_settings is None or not (
            index_settings.guest_exec_pve_cluster_id
            and index_settings.guest_exec_target_node
            and index_settings.guest_exec_target_storage
        ):
            raise ResurgamApiError(
                code="guest_exec_ephemeral_not_configured",
                message="This customer has no default cluster/node/storage configured for guest_exec_ephemeral indexing.",
                status_code=400,
            )
        cluster = db.get(PveCluster, index_settings.guest_exec_pve_cluster_id)
        if cluster is None:
            raise ResurgamApiError(
                code="not_found", message="Configured guest-exec PVE cluster not found.", status_code=404
            )
        trust, token = _cluster_trust_and_token(db, settings, cluster)
        vmid_result = next_free_vmid(
            cluster.host, cluster.port, trust, token, timeout=settings.connectivity_timeout_seconds
        )
        if not vmid_result.reachable or vmid_result.vmid is None:
            raise upstream_error("pve", vmid_result.error)
        target_vmid = vmid_result.vmid
        target_node = index_settings.guest_exec_target_node

    return FileIndexJob(
        status="pending",
        trigger=trigger,
        file_index_method=method,
        backup_group_id=group.id,
        backup_snapshot_id=snapshot.id,
        customer_id=customer_id,
        actor_user_id=actor_user_id,
        correlation_id=correlation_id,
        guest_exec_target_vmid=target_vmid,
        guest_exec_target_node=target_node,
    )


@router.post(
    "/backup-groups/{group_id}/snapshots/{snapshot_id}/index-jobs",
    response_model=FileIndexJobOut,
    status_code=201,
)
def create_file_index_job(
    group_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> FileIndexJobOut:
    """Manifest §13 -- enqueues a full-tree index job. `external_mount`
    jobs are processed by the out-of-process `resurgam-file-index-worker`
    (a separate privileged daemon from the file-restore worker, see
    `worker/file_index_worker.py`'s own docstring for why);
    `guest_exec_ephemeral` jobs (ADR 0021) are processed by
    `resurgam-lab-agent` instead (`api/v1/lab_agent.py`). Returns
    immediately with `status="pending"`; the caller polls
    `GET /file-index/jobs/{id}`.
    """
    group, snapshot = _get_group_and_snapshot_or_404(db, group_id, snapshot_id)
    require_restore_access(db, user, group)
    customer_id = _resolve_customer_id(db, group)
    index_settings = _require_indexing_enabled(db, group, customer_id)

    if _snapshot_has_in_flight_job(db, snapshot.id):
        raise ResurgamApiError(
            code="index_job_already_in_progress",
            message="An index job for this snapshot is already pending, running, or paused.",
            status_code=409,
        )

    job = _build_file_index_job(
        db,
        settings,
        group=group,
        snapshot=snapshot,
        customer_id=customer_id,
        index_settings=index_settings,
        trigger="manual",
        actor_user_id=user.user_id,
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    db.add(job)
    db.flush()
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="file_index_job_created",
        target_type="file_index_job",
        target_id=job.id,
        customer_id=customer_id,
    )
    db.commit()
    db.refresh(job)
    job.workload_label = workload_label(group)
    return FileIndexJobOut.model_validate(job)


@router.post("/backup-groups/{group_id}/index-jobs/bulk", response_model=BulkFileIndexJobResult, status_code=201)
def create_bulk_file_index_jobs(
    group_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> BulkFileIndexJobResult:
    """One `FileIndexJob` row per snapshot in this group that has neither a
    live index nor an in-flight job already -- not one job that iterates
    internally, so bulk reuses the exact same claim/progress/pause/cancel
    machinery a manual per-snapshot trigger uses.
    """
    group = db.get(BackupGroup, group_id)
    if group is None:
        raise ResurgamApiError(code="not_found", message="Backup group not found.", status_code=404)
    require_restore_access(db, user, group)
    customer_id = _resolve_customer_id(db, group)
    index_settings = _require_indexing_enabled(db, group, customer_id)

    snapshots = db.query(BackupSnapshot).filter_by(backup_group_id=group.id).all()
    snapshot_ids = [s.id for s in snapshots]
    # Two set-based queries covering the whole group, instead of two
    # exists() queries per snapshot -- a VM group with years of daily/
    # hourly backups can have thousands of snapshots.
    indexed_snapshot_ids = (
        {
            row[0]
            for row in db.query(FileIndexEntry.backup_snapshot_id)
            .filter(FileIndexEntry.backup_snapshot_id.in_(snapshot_ids))
            .distinct()
        }
        if snapshot_ids
        else set()
    )
    in_flight_snapshot_ids = (
        {
            row[0]
            for row in db.query(FileIndexJob.backup_snapshot_id)
            .filter(
                FileIndexJob.backup_snapshot_id.in_(snapshot_ids), FileIndexJob.status.in_(_NON_TERMINAL_JOB_STATUSES)
            )
            .distinct()
        }
        if snapshot_ids
        else set()
    )

    created_job_ids: list[uuid.UUID] = []
    skipped = 0
    correlation_id = getattr(request.state, "correlation_id", None)
    for snapshot in snapshots:
        if snapshot.id in indexed_snapshot_ids or snapshot.id in in_flight_snapshot_ids:
            skipped += 1
            continue
        job = _build_file_index_job(
            db,
            settings,
            group=group,
            snapshot=snapshot,
            customer_id=customer_id,
            index_settings=index_settings,
            trigger="bulk",
            actor_user_id=user.user_id,
            correlation_id=correlation_id,
        )
        db.add(job)
        db.flush()
        created_job_ids.append(job.id)

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="file_index_jobs_bulk_created",
        target_type="backup_group",
        target_id=group.id,
        customer_id=customer_id,
        detail={"created": len(created_job_ids), "skipped": skipped},
    )
    db.commit()
    return BulkFileIndexJobResult(created_job_ids=created_job_ids, skipped_already_indexed=skipped)


def _get_index_job_or_404(db: Session, user: AuthenticatedUser, job_id: uuid.UUID) -> FileIndexJob:
    job = db.get(FileIndexJob, job_id)
    if job is None:
        raise ResurgamApiError(code="not_found", message="Job not found.", status_code=404)
    group = db.get(BackupGroup, job.backup_group_id)
    # Never trust a bare job id alone: re-check scope against the job's
    # own group every time, so a guessed/leaked id from another
    # customer's scope 404s rather than leaking that job's data.
    require_restore_access(db, user, group)
    return job


def _attach_workload_labels(db: Session, jobs: list[FileIndexJob]) -> list[FileIndexJob]:
    """See `filerestore.py`'s identical helper -- same gap, same fix,
    for File Index's own job list/backlog (a real UX-audit finding,
    2026-09-23: a page of visually-identical pending rows with no way
    to tell which VM each one even targets).
    """
    group_ids = {job.backup_group_id for job in jobs}
    groups = {g.id: g for g in db.query(BackupGroup).filter(BackupGroup.id.in_(group_ids)).all()} if group_ids else {}
    for job in jobs:
        group = groups.get(job.backup_group_id)
        job.workload_label = workload_label(group) if group is not None else str(job.backup_group_id)
    return jobs


@router.get("/file-index/jobs/{job_id}", response_model=FileIndexJobOut)
def get_file_index_job(
    job_id: uuid.UUID, db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> FileIndexJobOut:
    job = _get_index_job_or_404(db, user, job_id)
    _attach_workload_labels(db, [job])
    return FileIndexJobOut.model_validate(job)


@router.get("/file-index/jobs", response_model=list[FileIndexJobOut])
def list_file_index_jobs(
    backup_group_id: uuid.UUID | None = Query(default=None),
    backup_snapshot_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    trigger: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[FileIndexJobOut]:
    query = db.query(FileIndexJob)
    visible = visible_customer_ids(user)
    if visible is not None:
        query = query.filter(FileIndexJob.customer_id.in_(visible))
    if backup_group_id is not None:
        query = query.filter(FileIndexJob.backup_group_id == backup_group_id)
    if backup_snapshot_id is not None:
        query = query.filter(FileIndexJob.backup_snapshot_id == backup_snapshot_id)
    if status is not None:
        query = query.filter(FileIndexJob.status == status)
    if trigger is not None:
        query = query.filter(FileIndexJob.trigger == trigger)
    jobs = query.order_by(FileIndexJob.created_at.desc()).limit(limit).all()
    _attach_workload_labels(db, jobs)
    return [FileIndexJobOut.model_validate(job) for job in jobs]


@router.post("/file-index/jobs/{job_id}/cancel", response_model=FileIndexJobOut)
def cancel_file_index_job(
    job_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> FileIndexJobOut:
    """`pending`/`paused` jobs cancel synchronously (nothing is in flight
    to interrupt); a `running` job just gets `cancel_requested` flipped --
    best-effort, noticed at the worker's next checkpoint, matching ADR
    0013's own accepted no-lease/no-heartbeat-interrupt precedent for
    file-restore jobs.
    """
    job = _get_index_job_or_404(db, user, job_id)
    if job.status in ("pending", "paused"):
        job.status = "cancelled"
        job.completed_at = datetime.now(UTC)
    elif job.status == "running":
        job.cancel_requested = True
    else:
        raise ResurgamApiError(code="job_not_cancellable", message=f"Job is already {job.status}.", status_code=409)
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="file_index_job_cancel_requested",
        target_type="file_index_job",
        target_id=job.id,
        customer_id=job.customer_id,
    )
    db.commit()
    db.refresh(job)
    _attach_workload_labels(db, [job])
    return FileIndexJobOut.model_validate(job)


def _require_customer_search_access(user: AuthenticatedUser, customer_id: uuid.UUID) -> None:
    allowed = user.has_role(RoleKey.PLATFORM_ADMIN) or user.has_role(
        RoleKey.RESTORE_OPERATOR, scope_type=ScopeType.CUSTOMER, scope_id=customer_id
    )
    if not allowed:
        raise ResurgamApiError(code="not_found", message="Customer not found.", status_code=404)


@router.get("/file-index/search", response_model=FileIndexSearchResponse)
def search_file_index(
    customer_id: uuid.UUID = Query(...),
    backup_group_id: uuid.UUID | None = Query(default=None),
    backup_snapshot_id: uuid.UUID | None = Query(default=None),
    path: str | None = Query(default=None),
    filename: str | None = Query(default=None),
    # Exposed as ?type= (unchanged API); renamed in Python so it doesn't shadow the builtin.
    entry_type: str | None = Query(default=None, alias="type"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> FileIndexSearchResponse:
    get_customer_in_scope_or_404(db, customer_id, user)
    _require_customer_search_access(user, customer_id)

    query = db.query(FileIndexEntry).filter(FileIndexEntry.customer_id == customer_id)
    if backup_group_id is not None:
        query = query.filter(FileIndexEntry.backup_group_id == backup_group_id)
    if backup_snapshot_id is not None:
        query = query.filter(FileIndexEntry.backup_snapshot_id == backup_snapshot_id)
    if path is not None:
        query = query.filter(FileIndexEntry.path.ilike(f"{_escape_like(path)}%", escape="\\"))
    if filename is not None:
        # Substring match via the pg_trgm GIN index on `filename`
        # (migration 0020) -- not just prefix, per manifest §13's
        # "global search" expectation.
        query = query.filter(FileIndexEntry.filename.ilike(f"%{_escape_like(filename)}%", escape="\\"))
    if entry_type is not None:
        query = query.filter(FileIndexEntry.type == entry_type)

    # Fetch one extra row to learn `has_more` without a separate COUNT(*)
    # over a potentially huge table.
    rows = query.order_by(FileIndexEntry.path).offset(offset).limit(limit + 1).all()
    has_more = len(rows) > limit
    items = _resolve_display_values(db, rows[:limit])
    return FileIndexSearchResponse(items=items, has_more=has_more)


def _resolve_display_values(db: Session, rows: list[FileIndexEntry]) -> list[FileIndexEntryOut]:
    """ "Reuse and link" (manifest §13, ADR 0016): a linked row's own
    `size_bytes`/`mtime` are NULL by design (see `FileIndexEntry.
    linked_from_entry_id`) -- resolved here from its link source, a
    single batched lookup over the (always small, page-bounded) result
    set actually being returned, not the whole matching set.
    """
    link_ids = {row.linked_from_entry_id for row in rows if row.linked_from_entry_id is not None}
    sources: dict[uuid.UUID, FileIndexEntry] = {}
    if link_ids:
        sources = {e.id: e for e in db.query(FileIndexEntry).filter(FileIndexEntry.id.in_(link_ids)).all()}
    items = []
    for row in rows:
        source = sources.get(row.linked_from_entry_id) if row.linked_from_entry_id is not None else None
        items.append(
            FileIndexEntryOut(
                id=row.id,
                backup_snapshot_id=row.backup_snapshot_id,
                backup_group_id=row.backup_group_id,
                archive=row.archive,
                path=row.path,
                filename=row.filename,
                type=row.type,
                size_bytes=source.size_bytes if source is not None else row.size_bytes,
                mtime=source.mtime if source is not None else row.mtime,
            )
        )
    return items


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _directory_prefix(path: str) -> str:
    return "/" if path == "/" else f"{path}/"


@router.get("/file-index/browse", response_model=list[FileIndexEntryOut])
def browse_file_index(
    backup_group_id: uuid.UUID = Query(...),
    backup_snapshot_id: uuid.UUID = Query(...),
    path: str = Query(default="/"),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[FileIndexEntryOut]:
    """Explorer-style one-level directory listing over the persistent
    index (manifest §13, "point in time" browsing raised directly by the
    user) -- unlike `filerestore/browse.py`'s on-demand equivalent, this
    never calls PBS; it's a read over already-indexed metadata, so
    switching between snapshots at the same path is instant, and works
    even for a snapshot nobody is actively browsing live. Mirrors
    `catalog.list_directory`'s own one-level-prefix filtering logic
    (there done in Python over an in-memory dump; here done in SQL over
    the persisted index), folders sorted before files, then by name.
    """
    group, snapshot = _get_group_and_snapshot_or_404(db, backup_group_id, backup_snapshot_id)
    require_restore_access(db, user, group)

    prefix = _escape_like(_directory_prefix(path))
    is_directory_first = case((FileIndexEntry.type == "directory", 0), else_=1)
    rows = (
        db.query(FileIndexEntry)
        .filter(
            FileIndexEntry.backup_snapshot_id == snapshot.id,
            FileIndexEntry.path.like(f"{prefix}%", escape="\\"),
            ~FileIndexEntry.path.like(f"{prefix}%/%", escape="\\"),
        )
        .order_by(is_directory_first, FileIndexEntry.filename)
        .all()
    )
    return _resolve_display_values(db, rows)


@router.get("/file-index/snapshot-status", response_model=SnapshotIndexStatusOut)
def get_group_snapshot_index_status(
    backup_group_id: uuid.UUID = Query(...),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> SnapshotIndexStatusOut:
    """One batched answer for "which of this group's snapshots already
    have a completed index" -- feeds the Unified File Workspace's
    calendar (green/yellow dots), disk tree, and content browser
    (which backend to talk to), all from one call instead of once per
    snapshot. Raised directly by the user: the frontend previously had
    no real endpoint for this at all, only a fragile "is the root
    listing empty" heuristic in `FileIndexBrowserPanel`.

    "Indexed" here means the same thing `browse`/`search` already rely
    on implicitly: at least one `FileIndexEntry` row exists for that
    snapshot -- not `FileIndexJob.status == "completed"`, since a job
    can complete with zero entries indexed (an empty snapshot) or a
    later job can add more without a prior one ever having "completed"
    in a way that matters here.
    """
    group = db.get(BackupGroup, backup_group_id)
    if group is None:
        raise ResurgamApiError(code="not_found", message="Backup group not found.", status_code=404)
    require_restore_access(db, user, group)

    indexed_ids = [
        row[0]
        for row in db.query(FileIndexEntry.backup_snapshot_id)
        .filter(FileIndexEntry.backup_group_id == backup_group_id)
        .distinct()
        .all()
    ]
    return SnapshotIndexStatusOut(indexed_snapshot_ids=indexed_ids)


@router.get("/customers/{customer_id}/file-index-settings", response_model=CustomerFileIndexSettingsOut)
def get_customer_file_index_settings(
    customer_id: uuid.UUID, db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> CustomerFileIndexSettingsOut:
    get_customer_in_scope_or_404(db, customer_id, user)
    settings = db.query(CustomerFileIndexSettings).filter_by(customer_id=customer_id).one_or_none()
    if settings is None:
        raise ResurgamApiError(
            code="not_found", message="File indexing has not been configured for this customer.", status_code=404
        )
    return CustomerFileIndexSettingsOut.model_validate(settings)


def _compute_next_scheduled_run_at(schedule_enabled: bool, interval_hours: int | None) -> datetime | None:
    if not schedule_enabled or interval_hours is None:
        return None
    return datetime.now(UTC) + timedelta(hours=interval_hours)


@router.post(
    "/customers/{customer_id}/file-index-settings", response_model=CustomerFileIndexSettingsOut, status_code=201
)
def create_customer_file_index_settings(
    customer_id: uuid.UUID,
    body: CustomerFileIndexSettingsCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(_require_platform_admin),
) -> CustomerFileIndexSettingsOut:
    get_customer_in_scope_or_404(db, customer_id, user)
    if db.query(CustomerFileIndexSettings).filter_by(customer_id=customer_id).one_or_none() is not None:
        raise ResurgamApiError(
            code="file_index_settings_exists",
            message="File indexing settings already exist for this customer.",
            status_code=409,
        )

    settings = CustomerFileIndexSettings(
        customer_id=customer_id,
        enabled=body.enabled,
        retention_days=body.retention_days,
        schedule_enabled=body.schedule_enabled,
        schedule_interval_hours=body.schedule_interval_hours,
        window_start_time=body.window_start_time,
        window_end_time=body.window_end_time,
        next_scheduled_run_at=_compute_next_scheduled_run_at(body.schedule_enabled, body.schedule_interval_hours),
        guest_exec_pve_cluster_id=body.guest_exec_pve_cluster_id,
        guest_exec_target_node=body.guest_exec_target_node,
        guest_exec_target_storage=body.guest_exec_target_storage,
        full_reindex_every_n_runs=body.full_reindex_every_n_runs,
    )
    db.add(settings)
    db.flush()
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="customer_file_index_settings_created",
        target_type="customer_file_index_settings",
        target_id=settings.id,
    )
    db.commit()
    db.refresh(settings)
    return CustomerFileIndexSettingsOut.model_validate(settings)


@router.patch("/customers/{customer_id}/file-index-settings", response_model=CustomerFileIndexSettingsOut)
def update_customer_file_index_settings(
    customer_id: uuid.UUID,
    body: CustomerFileIndexSettingsUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(_require_platform_admin),
) -> CustomerFileIndexSettingsOut:
    get_customer_in_scope_or_404(db, customer_id, user)
    settings = db.query(CustomerFileIndexSettings).filter_by(customer_id=customer_id).one_or_none()
    if settings is None:
        raise ResurgamApiError(
            code="not_found", message="File indexing has not been configured for this customer.", status_code=404
        )

    updates = body.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(settings, field, value)
    # A real bug found live (2026-09-24, first-ever call setting
    # guest_exec_pve_cluster_id): `model_dump()`'s default mode="python"
    # leaves a UUID field as a live `uuid.UUID` object, which psycopg's
    # JSONB adapter can't serialize -- `mode="json"` converts it (and any
    # other non-JSON-native type) to its JSON-safe form first.
    audit_detail = body.model_dump(exclude_unset=True, mode="json")

    window_start = settings.window_start_time
    window_end = settings.window_end_time
    if (window_start is None) != (window_end is None):
        raise ResurgamApiError(
            code="invalid_window",
            message="window_start_time and window_end_time must be set together.",
            status_code=400,
        )
    if settings.schedule_enabled and settings.schedule_interval_hours is None:
        raise ResurgamApiError(
            code="invalid_schedule",
            message="schedule_interval_hours is required when schedule_enabled is true.",
            status_code=400,
        )

    if "schedule_enabled" in updates or "schedule_interval_hours" in updates:
        if settings.schedule_enabled and settings.next_scheduled_run_at is None:
            settings.next_scheduled_run_at = _compute_next_scheduled_run_at(
                settings.schedule_enabled, settings.schedule_interval_hours
            )
        elif not settings.schedule_enabled:
            settings.next_scheduled_run_at = None

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="customer_file_index_settings_updated",
        target_type="customer_file_index_settings",
        target_id=settings.id,
        detail=audit_detail,
    )
    db.commit()
    db.refresh(settings)
    return CustomerFileIndexSettingsOut.model_validate(settings)


@router.delete("/customers/{customer_id}/file-index-settings", status_code=204)
def delete_customer_file_index_settings(
    customer_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(_require_platform_admin),
) -> None:
    """Deletes the settings row *and* purges every `file_index_entries`
    row for this customer -- an explicit bulk delete in the same
    transaction, not relying solely on the entries' nullable, denormalized
    `customer_id` FK's own `ON DELETE CASCADE` (that FK exists for
    `Customer` deletion, not for this settings row's deletion). Confirmed
    with the user: deleting a customer's file-index configuration should
    leave no orphaned index data behind, not age it out silently via
    retention with no config left to explain why it's still there.
    """
    get_customer_in_scope_or_404(db, customer_id, user)
    settings = db.query(CustomerFileIndexSettings).filter_by(customer_id=customer_id).one_or_none()
    if settings is None:
        raise ResurgamApiError(
            code="not_found", message="File indexing has not been configured for this customer.", status_code=404
        )

    purged = db.query(FileIndexEntry).filter(FileIndexEntry.customer_id == customer_id).delete()
    db.delete(settings)
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="customer_file_index_settings_deleted",
        target_type="customer_file_index_settings",
        target_id=customer_id,
        detail={"entries_purged": purged},
    )
    db.commit()
