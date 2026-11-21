"""Release update source (ADR 0026): System Settings -> Updates -> Update
source. Configure the GitHub/GitLab repository releases are published to
(plus an access token for a private one), check it for newer versions, and
download a release's bundle into the updates `incoming/` directory -- from
there the ADR 0024 verify/apply flow (`api/v1/updates.py`) is unchanged.
All endpoints are `platform_admin` only.
"""

from fastapi import APIRouter, Depends, Path, Request, Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from resurgam_api import update_source as source
from resurgam_api.appliance_updates import UpdatesError, UpdatesStore
from resurgam_api.auth.dependency import require_role
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.secrets import SecretPurpose, SecretReference
from resurgam_api.models.update_source import UpdateSourceSettings
from resurgam_api.schemas.update_source import (
    UpdateSourceDownloadCreate,
    UpdateSourceDownloadOut,
    UpdateSourceOut,
    UpdateSourceReleaseOut,
    UpdateSourceSettingsRequest,
    UpdateSourceTestOut,
    UpdateSourceTestRequest,
)
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider
from resurgam_api.updates.release_source import ReleaseSourceError
from resurgam_api.version import running_version

router = APIRouter(prefix="/updates/source", tags=["updates"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)

# RESURGAM's own repository -- what the form shows before anything is saved.
DEFAULT_REPOSITORY_URL = "https://github.com/cybrwerk/resurgam"


def _store(settings: Settings) -> UpdatesStore:
    return UpdatesStore(settings.updates_dir, settings.backups_dir)


def _api_error(exc: UpdatesError) -> ResurgamApiError:
    return ResurgamApiError(code=exc.code, message=exc.message, status_code=exc.status_code)


def _to_out(row: UpdateSourceSettings | None, settings: Settings) -> UpdateSourceOut:
    store = _store(settings)
    active = None
    if store.unavailable_reason() is None:
        found = source.active_download(store)
        active = UpdateSourceDownloadOut.model_validate(found) if found else None
    releases = [UpdateSourceReleaseOut.model_validate(r) for r in source.cached_releases(row)]
    if row is None:
        return UpdateSourceOut(
            configured=False,
            provider="github",
            repository_url=DEFAULT_REPOSITORY_URL,
            token_configured=False,
            tls_verify=True,
            include_prereleases=False,
            auto_check=True,
            check_interval_hours=24,
            current_version=running_version(),
            active_download=active,
        )
    return UpdateSourceOut(
        configured=True,
        provider=row.provider,
        repository_url=row.repository_url,
        token_configured=row.token_ref_id is not None,
        tls_verify=row.tls_verify,
        include_prereleases=row.include_prereleases,
        auto_check=row.auto_check,
        check_interval_hours=row.check_interval_hours,
        current_version=running_version(),
        last_checked_at=row.last_checked_at.isoformat() if row.last_checked_at else None,
        last_check_error=row.last_check_error,
        latest_version=releases[0].version if releases else None,
        available_releases=releases,
        active_download=active,
    )


def _audit(db: Session, request: Request, user: AuthenticatedUser, action: str, detail: dict) -> None:
    db.add(
        AuditEvent(
            actor_user_id=user.user_id,
            action=action,
            target_type="update_source",
            target_id=None,
            correlation_id=getattr(request.state, "correlation_id", None),
            detail=detail,
        )
    )
    db.commit()


def _drop_token(db: Session, settings: Settings, row: UpdateSourceSettings) -> None:
    """Remove the stored token: its encrypted value and its reference row
    (so a later token can be stored under the same reference key)."""
    LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key).delete(source.TOKEN_REFERENCE_KEY)
    ref_id = row.token_ref_id
    row.token_ref_id = None
    db.flush()
    ref = db.get(SecretReference, ref_id) if ref_id is not None else None
    if ref is not None:
        db.delete(ref)
        db.flush()


def _require_source(db: Session) -> UpdateSourceSettings:
    row = source.get_update_source(db)
    if row is None:
        raise ResurgamApiError(
            code="update_source_not_configured",
            message="No update source is configured. Save the repository URL (and token) first.",
            status_code=409,
        )
    return row


