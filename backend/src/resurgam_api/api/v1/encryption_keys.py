"""Encryption key store (ADR 0031, stage 2): PBS client-side encryption keys by
fingerprint.

Who may do what:

- platform admins: everything -- upload, label, assign to a tenant, delete,
  download, key sheet, export and import, and the per-tenant switch below;
- tenant admins (`customer_admin`): see their own tenant's keys, and download
  them or print their key sheet only when a platform admin turned on
  `allow_tenant_key_download` for that tenant.

Every download, key sheet and export needs a login session (never an API
token) and a fresh confirmation (`auth/step_up.py`), is written to the audit
log and announced by mail to the platform admins.
"""

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from resurgam_api import encryption_keys
from resurgam_api.auth.dependency import get_current_user, get_session_user
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.step_up import require_step_up
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.mail import send_email
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.encryption_key import EncryptionKey
from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment
from resurgam_api.models.inventory import BackupGroup, BackupSnapshot
from resurgam_api.models.tenancy import Customer
from resurgam_api.reporting.key_sheet import render_key_sheet_pdf
from resurgam_api.schemas.encryption_key import (
    EncryptionKeyDownloadRequest,
    EncryptionKeyExportRequest,
    EncryptionKeyImportOut,
    EncryptionKeyImportRequest,
    EncryptionKeyOut,
    EncryptionKeyUpdateRequest,
    EncryptionKeyUploadRequest,
    TenantKeyDownloadPermissionOut,
    TenantKeyDownloadPermissionRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/encryption-keys", tags=["encryption-keys"])


# --- helpers -------------------------------------------------------------------


def _is_platform_admin(user: AuthenticatedUser) -> bool:
    return user.has_role(RoleKey.PLATFORM_ADMIN)


def _require_platform_admin(user: AuthenticatedUser) -> None:
    if not _is_platform_admin(user):
        raise ResurgamApiError(code="forbidden", message="Only a platform admin can do this.", status_code=403)


def _admin_customer_ids(user: AuthenticatedUser) -> set[uuid.UUID]:
    return {
        a.scope_id
        for a in user.assignments
        if a.role == RoleKey.CUSTOMER_ADMIN and a.scope_type == ScopeType.CUSTOMER and a.scope_id is not None
    }


def _can_see(user: AuthenticatedUser, key: EncryptionKey) -> bool:
    return _is_platform_admin(user) or (key.customer_id is not None and key.customer_id in _admin_customer_ids(user))


def _can_download(db: Session, user: AuthenticatedUser, key: EncryptionKey) -> bool:
    if _is_platform_admin(user):
        return True
    if key.customer_id is None or key.customer_id not in _admin_customer_ids(user):
        return False
    customer = db.get(Customer, key.customer_id)
    return bool(customer and customer.allow_tenant_key_download)


def _get_visible_key(db: Session, user: AuthenticatedUser, key_id: uuid.UUID) -> EncryptionKey:
    key = db.get(EncryptionKey, key_id)
    if key is None or not _can_see(user, key):
        # 404, not 403: don't confirm a key exists to someone who can't see it.
        raise ResurgamApiError(code="not_found", message="Encryption key not found.", status_code=404)
    return key


def _backup_counts(db: Session) -> dict[str, int]:
    rows = (
        db.query(BackupSnapshot.key_fingerprint, func.count(BackupSnapshot.id))
        .join(BackupGroup, BackupGroup.id == BackupSnapshot.backup_group_id)
        .filter(
            BackupSnapshot.key_fingerprint.isnot(None),
            BackupGroup.superseded_at.is_(None),
            BackupGroup.removed_from_pbs_at.is_(None),
        )
        .group_by(BackupSnapshot.key_fingerprint)
        .all()
    )
    return {fp.lower(): count for fp, count in rows}


def _to_out(
    db: Session, user: AuthenticatedUser, key: EncryptionKey, counts: dict[str, int], names: dict[uuid.UUID, str]
) -> EncryptionKeyOut:
    return EncryptionKeyOut(
        id=key.id,
        fingerprint=key.fingerprint,
        label=key.label,
        customer_id=key.customer_id,
        customer_name=names.get(key.customer_id) if key.customer_id else None,
        kdf=key.kdf,
        source=key.source,
        key_created_at=key.key_created_at,
        created_at=key.created_at,
        copy_confirmed_at=key.copy_confirmed_at,
        backup_count=counts.get(key.fingerprint, 0),
        can_download=_can_download(db, user, key),
    )


def _single_out(db: Session, user: AuthenticatedUser, key: EncryptionKey) -> EncryptionKeyOut:
    names = {c.id: c.name for c in db.query(Customer).all()}
    return _to_out(db, user, key, _backup_counts(db), names)


def _audit(
    db: Session,
    request: Request,
    user: AuthenticatedUser,
    action: str,
    *,
    target_type: str = "encryption_key",
    target_id: str | None,
    **detail,
) -> None:
    db.add(
        AuditEvent(
            actor_user_id=user.user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            correlation_id=getattr(request.state, "correlation_id", None),
            detail=detail,
        )
    )


def _check_customer(db: Session, customer_id: uuid.UUID | None) -> None:
    if customer_id is not None and db.get(Customer, customer_id) is None:
        raise ResurgamApiError(code="customer_not_found", message="That tenant does not exist.", status_code=400)


def _keyfile_error(exc: encryption_keys.KeyfileError) -> ResurgamApiError:
    status = 409 if exc.code == "key_exists" else 400
    return ResurgamApiError(code=exc.code, message=exc.message, status_code=status)


def _notify_platform_admins(db: Session, settings: Settings, user: AuthenticatedUser, what: str) -> None:
    """Best effort: a key leaving RESURGAM is announced to every platform admin,
    so a misused account is noticed. A mail failure never blocks the download --
    the audit log entry is the record that counts."""
    recipients = [
        row[0]
        for row in db.query(User.email)
        .join(UserRoleAssignment, UserRoleAssignment.user_id == User.id)
        .join(Role, Role.id == UserRoleAssignment.role_id)
        .filter(Role.key == RoleKey.PLATFORM_ADMIN.value, User.disabled.is_(False), User.is_breakglass.is_(False))
        .distinct()
        .all()
        if row[0]
    ]
    subject = "RESURGAM: encryption key downloaded"
    body = (
        f"{user.display_name} <{user.email}> {what} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC.\n\n"
        "If this was not expected, check the audit log and treat the key as compromised: create a new key, "
        "set it on the PVE storage and run new backups.\n"
    )
    for to in recipients:
        try:
            send_email(db, settings, to=to, subject=subject, body_text=body)
        except Exception:
            logger.warning("could not send key download notice to a platform admin", exc_info=True)
            return


def _download_prepare(
    db: Session,
    settings: Settings,
    request: Request,
    user: AuthenticatedUser,
    key_id: uuid.UUID,
    body: EncryptionKeyDownloadRequest,
) -> tuple[EncryptionKey, str]:
    key = _get_visible_key(db, user, key_id)
    if not _can_download(db, user, key):
        raise ResurgamApiError(
            code="key_download_not_allowed",
            message="A platform admin has not allowed this tenant's admins to download their keys.",
            status_code=403,
        )
    method = require_step_up(db, settings, request, user, body.confirmation)
    request.state.step_up_method = method
    return key, encryption_keys.keyfile_content(db, settings, key)


def _safe_name(fingerprint: str) -> str:
    return fingerprint.replace(":", "")[:16]


# --- list / upload / update / delete ---------------------------------------------


@router.get("", response_model=list[EncryptionKeyOut])
def list_encryption_keys(
    db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> list[EncryptionKeyOut]:
    """The keys the caller may see: every key for a platform admin, the keys
    assigned to their own tenant for a tenant admin, none for anyone else. Key
    material is never part of this list."""
    keys = db.query(EncryptionKey).order_by(EncryptionKey.created_at).all()
    counts = _backup_counts(db)
    names = {c.id: c.name for c in db.query(Customer).all()}
    return [_to_out(db, user, k, counts, names) for k in keys if _can_see(user, k)]


@router.get("/missing", response_model=list[str])
def list_missing_fingerprints(
    db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> list[str]:
    """Platform admins: fingerprints that current backups are encrypted with but
    that are not in the store -- those backups cannot be indexed, browsed or
    restored by RESURGAM until the key is uploaded."""
    _require_platform_admin(user)
    return encryption_keys.fingerprints_without_key(db, list(_backup_counts(db)))


@router.post("", response_model=EncryptionKeyOut, status_code=201)
def upload_encryption_key(
    body: EncryptionKeyUploadRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> EncryptionKeyOut:
    """Adds a PBS keyfile. Its fingerprint (read from the file) decides which
    backups it is used for. 409 `key_exists` if that fingerprint is stored."""
    _require_platform_admin(user)
    _check_customer(db, body.customer_id)
    try:
        key = encryption_keys.store_key(
            db, settings, body.keyfile_content, label=body.label, customer_id=body.customer_id, user_id=user.user_id
        )
    except encryption_keys.KeyfileError as exc:
        raise _keyfile_error(exc) from exc
    _audit(db, request, user, "encryption_key_uploaded", target_id=str(key.id), fingerprint=key.fingerprint)
    db.commit()
    return _single_out(db, user, key)


@router.patch("/{key_id}", response_model=EncryptionKeyOut)
def update_encryption_key(
    key_id: uuid.UUID,
    body: EncryptionKeyUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> EncryptionKeyOut:
    """Changes the label or the tenant assignment. The key itself never changes."""
    _require_platform_admin(user)
    key = _get_visible_key(db, user, key_id)
    if body.label is not None:
        key.label = body.label.strip()
    if body.clear_customer:
        key.customer_id = None
    elif body.customer_id is not None:
        _check_customer(db, body.customer_id)
        key.customer_id = body.customer_id
    _audit(
        db,
        request,
        user,
        "encryption_key_updated",
        target_id=str(key.id),
        fingerprint=key.fingerprint,
        label=key.label,
        customer_id=str(key.customer_id) if key.customer_id else None,
    )
    db.commit()
    return _single_out(db, user, key)


@router.delete("/{key_id}", status_code=204)
def delete_encryption_key(
    key_id: uuid.UUID,
    request: Request,
    force: bool = Query(
        default=False,
        description="Delete even though current backups are encrypted with this key. RESURGAM then cannot read "
        "them any more until the key is uploaded again.",
    ),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_session_user),
) -> Response:
    """Removes a key from RESURGAM (not from PVE or PBS). 409 `key_in_use` while
    current backups use it, unless `force=true`."""
    _require_platform_admin(user)
    key = _get_visible_key(db, user, key_id)
    in_use = _backup_counts(db).get(key.fingerprint, 0)
    if in_use and not force:
        raise ResurgamApiError(
            code="key_in_use",
            message=f"{in_use} current backups are encrypted with this key. Without it RESURGAM cannot read them.",
            status_code=409,
            details={"backup_count": in_use},
        )
    _audit(
        db, request, user, "encryption_key_deleted", target_id=str(key.id), fingerprint=key.fingerprint, forced=force
    )
    encryption_keys.delete_key(db, settings, key)
    db.commit()
    return Response(status_code=204)


# --- download / key sheet / confirm copy ------------------------------------------


@router.post(
    "/{key_id}/download",
    responses={200: {"content": {"application/json": {}}, "description": "The keyfile, exactly as stored."}},
)
def download_encryption_key(
    key_id: uuid.UUID,
    body: EncryptionKeyDownloadRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_session_user),
) -> Response:
    """Returns the keyfile itself (PBS JSON format; usable with
    `proxmox-backup-client --keyfile` and as a PVE storage's encryption key).
    Needs a fresh confirmation; audited and announced to the platform admins."""
    key, content = _download_prepare(db, settings, request, user, key_id, body)
    _audit(
        db,
        request,
        user,
        "encryption_key_downloaded",
        target_id=str(key.id),
        fingerprint=key.fingerprint,
        confirmed_by=request.state.step_up_method,
    )
    db.commit()
    _notify_platform_admins(db, settings, user, f"downloaded encryption key {key.fingerprint}")
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="pbs-key-{_safe_name(key.fingerprint)}.json"'},
    )


@router.post(
    "/{key_id}/key-sheet",
    responses={200: {"content": {"application/pdf": {}}, "description": "One printable A4 page."}},
)
def download_key_sheet(
    key_id: uuid.UUID,
    body: EncryptionKeyDownloadRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_session_user),
) -> Response:
    """A printable PDF with the fingerprint, the full keyfile as text and as a QR
    code, and restore steps -- the paper copy to file away. Same confirmation,
    audit and mail as the keyfile download."""
    key, content = _download_prepare(db, settings, request, user, key_id, body)
    customer = db.get(Customer, key.customer_id) if key.customer_id else None
    pdf = render_key_sheet_pdf(
        fingerprint=key.fingerprint,
        keyfile=content,
        label=key.label,
        customer_name=customer.name if customer else None,
        key_created_at=key.key_created_at,
        printed_at=datetime.now(UTC),
        printed_by=user.display_name or user.email,
    )
    _audit(
        db,
        request,
        user,
        "encryption_key_sheet_printed",
        target_id=str(key.id),
        fingerprint=key.fingerprint,
        confirmed_by=request.state.step_up_method,
    )
    db.commit()
    _notify_platform_admins(db, settings, user, f"downloaded the key sheet of encryption key {key.fingerprint}")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="pbs-key-sheet-{_safe_name(key.fingerprint)}.pdf"'},
    )


