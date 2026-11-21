import posixpath
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from resurgam_api.auth.dependency import extract_bearer_token, get_current_user, load_authenticated_user
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.scope import effective_group_customer_id, require_restore_access, restore_visible_customer_ids
from resurgam_api.auth.sessions import (
    InvalidSessionTokenError,
    create_file_restore_download_ticket,
    verify_file_restore_download_ticket,
    verify_session_token,
)
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.filerestore import capability, storage
from resurgam_api.filerestore.audit_detail import download_audit_detail
from resurgam_api.filerestore.browse import FileRestoreError, require_free_space
from resurgam_api.inventory.workload_label import workload_label
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.datastore import DatastoreCustomerAssignment
from resurgam_api.models.filerestore_job import FileRestoreJob
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.inventory import BackupGroup, BackupSnapshot
from resurgam_api.schemas.filerestore import (
    CapabilityReportOut,
    DownloadTicketOut,
    FileRestoreJobCreateRequest,
    FileRestoreJobOut,
)

router = APIRouter(tags=["file-restore"])

_JOB_ID = PathParam(description="File-restore job id, as returned when the job was created.")


def _record_audit(
    db: Session, request: Request, *, actor_user_id, action: str, target_id, customer_id=None, detail=None
) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            target_type="file_restore_job",
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


def _invalid(message: str) -> ResurgamApiError:
    return ResurgamApiError(code="invalid_request", message=message, status_code=422)


def _check_snapshot_path(path: str, field: str) -> str:
    """Early, clear rejection of what the worker's own traversal guard (`pbs_client`) would
    refuse later -- so the caller gets a 422 with the fix instead of a failed job."""
    if not path.startswith("/"):
        raise _invalid(f"{field}: '{path}' must be an absolute path inside the snapshot, starting with '/'.")
    if "\x00" in path:
        raise _invalid(f"{field}: the path contains a NUL character.")
    if ".." in path.split("/"):
        raise _invalid(f"{field}: '{path}' must not contain '..' components.")
    if len(path) > 4096:
        raise _invalid(f"{field}: the path is longer than 4096 characters.")
    return path.rstrip("/") or "/"


def _normalize_selection(paths: list[str], settings: Settings) -> tuple[list[str], str]:
    """Validated, de-duplicated selection (an entry inside another selected directory is
    covered by it and dropped) plus its common parent directory -- the base the ZIP's names
    are relative to."""
    if not paths:
        raise _invalid("paths: select at least one file or directory for the ZIP.")
    if len(paths) > settings.file_restore_archive_max_entries:
        raise _invalid(
            f"paths: {len(paths)} entries selected, at most {settings.file_restore_archive_max_entries} are allowed "
            "per ZIP (FILE_RESTORE_ARCHIVE_MAX_ENTRIES) -- select their parent directory instead."
        )
    cleaned = sorted({_check_snapshot_path(p, "paths") for p in paths})
    if "/" in cleaned:
        raise _invalid(
            "paths: the snapshot root '/' can't be downloaded as a ZIP -- select the directories inside it, or use a "
            "full restore for the whole backup."
        )
    selection: list[str] = []
    for path in cleaned:  # sorted: a parent always comes before its children
        if not any(path.startswith(kept + "/") for kept in selection):
            selection.append(path)
    base = posixpath.commonpath([posixpath.dirname(p) for p in selection])
    return selection, base


