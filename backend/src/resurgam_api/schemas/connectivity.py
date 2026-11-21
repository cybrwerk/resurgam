from datetime import datetime

from pydantic import BaseModel, Field

from resurgam_api.connectivity.token_format import (
    Product,
    assemble,
    normalize_combined,
    token_id_error,
    token_secret_error,
)
from resurgam_api.models.pbs import ConnectionHealthStatus


class TlsProbeRequest(BaseModel):
    host: str = Field(description="Host name or IP address of the PBS/PVE server (no scheme, no port).")
    port: int = Field(gt=0, le=65535, description="HTTPS API port: 8007 for PBS, 8006 for PVE.")


class TlsProbeResponse(BaseModel):
    fingerprint_sha256: str = Field(
        description="SHA-256 fingerprint (lowercase hex) of the server's current TLS leaf certificate, to confirm "
        "and pin."
    )


class TestConnectionResponse(BaseModel):
    """Classified result of a connection test (same classification as the
    periodic heartbeat, see `connectivity.health`). Always HTTP 200 -- the
    outcome is in `status`."""

    reachable: bool = Field(
        description="True when the API answered and accepted the token (status `ok` or `missing_privileges`)."
    )
    status: ConnectionHealthStatus = Field(
        default=ConnectionHealthStatus.UNKNOWN,
        description=(
            "Classified outcome: `ok`; `unreachable` (DNS/TCP failure or unexpected HTTP status); `tls_mismatch` "
            "(pinned fingerprint differs or certificate not trusted); `auth_failed` (HTTP 401: token ID or secret "
            "rejected); `missing_privileges` (token lacks required privileges, see `missing_privileges`). "
            "Also stored as the entry's `last_health_status`."
        ),
    )
    version: str | None = Field(default=None, description="Server version reported by `GET /version`.")
    error: str | None = Field(
        default=None, description="Actionable explanation of the problem; null when everything is fine."
    )
    missing_privileges: list[str] = Field(
        default_factory=list,
        description="Required privileges the token does not hold (see the `required-privileges` endpoint for why "
        "each is needed). Empty when all are present or when they could not be checked.",
    )
    permissions_checked: bool = Field(
        default=False, description="True if the token's effective permissions were read (`GET /access/permissions`)."
    )
    resolved_ip: str | None = Field(default=None, description="IP address the host name resolved to.")
    checked_at: datetime | None = Field(default=None, description="When the test ran (UTC).")


class RequiredPrivilegeOut(BaseModel):
    name: str = Field(description="Privilege name exactly as the product spells it, e.g. `Datastore.Audit`.")
    path: str = Field(description="ACL path the privilege is checked on; a grant on `/` with propagation covers it.")
    reason: str = Field(description="What RESURGAM uses the privilege for.")


class RequiredPrivilegesOut(BaseModel):
    """Everything an admin needs to prepare a token by hand -- shown in the
    add form before connecting."""

    privileges: list[RequiredPrivilegeOut] = Field(description="Every privilege RESURGAM's token needs.")
    roles: list[str] = Field(
        description="Roles RESURGAM grants its own token at `/` in credentials mode (PBS: built-in roles; PVE: "
        "the custom role `RESURGAM` holding exactly the privileges above)."
    )
    manual_commands: list[str] = Field(
        description="Shell commands (run as root on the server) to create a dedicated user's token and its ACL "
        "manually for the API-token option. Replace the example user if you use another one."
    )
    default_token_name: str = Field(description="Token name RESURGAM uses when it creates its own token.")


class TokenLoginCredentials(BaseModel):
    """Credentials mode: RESURGAM logs in ONCE with these, creates its own
    privilege-separated API token, grants it the required privileges and
    stores only that token. The password is never stored or logged.
    Accounts with two-factor authentication cannot be used."""

    username: str = Field(
        min_length=1,
        description="Account to log in with, including the realm, e.g. `root@pam`. Must not have two-factor "
        "authentication enabled and must be allowed to create tokens and change permissions.",
    )
    password: str = Field(
        min_length=1, description="Password of that account. Used for this one request only, never stored."
    )
    token_name: str = Field(
        default="resurgam",
        min_length=1,
        description="Name of the token RESURGAM creates for that user (letters, digits, `.`, `-`, `_`). Fails "
        "with `token_exists` if the user already has a token with this name.",
    )


TOKEN_ID_DESCRIPTION = (
    "API token ID in the format `user@realm!tokenname`, e.g. `{example}`. Find it in the {gui} GUI under "
    "{location}. Use together with `token_secret`."
)
TOKEN_SECRET_DESCRIPTION = (
    "API token secret (a UUID such as `d63e505a-e3ec-449a-9bc7-1da610d4ccde`), shown only once when the token is "
    "created. Never returned by any endpoint."
)
API_TOKEN_DEPRECATED_DESCRIPTION = (
    "DEPRECATED -- use `token_id` + `token_secret` (or `credentials`). Combined token "
    "`user@realm!tokenname{sep}secret`; a wrong separator (`:` vs `=`) is corrected automatically."
)


def resolve_token_input(
    product: Product,
    *,
    token_id: str | None,
    token_secret: str | None,
    api_token: str | None,
    has_credentials: bool,
    required: bool,
) -> None:
    """Shared validation for create/update requests: at most one token
    input style, and well-formed values. Raises ValueError with an
    actionable message (never containing the secret)."""
    styles = sum(
        [bool(token_id or token_secret), bool(api_token), has_credentials],
    )
    if styles > 1:
        raise ValueError(
            "Give the token in only one way: token_id + token_secret, or credentials (RESURGAM creates the token), "
            "or the deprecated api_token."
        )
    if required and styles == 0:
        raise ValueError(
            "An API token is required: give token_id + token_secret, or credentials so RESURGAM creates its own token."
        )
    if token_id is not None and token_id.strip():
        error = token_id_error(token_id, product)
        if error:
            raise ValueError(error)
    if token_secret is not None and token_secret.strip():
        error = token_secret_error(token_secret)
        if error:
            raise ValueError(error)
    if required and not has_credentials and not api_token:
        if not (token_id and token_id.strip()):
            raise ValueError("token_id is required together with token_secret (format user@realm!tokenname).")
        if not (token_secret and token_secret.strip()):
            raise ValueError("token_secret is required together with token_id.")


def combined_token(
    product: Product, *, token_id: str | None, token_secret: str | None, api_token: str | None
) -> str | None:
    """The string to store, or None if no complete token was given."""
    if api_token:
        return normalize_combined(api_token, product)
    if token_id and token_id.strip() and token_secret and token_secret.strip():
        return assemble(token_id, token_secret, product)
    return None
