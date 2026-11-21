"""Credentials mode ("let RESURGAM create its own token"): log in once with an admin's
username + password, create a dedicated, privilege-separated API token,
grant it exactly the required privileges, return it -- and forget the
password.

The password is only ever a local variable passed to one HTTP request. It
is never stored, logged, put in an exception message or in audit details.

Vendor API used (verified against the products' API viewer schemas,
pve-docs/api-viewer/apidoc.js and pbs docs/api-viewer/apidoc.js, 2026-10-06):
- `POST /access/ticket` {username, password} -> {ticket, CSRFPreventionToken}.
  TFA accounts get a *partial* ticket instead: PVE adds `NeedTFA: 1` and the
  ticket payload starts with `!tfa!` (pve-access-control
  `PVE/API2/AccessControl.pm`); PBS returns a ticket containing `!tfa!` and
  `CSRFPreventionToken: "invalid"` (proxmox-auth-api `api/access.rs`,
  `api/ticket.rs`). Both are detected and reported as `tfa_required`.
  The ticket is sent as cookie `PVEAuthCookie` / `PBSAuthCookie` (percent-
  encoded; both servers URI-unescape it), writes also need the
  `CSRFPreventionToken` header.
- `GET/POST /access/users/{userid}/token/{tokenid}` -- allowed for the
  user's own tokens. PVE takes `privsep` (default 1) + `comment` and returns
  `full-tokenid` + `value`; PBS takes `comment` and returns `tokenid` +
  `value` (PBS tokens are always evaluated as intersection with the user).
- PVE: `GET /access/roles/{roleid}`, `POST /access/roles` {roleid, privs},
  `PUT /access/roles/{roleid}` {privs} (needs Sys.Modify on /access), then
  `PUT /access/acl` {path, roles, tokens, propagate} (needs Permissions.Modify).
- PBS: `PUT /access/acl` {path, role, auth-id, propagate}, once per built-in
  role ("limited to updating ACLs of the user's API tokens" without
  Permissions.Modify).
- On failure after the token was created, the token is deleted again
  (`DELETE /access/users/{userid}/token/{tokenid}`) so no orphan remains.
"""

import ssl
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from resurgam_api.connectivity.health import is_certificate_error, resolve_host
from resurgam_api.connectivity.models import TrustConfig
from resurgam_api.connectivity.required_privileges import (
    PBS_ROLES,
    PVE_REQUIRED_PRIVILEGES,
    PVE_ROLE_NAME,
    TOKEN_COMMENT,
    privilege_names,
)
from resurgam_api.connectivity.token_format import (
    EXAMPLE_USER_ID,
    TOKEN_NAME_RE,
    USER_ID_RE,
    Product,
    assemble,
)
from resurgam_api.models.pbs import TlsTrustMode
from resurgam_api.net.tls_pinning import TlsFingerprintMismatchError, verify_pinned_fingerprint


