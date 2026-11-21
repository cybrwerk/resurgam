import httpx
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from resurgam_api.auth.dependency import require_role
from resurgam_api.auth.oidc import resolve_effective_oidc_config
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.oidc_settings import OidcSettings
from resurgam_api.models.secrets import SecretPurpose, SecretReference
from resurgam_api.schemas.integrations import (
    OidcDiscoveryTestRequest,
    OidcDiscoveryTestResponse,
    OidcSettingsOut,
    UpdateOidcSettingsRequest,
)
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

router = APIRouter(prefix="/integrations", tags=["integrations"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)

# Manifest §6.3 defines exactly one integration type not already covered
# by its own dedicated nav item (PBS/PVE both have theirs under
# Infrastructure) -- OIDC. This is the first *singleton* secret in this
# codebase (every other SecretReference is per-row: PBS instance, PVE
# cluster, per-user TOTP), so it's stored under one fixed, well-known key
# rather than inventing a new owning-row shape for a config that only
# ever has one instance.
_OIDC_SECRET_REFERENCE_KEY = "oidc-client-secret"


def _record_audit(db: Session, request: Request, *, actor_user_id, action: str, detail=None) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            target_type="oidc_settings",
            target_id=None,
            correlation_id=getattr(request.state, "correlation_id", None),
            detail=detail,
        )
    )


@router.get("/oidc", response_model=OidcSettingsOut)
def get_oidc_settings(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings), _user=Depends(_require_platform_admin)
) -> OidcSettingsOut:
    config = resolve_effective_oidc_config(db, settings)
    return OidcSettingsOut(
        issuer=config.issuer,
        client_id=config.client_id,
        redirect_uri=config.redirect_uri,
        enabled=config.source == "database",
        client_secret_configured=bool(config.client_secret),
        source=config.source,
    )


@router.put("/oidc", response_model=OidcSettingsOut)
def update_oidc_settings(
    body: UpdateOidcSettingsRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> OidcSettingsOut:
    """Creates or updates the one `OidcSettings` row (singleton, enforced
    here by always operating on the first/only row rather than a
    provided id). `client_secret` is optional on an update -- omit it to
    keep whatever's already stored, matching the PBS-instance/PVE-cluster
    PATCH convention (`if body.api_token: rotate`) already established
    for per-row secrets.
    """
    row = db.query(OidcSettings).first()
    if row is None:
        if not body.client_secret:
            raise ResurgamApiError(
                code="client_secret_required",
                message="client_secret is required the first time OIDC settings are saved.",
                status_code=400,
            )
        secret_ref = SecretReference(
            reference_key=_OIDC_SECRET_REFERENCE_KEY,
            description="OIDC client secret",
            purpose=SecretPurpose.OIDC_CLIENT_SECRET,
        )
        db.add(secret_ref)
        db.flush()
        row = OidcSettings(
            issuer=body.issuer,
            client_id=body.client_id,
            client_secret_ref_id=secret_ref.id,
            redirect_uri=body.redirect_uri,
            enabled=body.enabled,
        )
        db.add(row)
    else:
        row.issuer = body.issuer
        row.client_id = body.client_id
        row.redirect_uri = body.redirect_uri
        row.enabled = body.enabled

    if body.client_secret:
        provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
        provider.store(_OIDC_SECRET_REFERENCE_KEY, body.client_secret)

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="oidc_settings_updated",
        detail={"enabled": body.enabled, "secret_rotated": bool(body.client_secret)},
    )
    db.commit()

    config = resolve_effective_oidc_config(db, settings)
    return OidcSettingsOut(
        issuer=config.issuer,
        client_id=config.client_id,
        redirect_uri=config.redirect_uri,
        enabled=config.source == "database",
        client_secret_configured=bool(config.client_secret),
        source=config.source,
    )


@router.post("/oidc/test", response_model=OidcDiscoveryTestResponse)
def test_oidc_discovery(
    body: OidcDiscoveryTestRequest,
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> OidcDiscoveryTestResponse:
    """Stateless -- no persistence, no `db` write -- mirrors `probe_tls`
    (`api/v1/pbs_instances.py`): test a config *before* saving it, not
    the already-saved `test-connection` pattern PBS/PVE use post-save.
    """
    url = f"{body.issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        response = httpx.get(url, timeout=settings.connectivity_timeout_seconds)
        response.raise_for_status()
        response.json()  # confirm it's actually parseable discovery metadata
    except (httpx.HTTPError, ValueError) as exc:
        return OidcDiscoveryTestResponse(reachable=False, error=str(exc))
    return OidcDiscoveryTestResponse(reachable=True)