@router.post("/{key_id}/confirm-copy", response_model=EncryptionKeyOut)
def confirm_key_copy(
    key_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_session_user),
) -> EncryptionKeyOut:
    """Records that a copy of this key (key sheet or keyfile) is stored safely
    outside RESURGAM. Clears the 'key copy not confirmed' alarm."""
    key = _get_visible_key(db, user, key_id)
    if not _can_download(db, user, key):
        raise ResurgamApiError(
            code="key_download_not_allowed",
            message="Only someone allowed to download this key can confirm a copy of it.",
            status_code=403,
        )
    key.copy_confirmed_at = datetime.now(UTC)
    key.copy_confirmed_by_user_id = user.user_id
    _audit(db, request, user, "encryption_key_copy_confirmed", target_id=str(key.id), fingerprint=key.fingerprint)
    db.commit()
    return _single_out(db, user, key)


# --- export / import ------------------------------------------------------------------


@router.post(
    "/export",
    responses={200: {"content": {"application/json": {}}, "description": "The passphrase-encrypted export file."}},
)
def export_encryption_keys(
    body: EncryptionKeyExportRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_session_user),
) -> Response:
    """Platform admins: every stored key in one file, encrypted with the given
    passphrase (scrypt + AES-256-GCM) -- for moving to a new RESURGAM or keeping
    an offline copy. Import it with POST `/encryption-keys/import`."""
    _require_platform_admin(user)
    method = require_step_up(db, settings, request, user, body.confirmation)
    keys = db.query(EncryptionKey).order_by(EncryptionKey.created_at).all()
    entries = [
        {
            "fingerprint": k.fingerprint,
            "label": k.label,
            "keyfile": encryption_keys.keyfile_content(db, settings, k),
        }
        for k in keys
    ]
    try:
        document = encryption_keys.export_keys(entries, body.passphrase)
    except encryption_keys.KeyfileError as exc:
        raise _keyfile_error(exc) from exc
    _audit(
        db,
        request,
        user,
        "encryption_keys_exported",
        target_id=None,
        key_count=len(entries),
        fingerprints=[e["fingerprint"] for e in entries],
        confirmed_by=method,
    )
    db.commit()
    _notify_platform_admins(db, settings, user, f"exported all {len(entries)} encryption keys")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    return Response(
        content=document,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="resurgam-encryption-keys-{stamp}.json"'},
    )


