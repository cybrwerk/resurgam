"""Appliance updates (ADR 0024): upload a signed release bundle, ask the
host-side updater to verify or apply it, and follow its progress.

The backend only writes into the shared updates directory and reads the
updater's status files back -- it never runs Docker or systemd itself.
All endpoints are `platform_admin` only.
"""

import os
import shutil
from pathlib import Path as FsPath

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from resurgam_api.appliance_updates import (
    TAR_BLOCK_SIZE,
    UpdatesError,
    UpdatesStore,
    file_time_iso,
    inspect_bundle,
    looks_like_tar_header,
)
from resurgam_api.auth.dependency import require_role
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.identity import RoleKey
from resurgam_api.schemas.updates import (
    UpdateBackupOut,
    UpdateBundleOut,
    UpdateHistoryEntryOut,
    UpdateRequestCreate,
    UpdateRequestStatusOut,
    UpdatesOverviewOut,
)
from resurgam_api.updates import bundle
from resurgam_api.version import running_version

router = APIRouter(prefix="/updates", tags=["updates"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)


def _store(settings: Settings = Depends(get_settings)) -> UpdatesStore:
    return UpdatesStore(settings.updates_dir, settings.backups_dir)


def _available_store(
    store: UpdatesStore = Depends(_store), _user: AuthenticatedUser = Depends(_require_platform_admin)
) -> UpdatesStore:
    """Authorises first (so an unauthenticated caller gets 401, not 503),
    then answers 503 when this is not an appliance."""
    try:
        store.require_available()
    except UpdatesError as exc:
        raise _api_error(exc) from exc
    return store


def _api_error(exc: UpdatesError) -> ResurgamApiError:
    return ResurgamApiError(code=exc.code, message=exc.message, status_code=exc.status_code)


def _audit(db: Session, request: Request, user: AuthenticatedUser, action: str, target_id: str, detail: dict) -> None:
    db.add(
        AuditEvent(
            actor_user_id=user.user_id,
            action=action,
            target_type="appliance_update",
            target_id=target_id,
            correlation_id=getattr(request.state, "correlation_id", None),
            detail=detail,
        )
    )
    db.commit()


@router.get("", response_model=UpdatesOverviewOut)
def get_updates_overview(
    store: UpdatesStore = Depends(_store),
    settings: Settings = Depends(get_settings),
    _user: AuthenticatedUser = Depends(_require_platform_admin),
) -> UpdatesOverviewOut:
    """`platform_admin` only. Whether updates are possible here, the running
    version, the upload size limit and the apply in progress (if any).
    Answers 200 with `available: false` outside the appliance instead of
    503, so the UI can explain why instead of showing an error."""
    reason = store.unavailable_reason()
    active = None
    if reason is None:
        store.require_available()
        found = store.active_apply()
        active = UpdateRequestStatusOut.model_validate(found) if found else None
    return UpdatesOverviewOut(
        available=reason is None,
        unavailable_reason=reason,
        current_version=running_version(),
        max_bundle_bytes=settings.updates_max_bundle_bytes,
        active_apply=active,
    )


@router.get("/bundles", response_model=list[UpdateBundleOut])
def list_bundles(
    store: UpdatesStore = Depends(_available_store),
    _user: AuthenticatedUser = Depends(_require_platform_admin),
) -> list[UpdateBundleOut]:
    """`platform_admin` only. Bundles in the `incoming/` directory, newest
    first -- uploaded through the UI or copied there by an operator."""
    return [UpdateBundleOut.model_validate(b) for b in store.list_bundles()]


@router.post(
    "/bundles",
    response_model=UpdateBundleOut,
    status_code=201,
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": "The raw `resurgam-<version>.bundle` file as the request body (not multipart).",
            "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def upload_bundle(
    request: Request,
    filename: str = Query(
        default="",
        max_length=255,
        description="Original file name of the bundle (e.g. resurgam-0.3.0.bundle); recorded in the audit log only.",
    ),
    store: UpdatesStore = Depends(_available_store),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(_require_platform_admin),
) -> UpdateBundleOut:
    """`platform_admin` only. Streams the request body straight to
    `incoming/` (never held in memory), enforcing the size limit while
    streaming. A body that is not a tar archive is rejected after its first
    512 bytes; a tar without `manifest.json` and `manifest.json.sig` is
    rejected once complete. The signature itself is verified by the host
    updater -- request a `verify` next."""
    limit = settings.updates_max_bundle_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit():
        declared_size = int(declared)
        if declared_size > limit:
            raise ResurgamApiError(
                code="bundle_too_large",
                message=f"The bundle is {declared_size} bytes; the limit is {limit} bytes "
                "(setting UPDATES_MAX_BUNDLE_BYTES).",
                status_code=413,
            )
        free = shutil.disk_usage(store.incoming).free
        if declared_size > free:
            raise ResurgamApiError(
                code="insufficient_storage",
                message=f"Not enough free space in {store.incoming} ({free} bytes free, bundle is {declared_size}). "
                "Delete old bundles or backups first.",
                status_code=507,
            )

    part_path, final_path = store.new_upload_paths()
    received = 0
    header_checked = False
    head = b""
    try:
        with part_path.open("wb") as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                received += len(chunk)
                if received > limit:
                    raise ResurgamApiError(
                        code="bundle_too_large",
                        message=f"The upload exceeded the limit of {limit} bytes (setting UPDATES_MAX_BUNDLE_BYTES).",
                        status_code=413,
                    )
                if not header_checked:
                    head += chunk[: TAR_BLOCK_SIZE - len(head)]
                    if len(head) >= TAR_BLOCK_SIZE:
                        header_checked = True
                        if not looks_like_tar_header(head):
                            raise ResurgamApiError(
                                code="not_a_bundle",
                                message="This file is not a RESURGAM update bundle (not a tar archive). "
                                "Upload the resurgam-<version>.bundle file from the release.",
                                status_code=400,
                            )
                await run_in_threadpool(handle.write, chunk)
        if not header_checked:
            raise ResurgamApiError(
                code="not_a_bundle",
                message="The upload is empty or too small to be a RESURGAM update bundle.",
                status_code=400,
            )
        inspection = await run_in_threadpool(inspect_bundle, part_path)
        if inspection.problem:
            raise ResurgamApiError(
                code="not_a_bundle",
                message=f"This file is not a RESURGAM update bundle: {inspection.problem} "
                "Upload the resurgam-<version>.bundle file from the release.",
                status_code=400,
            )
        prechecked = await run_in_threadpool(_signature_precheck, part_path, settings.release_public_key_file)
        os.replace(part_path, final_path)
    finally:
        part_path.unlink(missing_ok=True)

    version = (inspection.manifest or {}).get("version")
    _audit(
        db,
        request,
        user,
        "update_bundle_uploaded",
        final_path.name,
        {"original_filename": filename or None, "size_bytes": received, "version": version},
    )
    return UpdateBundleOut(
        name=final_path.name,
        size_bytes=received,
        uploaded_at=file_time_iso(final_path),
        version=version,
        problem=None,
        signature_prechecked=prechecked,
    )


def _signature_precheck(bundle_path: FsPath, public_key_file: str) -> bool:
    """Fast signature + structure check (no component hashing) with the
    release key baked into the image. Returns False when no key is
    available (local dev); raises a 400 when the bundle is rejected."""
    key_path = FsPath(public_key_file)
    if not key_path.is_file():
        return False
    try:
        bundle.verify_bundle(bundle_path, bundle.load_public_key_file(key_path), None, check_hashes=False)
    except bundle.BundleError as exc:
        raise ResurgamApiError(
            code="bundle_signature_invalid",
            message=f"The bundle was rejected: {exc}",
            status_code=400,
        ) from exc
    return True


@router.delete("/bundles/{name}", status_code=204)
def delete_bundle(
    request: Request,
    name: str = Path(description="File name of the uploaded bundle, as listed by GET /updates/bundles."),
    store: UpdatesStore = Depends(_available_store),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(_require_platform_admin),
) -> Response:
    """`platform_admin` only. Removes an uploaded bundle. Refused (409)
    while a queued or running request still uses it."""
    try:
        store.delete_bundle(name)
    except UpdatesError as exc:
        raise _api_error(exc) from exc
    _audit(db, request, user, "update_bundle_deleted", name, {})
    return Response(status_code=204)


@router.get("/requests", response_model=list[UpdateRequestStatusOut])
def list_requests(
    store: UpdatesStore = Depends(_available_store),
    _user: AuthenticatedUser = Depends(_require_platform_admin),
) -> list[UpdateRequestStatusOut]:
    """`platform_admin` only. Every verify/apply request still in the
    shared directory with its current status, newest first."""
    return [UpdateRequestStatusOut.model_validate(s) for s in store.list_request_statuses()]


@router.post("/requests", response_model=UpdateRequestStatusOut, status_code=202)
def create_request(
    body: UpdateRequestCreate,
    request: Request,
    store: UpdatesStore = Depends(_available_store),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(_require_platform_admin),
) -> UpdateRequestStatusOut:
    """`platform_admin` only. Queues a `verify` or `apply` for the host
    updater by writing `requests/<id>.json`; poll
    GET /updates/requests/{id} for progress. An `apply` is refused (409)
    while another apply is queued or running. During an apply the backend
    itself is restarted, so expect polling to fail briefly."""
    try:
        status = store.create_request(action=body.action, bundle=body.bundle, requested_by=user.email)
    except UpdatesError as exc:
        raise _api_error(exc) from exc
    _audit(
        db,
        request,
        user,
        f"update_{body.action}_requested",
        status["request_id"],
        {"bundle": body.bundle, "from_version": running_version()},
    )
    return UpdateRequestStatusOut.model_validate(status)


@router.get("/requests/{request_id}", response_model=UpdateRequestStatusOut)
def get_request_status(
    request_id: str = Path(description="Request id returned by POST /updates/requests (32 hex characters)."),
    store: UpdatesStore = Depends(_available_store),
    _user: AuthenticatedUser = Depends(_require_platform_admin),
) -> UpdateRequestStatusOut:
    """`platform_admin` only. The host updater's status for one request
    (`queued` until it has written a status file)."""
    try:
        return UpdateRequestStatusOut.model_validate(store.get_status(request_id))
    except UpdatesError as exc:
        raise _api_error(exc) from exc


@router.get("/history", response_model=list[UpdateHistoryEntryOut])
def get_history(
    store: UpdatesStore = Depends(_available_store),
    _user: AuthenticatedUser = Depends(_require_platform_admin),
) -> list[UpdateHistoryEntryOut]:
    """`platform_admin` only. Applied and rolled-back updates from the
    updater's history.json, newest first (empty before the first update)."""
    return [UpdateHistoryEntryOut.model_validate(e) for e in store.history()]


@router.get("/backups", response_model=list[UpdateBackupOut])
def list_backups(
    store: UpdatesStore = Depends(_available_store),
    _user: AuthenticatedUser = Depends(_require_platform_admin),
) -> list[UpdateBackupOut]:
    """`platform_admin` only. Pre-update database dumps the updater keeps
    (the last 5), newest first. Restoring one is a host-side operation
    (`resurgam-update`), not offered through the API."""
    return [UpdateBackupOut.model_validate(b) for b in store.list_backups()]