@router.get("", response_model=UpdateSourceOut)
def get_update_source(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user: AuthenticatedUser = Depends(_require_platform_admin),
) -> UpdateSourceOut:
    """`platform_admin` only. The saved update source, the cached result of
    the last check (never contacts GitHub/GitLab itself -- use
    POST /updates/source/check) and the bundle download in progress.
    Before anything is saved it answers `configured: false` with RESURGAM's
    own GitHub repository pre-filled."""
    return _to_out(source.get_update_source(db), settings)


@router.put("", response_model=UpdateSourceOut)
def save_update_source(
    body: UpdateSourceSettingsRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(_require_platform_admin),
) -> UpdateSourceOut:
    """`platform_admin` only. Creates or updates the update source. `token`
    is three-way like the SMTP password: omitted keeps the stored token,
    `null` removes it, a string replaces it (stored encrypted, never
    returned). Changing the repository clears the cached check result.
    Moving to another provider or API host without sending a new `token`
    removes the stored one (answer: `token_cleared: true`) -- it is never
    sent to a host it was not saved for. Answers 400 when the URL does not
    name a repository for `provider`."""
    try:
        source.build_client(body.provider, body.repository_url, None, body.tls_verify)
    except ReleaseSourceError as exc:
        raise ResurgamApiError(code=exc.code, message=exc.message, status_code=400) from exc

    row = source.get_update_source(db)
    token_cleared = False
    if row is None:
        row = UpdateSourceSettings(provider=body.provider, repository_url=body.repository_url)
        db.add(row)
    else:
        if (row.provider, row.repository_url, row.include_prereleases) != (
            body.provider,
            body.repository_url,
            body.include_prereleases,
        ):
            row.available_releases = None
            row.latest_version = None
            row.last_check_error = None
            row.last_checked_at = None
        if (
            "token" not in body.model_fields_set
            and row.token_ref_id is not None
            and not source.same_token_scope(row, body.provider, body.repository_url)
        ):
            _drop_token(db, settings, row)
            token_cleared = True
    row.provider = body.provider
    row.repository_url = body.repository_url
    row.tls_verify = body.tls_verify
    row.include_prereleases = body.include_prereleases
    row.auto_check = body.auto_check
    row.check_interval_hours = body.check_interval_hours

    if "token" in body.model_fields_set:
        if not body.token:
            if row.token_ref_id is not None:
                _drop_token(db, settings, row)
        else:
            if row.token_ref_id is None:
                # Reuse a reference left behind by an older version that only
                # deleted the value; the reference key is unique.
                secret_ref = db.query(SecretReference).filter_by(reference_key=source.TOKEN_REFERENCE_KEY).first()
                if secret_ref is None:
                    secret_ref = SecretReference(
                        reference_key=source.TOKEN_REFERENCE_KEY,
                        description="Update source access token (GitHub/GitLab)",
                        purpose=SecretPurpose.UPDATE_SOURCE_TOKEN,
                    )
                    db.add(secret_ref)
                    db.flush()
                row.token_ref_id = secret_ref.id
            LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key).store(
                source.TOKEN_REFERENCE_KEY, body.token.strip()
            )

    db.add(
        AuditEvent(
            actor_user_id=user.user_id,
            action="update_source_saved",
            target_type="update_source",
            target_id=None,
            correlation_id=getattr(request.state, "correlation_id", None),
            detail={
                "provider": body.provider,
                "repository_url": body.repository_url,
                "auto_check": body.auto_check,
                "token_changed": "token" in body.model_fields_set,
                "token_cleared": token_cleared,
            },
        )
    )
    db.commit()
    db.refresh(row)
    return _to_out(row, settings).model_copy(update={"token_cleared": token_cleared})


@router.delete("", status_code=204)
def delete_update_source(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(_require_platform_admin),
) -> Response:
    """`platform_admin` only. Removes the update source and its stored
    token. Bundles already downloaded stay in the bundle list."""
    row = source.get_update_source(db)
    if row is not None:
        if row.token_ref_id is not None:
            _drop_token(db, settings, row)
        db.delete(row)
        db.commit()
        _audit(db, request, user, "update_source_deleted", {})
    return Response(status_code=204)