@router.post("/import", response_model=EncryptionKeyImportOut)
def import_encryption_keys(
    body: EncryptionKeyImportRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_session_user),
) -> EncryptionKeyImportOut:
    """Platform admins: adds the keys from a RESURGAM key export. Keys already
    stored (same fingerprint) are skipped. Tenant assignments are not part of an
    export -- assign tenants again afterwards."""
    _require_platform_admin(user)
    try:
        entries = encryption_keys.read_export(body.export_content, body.passphrase)
    except encryption_keys.KeyfileError as exc:
        raise _keyfile_error(exc) from exc
    imported: list[str] = []
    skipped: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("keyfile"), str):
            continue
        try:
            key = encryption_keys.store_key(
                db,
                settings,
                entry["keyfile"],
                label=str(entry.get("label") or ""),
                user_id=user.user_id,
                source="import",
            )
        except encryption_keys.KeyfileError as exc:
            if exc.code == "key_exists":
                skipped.append(str(entry.get("fingerprint") or ""))
                continue
            raise _keyfile_error(exc) from exc
        imported.append(key.fingerprint)
    _audit(db, request, user, "encryption_keys_imported", target_id=None, imported=imported, skipped=skipped)
    db.commit()
    return EncryptionKeyImportOut(imported=imported, skipped=skipped)


