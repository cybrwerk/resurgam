"""Configuration export/import (ADR 0024, "Configuration export/import")."""

from datetime import UTC, datetime

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from resurgam_api.auth.dependency import require_role
from resurgam_api.config import Settings, get_settings
from resurgam_api.config_backup.exporter import build_config_backup
from resurgam_api.config_backup.importer import import_config_backup
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.identity import RoleKey
from resurgam_api.schemas.config_backup import CONFIG_BACKUP_FORMAT, ConfigBackupDocument, ConfigImportResult

router = APIRouter(prefix="/config-backup", tags=["config-backup"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)


def _audit(db: Session, request: Request, *, actor_user_id, action: str, detail: dict) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            target_type="config_backup",
            target_id=None,
            correlation_id=getattr(request.state, "correlation_id", None),
            detail=detail,
        )
    )


def _filename(doc: ConfigBackupDocument) -> str:
    instance = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (doc.instance_name or "resurgam"))
    return f"resurgam-config-{instance}-{doc.exported_at.astimezone(UTC):%Y-%m-%d}.json"


@router.get(
    "",
    response_model=ConfigBackupDocument,
    summary="Export the configuration (no secrets)",
    responses={200: {"description": "The configuration document, served as a JSON file download."}},
)
def export_config_backup(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> JSONResponse:
    """`platform_admin` only. Returns a versioned JSON document (`format` 1)
    with everything an operator configured: instance settings, branding
    (logo embedded up to 512 KiB), mail and OIDC settings, customers with
    notification/file-index/3-2-1-1-0 scoping, PBS instances with their
    datastores, PVE clusters with links and Lab settings, datastore
    assignments, protection policies, pricing plans, users with role
    assignments, per-backup-group settings and Recovery Lab application
    groups. All cross-references use natural keys (slug, host + port,
    datastore name, email), not internal ids.

    **Never included:** any secret (API token secrets -- only the token ID
    is exported --, password hashes, TOTP/WebAuthn credentials, recovery
    codes, OIDC client secret, SMTP password, datastore encryption keys,
    lab-agent token), and no runtime/history data (snapshots, tasks, jobs,
    restore plans, Lab runs, reports, usage, audit log, compliance
    attestations). Where a secret existed, a `*_set` flag says so, so an
    import can list it as "to re-enter".
    """
    doc = build_config_backup(db, settings)
    _audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="config_backup_exported",
        detail={"resurgam_version": doc.resurgam_version},
    )
    db.commit()
    return JSONResponse(
        content=doc.model_dump(mode="json"),
        headers={"Content-Disposition": f'attachment; filename="{_filename(doc)}"'},
    )


@router.post(
    "/import",
    response_model=ConfigImportResult,
    summary="Import a configuration export (dry run or apply)",
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": "A document exactly as returned by `GET /config-backup`.",
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ConfigBackupDocument"}}},
        }
    },
)
def import_config_backup_endpoint(
    request: Request,
    document: dict = Body(..., include_in_schema=False),
    dry_run: bool = Query(
        default=True,
        description="true (default): validate and report what would happen, write nothing. false: apply all "
        "non-conflicting changes in one transaction.",
    ),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> ConfigImportResult:
    """`platform_admin` only. Validates the document's `format` and content,
    matches existing objects by natural keys (customer slug, PBS/PVE host +
    port, datastore name, user email, policy name per customer, pricing
    plan per customer/global, backup-group identity) and returns, per
    section, what is (or would be) created / updated / skipped / in
    conflict, with reasons. Additive: nothing missing from the document is
    deleted; conflicts are reported and left untouched.

    Secrets are never part of the document, so the result lists everything
    that must be entered again (`to_reenter`, with the page to do it on).
    Imported PBS/PVE connections report `auth_failed: secret not set` and
    are skipped by the Collector until their token secret is entered.
    Users are created without passwords: SSO users sign in via OIDC, local
    users get a password from an admin (`POST /users/{id}/set-password`).
    Safe to repeat: importing the same file again only applies what is
    still missing (e.g. backup-group settings after the first sync).
    """
    found_format = document.get("format") if isinstance(document, dict) else None
    if found_format != CONFIG_BACKUP_FORMAT:
        raise ResurgamApiError(
            code="config_backup_unsupported_format",
            message=(
                f"Unsupported configuration backup format {found_format!r}: this RESURGAM reads format "
                f'{CONFIG_BACKUP_FORMAT}. Use a file exported by GET /config-backup ("Export configuration"); a '
                "newer format needs a newer RESURGAM."
            ),
            status_code=422,
            details={"found_format": found_format, "supported_format": CONFIG_BACKUP_FORMAT},
        )
    try:
        doc = ConfigBackupDocument.model_validate(document)
    except ValidationError as exc:
        errors = [
            {"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"], "type": err["type"]}
            for err in exc.errors(include_input=False)
        ][:50]
        raise ResurgamApiError(
            code="config_backup_invalid",
            message="The configuration backup is not valid (see details.errors for the fields). Was the file edited "
            "by hand? Export it again from the source instance.",
            status_code=422,
            details={"errors": errors},
        ) from None

    result = import_config_backup(db, settings, doc, dry_run=dry_run, actor_user_id=user.user_id)
    if not dry_run:
        _audit(
            db,
            request,
            actor_user_id=user.user_id,
            action="config_backup_imported",
            detail={
                "source_resurgam_version": doc.resurgam_version,
                "source_instance_name": doc.instance_name,
                "source_exported_at": doc.exported_at.isoformat(),
                "imported_at": datetime.now(UTC).isoformat(),
                "counts": {
                    s.section: {"create": s.create, "update": s.update, "conflict": s.conflict} for s in result.sections
                },
            },
        )
        db.commit()
    return result