class TokenProvisioningError(Exception):
    """A provisioning failure with a stable `code`, an actionable `message`
    (never containing the password) and the HTTP status the RESURGAM API
    should answer with. Codes: invalid_input, unreachable, tls_mismatch,
    credentials_rejected, tfa_required, token_exists, permission_denied,
    provisioning_failed."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class ProvisionedToken:
    token_id: str  # user@realm!tokenname
    stored_token: str  # combined string with the product's separator
    granted: tuple[str, ...]  # roles granted at '/'


_PRODUCT_NAME = {"pbs": "PBS", "pve": "PVE"}
_COOKIE = {"pbs": "PBSAuthCookie", "pve": "PVEAuthCookie"}


class _Session:
    def __init__(self, product: Product, host: str, port: int, verify: bool, timeout: float) -> None:
        self.product = product
        self.base = f"https://{host}:{port}/api2/json"
        self.verify = verify
        self.timeout = timeout
        self.headers: dict[str, str] = {}

    def request(self, method: str, path: str, data: dict | None = None) -> httpx.Response:
        send = getattr(httpx, method.lower())
        kwargs: dict = {"headers": self.headers, "timeout": self.timeout, "verify": self.verify}
        if data is not None:
            kwargs["data"] = data
        try:
            return send(f"{self.base}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise _network_error(self.product, exc) from None


def _network_error(product: Product, exc: BaseException) -> TokenProvisioningError:
    name = _PRODUCT_NAME[product]
    if is_certificate_error(exc):
        return TokenProvisioningError(
            "tls_mismatch",
            f"The {name} TLS certificate is not trusted by the system CA store. Use TLS trust 'fingerprint' and "
            "confirm the fingerprint first.",
            502,
        )
    detail = "the connection timed out" if isinstance(exc, httpx.TimeoutException) else exc.__class__.__name__
    return TokenProvisioningError("unreachable", f"Cannot connect to {name}: {detail}.", 502)


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
        message = body.get("message") or body.get("errors") or ""
    except (ValueError, AttributeError):
        message = ""
    return str(message or response.text or "")[:200].strip()


def _prepare(product: Product, host: str, port: int, trust: TrustConfig, timeout: float) -> bool:
    name = _PRODUCT_NAME[product]
    ip, dns_error = resolve_host(host, port)
    if ip is None:
        raise TokenProvisioningError(
            "unreachable", f"Cannot resolve host name '{host}' ({dns_error}). Check it or use the IP address.", 502
        )
    if trust.trust_mode != TlsTrustMode.FINGERPRINT:
        return True
    if not trust.fingerprint_sha256:
        raise TokenProvisioningError("tls_mismatch", "TLS trust mode 'fingerprint' needs a confirmed fingerprint.")
    try:
        verify_pinned_fingerprint(host, port, trust.fingerprint_sha256, timeout=timeout)
    except TlsFingerprintMismatchError as exc:
        raise TokenProvisioningError(
            "tls_mismatch",
            f"The {name} certificate does not match the confirmed fingerprint (observed {exc.actual}).",
            502,
        ) from None
    except (OSError, ssl.SSLError) as exc:
        raise TokenProvisioningError("unreachable", f"Cannot connect to {name} at {ip}:{port}: {exc}.", 502) from None
    return False


def _login(session: _Session, username: str, password: str) -> None:
    name = _PRODUCT_NAME[session.product]
    response = session.request("POST", "/access/ticket", data={"username": username, "password": password})
    if response.status_code == 401:
        raise TokenProvisioningError(
            "credentials_rejected",
            f"{name} rejected the login for '{username}'. Check user name (incl. realm, e.g. root@pam) and password.",
        )
    if response.status_code != 200:
        raise TokenProvisioningError(
            "provisioning_failed",
            f"{name} login failed with HTTP {response.status_code}: {_error_text(response)}",
            502,
        )
    try:
        data = response.json().get("data") or {}
    except (ValueError, AttributeError):
        data = {}
    ticket = data.get("ticket") if isinstance(data, dict) else None
    csrf = data.get("CSRFPreventionToken") if isinstance(data, dict) else None
    if not ticket:
        raise TokenProvisioningError("provisioning_failed", f"{name} login returned no ticket.", 502)
    if data.get("NeedTFA") or "!tfa!" in ticket or csrf == "invalid":
        raise TokenProvisioningError(
            "tfa_required",
            f"This {name} account has two-factor authentication enabled. Use the API token option instead, or an "
            "account without two-factor authentication.",
        )
    session.headers = {
        "Cookie": f"{_COOKIE[session.product]}={quote(ticket, safe='')}",
        "CSRFPreventionToken": csrf or "",
    }


def _token_path(username: str, token_name: str) -> str:
    return f"/access/users/{quote(username, safe='@')}/token/{quote(token_name, safe='')}"


def _delete_token_quietly(session: _Session, username: str, token_name: str) -> None:
    try:
        session.request("DELETE", _token_path(username, token_name))
    except TokenProvisioningError:
        pass


def _validate(product: Product, username: str, password: str, token_name: str) -> None:
    if not USER_ID_RE.match(username):
        raise TokenProvisioningError(
            "invalid_input",
            f"The user name must include the realm: user@realm, e.g. '{EXAMPLE_USER_ID[product]}'.",
            422,
        )
    if not password:
        raise TokenProvisioningError("invalid_input", "The password must not be empty.", 422)
    if not TOKEN_NAME_RE.match(token_name) or (product == "pve" and not token_name[0].isalpha()):
        raise TokenProvisioningError(
            "invalid_input",
            "The token name may only contain letters, digits, '.', '-' and '_'"
            + (" and must start with a letter" if product == "pve" else "")
            + ", e.g. 'resurgam'.",
            422,
        )


def _create_token(session: _Session, username: str, token_name: str) -> tuple[str, str]:
    name = _PRODUCT_NAME[session.product]
    path = _token_path(username, token_name)
    existing = session.request("GET", path)
    if existing.status_code == 200:
        raise TokenProvisioningError(
            "token_exists",
            f"The {name} user '{username}' already has a token named '{token_name}'. Enter that token with the API "
            "token option, choose another token name, or delete the old token in the "
            f"{name} GUI first.",
            409,
        )
    data = {"comment": TOKEN_COMMENT}
    if session.product == "pve":
        data.update({"privsep": "1", "expire": "0"})
    response = session.request("POST", path, data=data)
    if response.status_code == 403:
        raise TokenProvisioningError(
            "permission_denied", f"'{username}' is not allowed to create API tokens on {name}.", 400
        )
    if response.status_code != 200:
        text = _error_text(response)
        if "exist" in text.lower():
            raise TokenProvisioningError(
                "token_exists",
                f"The {name} user '{username}' already has a token named '{token_name}'. Use the API token option "
                "or choose another token name.",
                409,
            )
        raise TokenProvisioningError(
            "provisioning_failed", f"{name} refused to create the token (HTTP {response.status_code}): {text}", 502
        )
    try:
        body = response.json().get("data") or {}
    except (ValueError, AttributeError):
        body = {}
    secret = body.get("value") if isinstance(body, dict) else None
    token_id = (body.get("full-tokenid") or body.get("tokenid")) if isinstance(body, dict) else None
    if not secret:
        _delete_token_quietly(session, username, token_name)
        raise TokenProvisioningError("provisioning_failed", f"{name} created the token but returned no secret.", 502)
    return token_id or f"{username}!{token_name}", secret


def _acl_forbidden(name: str, username: str, what: str) -> TokenProvisioningError:
    return TokenProvisioningError(
        "permission_denied",
        f"'{username}' may not {what} on {name}, so RESURGAM could not grant its token the required privileges. "
        "The token was removed again. Use an administrator account (e.g. root@pam) or set up the token manually "
        "with the commands under 'Required permissions'.",
        400,
    )


def _grant_pve(session: _Session, username: str, token_id: str) -> tuple[str, ...]:
    privs = ",".join(privilege_names(PVE_REQUIRED_PRIVILEGES))
    role = session.request("GET", f"/access/roles/{PVE_ROLE_NAME}")
    if role.status_code == 200:
        response = session.request("PUT", f"/access/roles/{PVE_ROLE_NAME}", data={"privs": privs})
    else:
        response = session.request("POST", "/access/roles", data={"roleid": PVE_ROLE_NAME, "privs": privs})
    if response.status_code == 403:
        raise _acl_forbidden("PVE", username, f"create or update the role '{PVE_ROLE_NAME}' (needs Sys.Modify)")
    if response.status_code != 200:
        raise TokenProvisioningError(
            "provisioning_failed",
            f"PVE refused to create the role '{PVE_ROLE_NAME}' (HTTP {response.status_code}): {_error_text(response)}",
            502,
        )
    acl = session.request(
        "PUT", "/access/acl", data={"path": "/", "roles": PVE_ROLE_NAME, "tokens": token_id, "propagate": "1"}
    )
    if acl.status_code == 403:
        raise _acl_forbidden("PVE", username, "change permissions (needs Permissions.Modify)")
    if acl.status_code != 200:
        raise TokenProvisioningError(
            "provisioning_failed", f"PVE refused the ACL change (HTTP {acl.status_code}): {_error_text(acl)}", 502
        )
    return (PVE_ROLE_NAME,)


def _grant_pbs(session: _Session, username: str, token_id: str) -> tuple[str, ...]:
    for role in PBS_ROLES:
        acl = session.request(
            "PUT", "/access/acl", data={"path": "/", "role": role, "auth-id": token_id, "propagate": "1"}
        )
        if acl.status_code == 403:
            raise _acl_forbidden("PBS", username, "change permissions")
        if acl.status_code != 200:
            raise TokenProvisioningError(
                "provisioning_failed",
                f"PBS refused the ACL change for role {role} (HTTP {acl.status_code}): {_error_text(acl)}",
                502,
            )
    return PBS_ROLES


def provision_token(
    product: Product,
    host: str,
    port: int,
    trust: TrustConfig,
    username: str,
    password: str,
    token_name: str,
    timeout: float = 10.0,
) -> ProvisionedToken:
    """Run the whole credentials flow for `product` ("pbs" or "pve").
    Raises `TokenProvisioningError` on any failure; on success the returned
    `stored_token` is ready for the secrets provider."""
    username = username.strip()
    token_name = token_name.strip()
    _validate(product, username, password, token_name)
    verify = _prepare(product, host, port, trust, timeout)
    session = _Session(product, host, port, verify, timeout)
    _login(session, username, password)
    del password  # not needed beyond the ticket call
    token_id, secret = _create_token(session, username, token_name)
    try:
        granted = (
            _grant_pve(session, username, token_id) if product == "pve" else _grant_pbs(session, username, token_id)
        )
    except TokenProvisioningError:
        _delete_token_quietly(session, username, token_name)
        raise
    return ProvisionedToken(token_id=token_id, stored_token=assemble(token_id, secret, product), granted=granted)