@router.post("/test", response_model=UpdateSourceTestOut)
def test_update_source(
    body: UpdateSourceTestRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user: AuthenticatedUser = Depends(_require_platform_admin),
) -> UpdateSourceTestOut:
    """`platform_admin` only. Tries the given values *before* saving them:
    reads the repository and its releases with the token from the body
    (or the stored one when `token` is omitted -- but only when provider
    and API host match the saved source; otherwise the test runs without a
    token and says so in `stored_token_withheld`). Always answers 200; a
    failure comes back as `ok: false` with an explanation in `error`."""
    withheld = False
    if "token" in body.model_fields_set:
        token = body.token or None
    else:
        row = source.get_update_source(db)
        token = None
        if row is not None and row.token_ref_id is not None:
            if source.same_token_scope(row, body.provider, body.repository_url):
                token = source.resolve_token(db, settings, row)
            else:
                withheld = True
    try:
        client = source.build_client(body.provider, body.repository_url, token, body.tls_verify)
        repo = client.test()
        releases = client.list_releases()
    except ReleaseSourceError as exc:
        return UpdateSourceTestOut(ok=False, error=exc.message, stored_token_withheld=withheld)
    return UpdateSourceTestOut(
        ok=True,
        stored_token_withheld=withheld,
        repository=repo["full_name"],
        private=repo["private"],
        releases_found=len(releases),
        latest_release=releases[0].version if releases else None,
    )


@router.post("/check", response_model=UpdateSourceOut)
def check_update_source(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user: AuthenticatedUser = Depends(_require_platform_admin),
) -> UpdateSourceOut:
    """`platform_admin` only. Checks the saved source for releases newer than
    the running version now and caches the result. A failed check answers
    200 with `last_check_error` set (the previous release list is kept)."""
    row = source.run_check(db, settings, _require_source(db))
    return _to_out(row, settings)


@router.post("/downloads", response_model=UpdateSourceDownloadOut, status_code=202)
def start_download(
    body: UpdateSourceDownloadCreate,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(_require_platform_admin),
) -> UpdateSourceDownloadOut:
    """`platform_admin` only, appliance only (503 elsewhere). Downloads the
    bundle of one release from the last check into incoming/ in the
    background; poll GET /updates/source/downloads/{id}. Once it succeeded,
    request a `verify` for its `bundle` (POST /updates/requests) exactly as
    for an uploaded bundle. 404 when the version is not among the cached
    newer releases (check again first), 409 when the release has no bundle
    attached or another download is running."""
    store = _store(settings)
    try:
        store.require_available()
    except UpdatesError as exc:
        raise _api_error(exc) from exc
    row = _require_source(db)
    release = source.find_cached_release(row, body.version)
    if release is None:
        raise ResurgamApiError(
            code="release_not_found",
            message=f"Release {body.version} is not among the newer releases found by the last check. "
            "Check for updates again.",
            status_code=404,
        )
    try:
        status = source.start_download(store, settings, source.client_for(db, settings, row), release, user.email)
    except UpdatesError as exc:
        raise _api_error(exc) from exc
    except ReleaseSourceError as exc:
        raise ResurgamApiError(code=exc.code, message=exc.message, status_code=400) from exc
    _audit(
        db,
        request,
        user,
        "update_bundle_download_started",
        {"version": body.version, "download_id": status["download_id"], "repository_url": row.repository_url},
    )
    return UpdateSourceDownloadOut.model_validate(status)


@router.get("/downloads/{download_id}", response_model=UpdateSourceDownloadOut)
async def get_download(
    download_id: str = Path(description="Download id returned by POST /updates/source/downloads (32 hex characters)."),
    settings: Settings = Depends(get_settings),
    _user: AuthenticatedUser = Depends(_require_platform_admin),
) -> UpdateSourceDownloadOut:
    """`platform_admin` only. Progress of one bundle download."""
    store = _store(settings)
    try:
        store.require_available()
        status = await run_in_threadpool(source.get_download, store, download_id)
    except UpdatesError as exc:
        raise _api_error(exc) from exc
    return UpdateSourceDownloadOut.model_validate(status)