# --- per-tenant download permission ------------------------------------------------------


@router.get("/tenants/{customer_id}/download-permission", response_model=TenantKeyDownloadPermissionOut)
def get_tenant_download_permission(
    customer_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> TenantKeyDownloadPermissionOut:
    """Whether this tenant's admins may download their own tenant's keys.
    Readable by platform admins and by that tenant's admins."""
    if not _is_platform_admin(user) and customer_id not in _admin_customer_ids(user):
        raise ResurgamApiError(code="not_found", message="Not found.", status_code=404)
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise ResurgamApiError(code="not_found", message="Not found.", status_code=404)
    return TenantKeyDownloadPermissionOut(customer_id=customer.id, allowed=customer.allow_tenant_key_download)


@router.put("/tenants/{customer_id}/download-permission", response_model=TenantKeyDownloadPermissionOut)
def set_tenant_download_permission(
    customer_id: uuid.UUID,
    body: TenantKeyDownloadPermissionRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_session_user),
) -> TenantKeyDownloadPermissionOut:
    """Platform admins: allow or forbid this tenant's admins to download the keys
    assigned to their tenant. Off by default. Audited."""
    _require_platform_admin(user)
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise ResurgamApiError(code="not_found", message="Not found.", status_code=404)
    customer.allow_tenant_key_download = body.allowed
    _audit(
        db,
        request,
        user,
        "tenant_key_download_permission_set",
        target_type="customer",
        target_id=str(customer.id),
        allowed=body.allowed,
    )
    db.commit()
    return TenantKeyDownloadPermissionOut(customer_id=customer.id, allowed=customer.allow_tenant_key_download)
