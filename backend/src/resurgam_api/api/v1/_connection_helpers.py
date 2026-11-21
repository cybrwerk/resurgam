"""Helpers shared by the PBS-instance and PVE-cluster endpoints: token
handling, the classified connection test response, the required-privileges
payload, and mapping upstream errors to API errors."""

import re
import uuid
from typing import Literal

from sqlalchemy.orm import Session

from resurgam_api.config import Settings
from resurgam_api.connectivity.health import HealthCheckResult
from resurgam_api.connectivity.models import TrustConfig
from resurgam_api.connectivity.required_privileges import (
    DEFAULT_TOKEN_NAME,
    PBS_REQUIRED_PRIVILEGES,
    PBS_ROLES,
    PVE_REQUIRED_PRIVILEGES,
    PVE_ROLE_NAME,
    pbs_manual_commands,
    pve_manual_commands,
)
from resurgam_api.connectivity.token_format import (
    assemble,
    normalize_combined,
    token_id_from_stored,
    token_secret_missing,
)
from resurgam_api.connectivity.token_provisioning import TokenProvisioningError, provision_token
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.secrets import SecretReference
from resurgam_api.schemas.connectivity import (
    RequiredPrivilegeOut,
    RequiredPrivilegesOut,
    TestConnectionResponse,
    TokenLoginCredentials,
)
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

Product = Literal["pbs", "pve"]
_NAME = {"pbs": "PBS", "pve": "PVE"}
_HTTP_STATUS_RE = re.compile(r"^HTTP (\d{3})\b")


def upstream_error(product: Product, error: str | None) -> ResurgamApiError:
    """Map a failed live call's error text (`"HTTP 401: ..."` etc. from
    `connectivity._http.request_json`) to a precise API error, instead of
    calling every failure "unreachable":
    - HTTP 401 -> `{product}_auth_failed` (token ID or secret rejected),
    - HTTP 403 -> `{product}_permission_denied` (token lacks a privilege),
    - anything else -> `{product}_unreachable`.
    Always HTTP 502 (an upstream problem, never RESURGAM's own 401/403,
    which the UI treats as "your session ended")."""
    name = _NAME[product]
    match = _HTTP_STATUS_RE.match(error or "")
    status = int(match.group(1)) if match else None
    if status == 401:
        return ResurgamApiError(
            code=f"{product}_auth_failed",
            message=(
                f"{name} rejected RESURGAM's API token (HTTP 401): token ID or secret rejected. Check that the "
                f"token exists and the secret is correct (edit the {name} entry to replace it)."
            ),
            status_code=502,
        )
    if status == 403:
        return ResurgamApiError(
            code=f"{product}_permission_denied",
            message=f"{name} denied the request (HTTP 403): the API token lacks a required privilege. {error}",
            status_code=502,
        )
    return ResurgamApiError(code=f"{product}_unreachable", message=error or f"{name} unreachable", status_code=502)


def health_to_response(result: HealthCheckResult) -> TestConnectionResponse:
    return TestConnectionResponse(
        reachable=result.reachable,
        status=result.status,
        version=result.version,
        error=result.message,
        missing_privileges=list(result.missing_privileges),
        permissions_checked=result.permissions_checked,
        resolved_ip=result.resolved_ip,
        checked_at=result.checked_at,
    )


def required_privileges_out(product: Product) -> RequiredPrivilegesOut:
    if product == "pbs":
        privileges, roles, commands = PBS_REQUIRED_PRIVILEGES, list(PBS_ROLES), pbs_manual_commands()
    else:
        privileges, roles, commands = PVE_REQUIRED_PRIVILEGES, [PVE_ROLE_NAME], pve_manual_commands()
    return RequiredPrivilegesOut(
        privileges=[RequiredPrivilegeOut(name=p.name, path=p.path, reason=p.reason) for p in privileges],
        roles=roles,
        manual_commands=commands,
        default_token_name=DEFAULT_TOKEN_NAME,
    )


def provision(
    product: Product, host: str, port: int, trust: TrustConfig, credentials: TokenLoginCredentials, settings: Settings
) -> str:
    """Credentials mode -> stored combined token, or a ResurgamApiError
    whose code is the provisioning error code (e.g. `tfa_required`)."""
    try:
        provisioned = provision_token(
            product,
            host,
            port,
            trust,
            credentials.username,
            credentials.password,
            credentials.token_name,
            timeout=max(settings.connectivity_timeout_seconds, 10.0),
        )
    except TokenProvisioningError as exc:
        raise ResurgamApiError(code=exc.code, message=exc.message, status_code=exc.status_code) from None
    return provisioned.stored_token


def retrieve_token(db: Session, settings: Settings, secret_ref_id: uuid.UUID) -> str | None:
    secret_ref = db.get(SecretReference, secret_ref_id)
    if secret_ref is None:
        return None
    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    try:
        return provider.retrieve(secret_ref.reference_key)
    except Exception:  # noqa: BLE001 -- a broken secret must not break listing; token_id is just informational
        return None


def stored_token_id(db: Session, settings: Settings, secret_ref_id: uuid.UUID) -> str | None:
    return token_id_from_stored(retrieve_token(db, settings, secret_ref_id))


def stored_token_secret_set(db: Session, settings: Settings, secret_ref_id: uuid.UUID) -> bool:
    """False for a secret-less placeholder (bare token ID / empty value, as
    written by a configuration import) or a missing/unreadable value."""
    return not token_secret_missing(retrieve_token(db, settings, secret_ref_id))


def updated_token(
    product: Product,
    current: str | None,
    *,
    token_id: str | None,
    token_secret: str | None,
    api_token: str | None,
) -> str | None:
    """New combined token for an update request, or None if the token is
    unchanged. A lone `token_secret` keeps the stored token ID and a lone
    `token_id` keeps the stored secret."""
    if api_token:
        return normalize_combined(api_token, product)
    new_id = token_id.strip() if token_id and token_id.strip() else None
    new_secret = token_secret.strip() if token_secret and token_secret.strip() else None
    if new_id is None and new_secret is None:
        return None
    current_id = token_id_from_stored(current)
    current_secret = current[len(current_id) + 1 :] if current and current_id else None
    final_id = new_id or current_id
    final_secret = new_secret or current_secret
    if not final_id or not final_secret:
        raise ResurgamApiError(
            code="token_incomplete",
            message=(
                "The stored token can't be split into ID and secret, so give both token_id "
                "(user@realm!tokenname) and token_secret."
            ),
            status_code=422,
        )
    return assemble(final_id, final_secret, product)