@router.post(
    "/backup-groups/{group_id}/snapshots/{snapshot_id}/files/jobs",
    response_model=FileRestoreJobOut,
    status_code=201,
    summary="Create a file-restore job (list, download, or ZIP of a folder/selection)",
    responses={
        404: {"description": "Backup group or snapshot not found, or outside the caller's scope."},
        422: {"description": "Invalid path/selection; the message says what to fix."},
        507: {"description": "Not enough free disk space on the appliance for download content."},
    },
)
def create_file_restore_job(
    group_id: Annotated[uuid.UUID, PathParam(description="Backup group (VM/CT/host) the snapshot belongs to.")],
    snapshot_id: Annotated[uuid.UUID, PathParam(description="Snapshot of that group to list or extract from.")],
    body: FileRestoreJobCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> FileRestoreJobOut:
    """Manifest §13/§15 -- enqueues a `list`, `download` or `archive` job for the
    out-of-process worker (`worker/file_restore_worker.py`, ADR 0013)
    instead of running `filerestore/browse.py` synchronously here. Returns
    immediately with `status="pending"`; the caller polls
    `GET /file-restore/jobs/{id}` until it reaches a terminal status.

    `archive` (folder or multi-selection download) produces one ZIP of the selected files and
    directories; entries that can't be extracted are skipped and listed in the job's
    `archive_report` rather than failing the archive. Download content is stored as a file on
    the appliance, so `download`/`archive` are refused up front (507) when its disk is below the
    configured free-space reserve.
    """
    group, snapshot = _get_group_and_snapshot_or_404(db, group_id, snapshot_id)
    require_restore_access(db, user, group)

    paths: list[str] | None = None
    if body.job_type == "archive":
        paths, path = _normalize_selection(body.paths or [], settings)
    else:
        if not body.path:
            raise _invalid(f"path: required for a '{body.job_type}' job -- the snapshot path to list or download.")
        path = _check_snapshot_path(body.path, "path")
    if body.job_type in ("download", "archive"):
        try:
            require_free_space(settings)
        except FileRestoreError as exc:
            raise ResurgamApiError(code=exc.code, message=exc.message, status_code=exc.status_code) from exc

    assignment = db.query(DatastoreCustomerAssignment).filter_by(datastore_id=group.datastore_id).one_or_none()
    job = FileRestoreJob(
        job_type=body.job_type,
        status="pending",
        backup_group_id=group.id,
        backup_snapshot_id=snapshot.id,
        customer_id=effective_group_customer_id(group, assignment),
        actor_user_id=user.user_id,
        path=path,
        paths=paths,
        correlation_id=getattr(request.state, "correlation_id", None),
        expires_at=datetime.now(UTC) + timedelta(seconds=settings.file_restore_job_ttl_seconds),
    )
    db.add(job)
    db.flush()
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="file_restore_job_created",
        target_id=job.id,
        customer_id=job.customer_id,
        detail=None
        if body.job_type == "list"
        else {"job_type": body.job_type, "paths": paths if paths is not None else [path]},
    )
    db.commit()
    db.refresh(job)
    _decorate(db, settings, [job])
    return FileRestoreJobOut.model_validate(job)


def _get_job_or_404(db: Session, user: AuthenticatedUser, job_id: uuid.UUID) -> FileRestoreJob:
    job = db.get(FileRestoreJob, job_id)
    if job is None:
        raise ResurgamApiError(code="not_found", message="Job not found.", status_code=404)
    group = db.get(BackupGroup, job.backup_group_id)
    # Never trust a bare job id alone: re-check scope against the job's
    # own group every time, so a guessed/leaked id from another
    # customer's scope 404s rather than leaking that job's data.
    require_restore_access(db, user, group)
    return job


def _decorate(db: Session, settings: Settings, jobs: list[FileRestoreJob]) -> list[FileRestoreJob]:
    """Workload labels plus `content_available` (computed, never stored)."""
    _attach_workload_labels(db, jobs)
    now = datetime.now(UTC)
    for job in jobs:
        job.content_available = (
            job.job_type != "list"
            and job.status == "completed"
            and job.result_storage_name is not None
            and job.expires_at > now
        )
    return jobs


def _attach_workload_labels(db: Session, jobs: list[FileRestoreJob]) -> list[FileRestoreJob]:
    """A job list's single most useful piece of context -- *which*
    workload it's about -- was previously missing entirely (a real gap
    found live, 2026-09-23 UX audit: an operator triaging a backlog of
    jobs could tell what each one was doing but not to which VM/CT).
    One batched lookup for the whole page, not one query per row.
    """
    group_ids = {job.backup_group_id for job in jobs}
    groups = {g.id: g for g in db.query(BackupGroup).filter(BackupGroup.id.in_(group_ids)).all()} if group_ids else {}
    for job in jobs:
        group = groups.get(job.backup_group_id)
        job.workload_label = workload_label(group) if group is not None else str(job.backup_group_id)
    return jobs


@router.get("/file-restore/jobs/{job_id}", response_model=FileRestoreJobOut)
def get_file_restore_job(
    job_id: Annotated[uuid.UUID, _JOB_ID],
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> FileRestoreJobOut:
    """One job's current state -- polled by the UI every ~1.5s while `pending`/`running`."""
    job = _get_job_or_404(db, user, job_id)
    _decorate(db, settings, [job])
    return FileRestoreJobOut.model_validate(job)


@router.get("/file-restore/jobs", response_model=list[FileRestoreJobOut])
def list_file_restore_jobs(
    status: str | None = Query(default=None, description="Only jobs with this status, e.g. `running`."),
    job_type: str | None = Query(default=None, description="Only jobs of this type: `list`, `download`, `archive`."),
    limit: int = Query(default=100, ge=1, le=1000, description="Most recent N jobs."),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[FileRestoreJobOut]:
    """Admin-visibility endpoint (raised directly by the user, alongside
    File Index's own job list and Restore Plan executions): every file-
    restore job the caller's scope can see, most recent first. Unlike
    `get_file_restore_job`/`_get_job_or_404` (re-derives scope from the
    job's own group on every read), this filters directly on the
    denormalized `customer_id` column -- but still requires
    `restore_operator`-or-`platform_admin` per customer, same as every
    other endpoint in this file: a completed 'list' job's `result` is a
    real file/directory listing from inside that customer's backup, not
    metadata a broader customer-scoped role (viewer/auditor/customer_admin)
    is meant to see (confirmed by `file_index.py`'s `search`/`browse`
    endpoints gating on the same role for the same reason).
    """
    query = db.query(FileRestoreJob)
    if not user.has_role(RoleKey.PLATFORM_ADMIN):
        query = query.filter(FileRestoreJob.customer_id.in_(restore_visible_customer_ids(user)))
    if status is not None:
        query = query.filter(FileRestoreJob.status == status)
    if job_type is not None:
        query = query.filter(FileRestoreJob.job_type == job_type)
    jobs = query.order_by(FileRestoreJob.created_at.desc()).limit(limit).all()
    _decorate(db, settings, jobs)
    return [FileRestoreJobOut.model_validate(job) for job in jobs]


@router.post("/file-restore/jobs/{job_id}/cancel", response_model=FileRestoreJobOut)
def cancel_file_restore_job(
    job_id: Annotated[uuid.UUID, _JOB_ID],
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> FileRestoreJobOut:
    """Cancels a `pending` or `running` job; it ends as `cancelled` right
    away. A running download/ZIP stops within a few seconds (the worker's
    guard checks for the cancel while the PBS tool writes and before every
    ZIP member) and its partial content is deleted; a running listing
    finishes its one PBS call and its result is discarded. The row is
    locked, so a cancel racing the worker's claim or its finish is never
    overwritten. 409 `job_not_cancellable` once the job has ended.
    """
    _get_job_or_404(db, user, job_id)
    job = db.execute(
        select(FileRestoreJob)
        .where(FileRestoreJob.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    if job.status not in ("pending", "running"):
        raise ResurgamApiError(
            code="job_not_cancellable",
            message=f"Job is {job.status} -- only a pending or running job can be cancelled.",
            status_code=409,
        )

    job.status = "cancelled"
    job.completed_at = datetime.now(UTC)
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="file_restore_job_cancelled",
        target_id=job.id,
        customer_id=job.customer_id,
    )
    db.commit()
    db.refresh(job)
    _decorate(db, settings, [job])
    return FileRestoreJobOut.model_validate(job)


def _completed_content_or_error(db: Session, settings: Settings, job: FileRestoreJob) -> Path:
    if job.job_type not in ("download", "archive") or job.status != "completed":
        raise ResurgamApiError(
            code="job_not_ready", message="Job is not a completed download or ZIP archive.", status_code=409
        )
    path = storage.content_path(settings, job.result_storage_name)
    if path is None or job.expires_at <= datetime.now(UTC):
        raise ResurgamApiError(
            code="content_expired",
            message="The downloaded content is no longer available (its download window has passed) -- start the "
            "download again.",
            status_code=410,
        )
    return path


@router.post(
    "/file-restore/jobs/{job_id}/download-ticket",
    response_model=DownloadTicketOut,
    summary="Get a short-lived link that downloads a completed job's content",
    responses={409: {"description": "Not a completed download/archive."}, 410: {"description": "Content expired."}},
)
def create_download_ticket(
    job_id: Annotated[uuid.UUID, _JOB_ID],
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> DownloadTicketOut:
    """Returns a link to `GET /file-restore/jobs/{id}/download?ticket=...` that works without an
    `Authorization` header, so the browser can navigate to it and stream a large file (a ZIP of
    many GB) straight to disk rather than the page holding it in memory. The ticket is signed,
    bound to this job and user, and expires quickly; the download re-checks the user's access.
    """
    job = _get_job_or_404(db, user, job_id)
    _completed_content_or_error(db, settings, job)
    ticket, expires = create_file_restore_download_ticket(user.user_id, job.id, settings)
    return DownloadTicketOut(
        download_url=f"/file-restore/jobs/{job.id}/download?ticket={ticket}",
        expires_at=datetime.fromtimestamp(expires, tz=UTC),
    )


def _download_user(
    job_id: Annotated[uuid.UUID, _JOB_ID],
    ticket: str | None = Query(
        default=None,
        description="Download ticket from `POST /file-restore/jobs/{id}/download-ticket`; replaces the "
        "`Authorization` header for a plain browser navigation.",
    ),
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> AuthenticatedUser:
    try:
        if ticket:
            user_id = verify_file_restore_download_ticket(ticket, job_id, settings)
        else:
            token = extract_bearer_token(authorization)
            if token is None:
                raise ResurgamApiError(
                    code="not_authenticated",
                    message="Missing Authorization header or download ticket.",
                    status_code=401,
                )
            user_id = verify_session_token(token, settings)
    except InvalidSessionTokenError as exc:
        raise ResurgamApiError(
            code="invalid_session",
            message="Session token or download ticket is invalid or expired -- request a new download link.",
            status_code=401,
        ) from exc
    return load_authenticated_user(db, user_id)


@router.get(
    "/file-restore/jobs/{job_id}/download",
    response_class=FileResponse,
    summary="Download a completed job's content (single file or ZIP)",
    responses={
        200: {"description": "The content, streamed from disk.", "content": {"application/octet-stream": {}}},
        409: {"description": "Not a completed download/archive."},
        410: {"description": "Content expired."},
    },
)
def download_file_restore_job(
    job_id: Annotated[uuid.UUID, _JOB_ID],
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(_download_user),
) -> FileResponse:
    """Streams the stored content file (never read into memory) with `Content-Disposition:
    attachment` -- UTF-8 file names are sent RFC 6266/5987-encoded (`filename*`). Authenticated
    by the usual `Authorization` header or by a `ticket` query parameter. Every fetch is audited
    with what was downloaded (paths, counts, size) -- never the content.
    """
    job = _get_job_or_404(db, user, job_id)
    path = _completed_content_or_error(db, settings, job)

    # Manifest §10.3: an audit event for the actual download, not just the
    # job that produced the content -- the only one of Browse/Extract/
    # Download/Stop that wasn't already covered by job lifecycle events.
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="file_restore_job_downloaded",
        target_id=job.id,
        customer_id=job.customer_id,
        detail={
            **(download_audit_detail(job) or {}),
            "via": "ticket" if request.query_params.get("ticket") else "header",
        },
    )
    db.commit()

    return FileResponse(
        path,
        media_type="application/zip" if job.job_type == "archive" else "application/octet-stream",
        filename=job.result_filename or path.name,
    )


@router.get("/file-restore/capability", response_model=CapabilityReportOut)
def get_file_restore_capability(
    _user: AuthenticatedUser = Depends(get_current_user), settings: Settings = Depends(get_settings)
) -> CapabilityReportOut:
    """Can files be browsed/downloaded out of VM and CT/host backups here -- per backup type, with what is
    missing, on which side, and how to fix it. On the appliance this reflects the appliance host (where the
    file-restore/file-index workers run every PBS CLI call), read from resurgam-host-probe's report; the API
    container itself has none of these tools and never needs them. In development it checks this process live.
    Never fails: every check resolves to a status (manifest §18.12).
    """
    state_file = Path(settings.host_capabilities_file) if settings.host_capabilities_file else None
    return capability.check_capability(state_file)
