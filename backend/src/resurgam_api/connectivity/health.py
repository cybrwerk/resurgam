"""Classified connection check for PBS instances and PVE clusters.

Shared by the manual "Test connection" endpoint, the automatic test the UI
runs right after create/edit, and the periodic heartbeat (collector
scheduler) -- so all three report the SAME status for the same failure.

Classification (stored in `last_health_status`, see
`models.pbs.ConnectionHealthStatus`):
- `unreachable`        DNS lookup failed, TCP connect refused/timed out, or an
                       unexpected HTTP status (message names host, resolved IP
                       and port).
- `tls_mismatch`       pinned fingerprint differs from the server's current
                       certificate, or (CA mode) the certificate isn't trusted
                       by the system CA store.
- `auth_failed`        HTTP 401: token ID or secret rejected -- or no secret stored at
                       all (imported from a configuration backup), checked
                       before any network call.
- `missing_privileges` the API answered but the token lacks one or more of
                       the privileges in `connectivity.required_privileges`
                       (or got HTTP 403 on the version call).
- `ok`                 reachable, authenticated, all required privileges held.

No function here touches the database -- `apply_health_result` copies a
result onto a `PbsInstance`/`PveCluster` row; committing is the caller's job.
"""

import socket
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from resurgam_api.connectivity.models import TrustConfig
from resurgam_api.connectivity.required_privileges import (
    PBS_REQUIRED_PRIVILEGES,
    PVE_REQUIRED_PRIVILEGES,
    RequiredPrivilege,
    missing_privileges,
)
from resurgam_api.connectivity.token_format import token_secret_missing
from resurgam_api.models.pbs import ConnectionHealthStatus, TlsTrustMode
from resurgam_api.net.tls_pinning import TlsFingerprintMismatchError, verify_pinned_fingerprint

VERSION_PATH = "/api2/json/version"


def secret_not_set_message(product: str) -> str:
    """The `auth_failed` explanation for a connection whose API token secret
    was never set (typically imported from a configuration backup, which
    never contains secrets)."""
    return (
        f"auth_failed: secret not set -- this {product} connection has no API token secret (configuration "
        f"backups never contain secrets). Edit the {product} entry and enter the token secret."
    )


PERMISSIONS_PATH = "/api2/json/access/permissions"


@dataclass(frozen=True)
class HealthCheckResult:
    """Outcome of one classified connection check.

    - `status`: one of `ConnectionHealthStatus` (never `unknown`/`degraded`).
    - `message`: human-readable, actionable explanation (None when `ok`).
    - `version`: server version string when the version call succeeded.
    - `missing_privileges`: required privilege names the token lacks (only
      filled when `status == missing_privileges` from the permission check).
    - `permissions_checked`: True if `GET /access/permissions` was read.
    - `resolved_ip`: the address the host name resolved to (None if DNS failed).
    - `checked_at`: timezone-aware UTC timestamp of the check.
    """

    status: ConnectionHealthStatus
    message: str | None = None
    version: str | None = None
    missing_privileges: tuple[str, ...] = ()
    permissions_checked: bool = False
    resolved_ip: str | None = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def reachable(self) -> bool:
        """True when the API answered and accepted the token -- i.e. the
        connection is usable, even if some privileges are missing."""
        return self.status in (ConnectionHealthStatus.OK, ConnectionHealthStatus.MISSING_PRIVILEGES)


def check_pbs_health(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    timeout: float = 5.0,
    *,
    check_privileges: bool = True,
) -> HealthCheckResult:
    """Classified connection check against a PBS instance. `token` is the
    stored combined token (`user@realm!name:secret`). Never raises for
    network/HTTP problems -- every failure becomes a `HealthCheckResult`.
    `check_privileges=False` skips `GET /access/permissions` (cheaper
    heartbeat; status is then `ok` as soon as the version call succeeds).
    """
    return _check(
        host,
        port,
        trust,
        token,
        timeout,
        product="PBS",
        auth_scheme="PBSAPIToken",
        required=PBS_REQUIRED_PRIVILEGES,
        check_privileges=check_privileges,
    )


def check_pve_health(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    timeout: float = 5.0,
    *,
    check_privileges: bool = True,
) -> HealthCheckResult:
    """Classified connection check against a PVE cluster. `token` is the
    stored combined token (`user@realm!name=secret`). Same contract as
    `check_pbs_health`.
    """
    return _check(
        host,
        port,
        trust,
        token,
        timeout,
        product="PVE",
        auth_scheme="PVEAPIToken",
        required=PVE_REQUIRED_PRIVILEGES,
        check_privileges=check_privileges,
    )


def apply_health_result(target, result: HealthCheckResult) -> None:
    """Copy a result onto a `PbsInstance` or `PveCluster` row
    (`last_health_check_at`, `last_health_status`, and `version` when
    known). Does not commit."""
    target.last_health_check_at = result.checked_at
    target.last_health_status = result.status.value
    if result.version:
        target.version = result.version


def is_certificate_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        if "CERTIFICATE_VERIFY_FAILED" in str(current):
            return True
        current = current.__cause__ or current.__context__
    return False


def resolve_host(host: str, port: int) -> tuple[str | None, str | None]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        reason = getattr(exc, "strerror", None) or str(exc)
        return None, reason
    if not infos:
        return None, "no address returned"
    return str(infos[0][4][0]), None


def _check(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    timeout: float,
    *,
    product: str,
    auth_scheme: str,
    required: tuple[RequiredPrivilege, ...],
    check_privileges: bool,
) -> HealthCheckResult:
    if token_secret_missing(token):
        # Imported from a configuration backup (secrets are never exported)
        # or otherwise never set: don't send a request that can only fail
        # with 401 -- report it as auth_failed with the fix.
        return HealthCheckResult(
            status=ConnectionHealthStatus.AUTH_FAILED,
            message=secret_not_set_message(product),
        )
    ip, dns_error = resolve_host(host, port)
    if ip is None:
        return HealthCheckResult(
            status=ConnectionHealthStatus.UNREACHABLE,
            message=(
                f"Cannot resolve host name '{host}' ({dns_error}). Check the spelling, the DNS server of the "
                f"RESURGAM host, or enter the {product} IP address instead."
            ),
        )
    where = f"{ip}:{port}" if ip == host else f"{host} ({ip}) port {port}"

    verify: bool = True
    if trust.trust_mode == TlsTrustMode.FINGERPRINT:
        if not trust.fingerprint_sha256:
            return HealthCheckResult(
                status=ConnectionHealthStatus.TLS_MISMATCH,
                message="TLS trust mode is 'fingerprint' but no fingerprint is stored. Edit the entry and confirm it.",
                resolved_ip=ip,
            )
        try:
            verify_pinned_fingerprint(host, port, trust.fingerprint_sha256, timeout=timeout)
        except TlsFingerprintMismatchError as exc:
            return HealthCheckResult(
                status=ConnectionHealthStatus.TLS_MISMATCH,
                message=(
                    f"The TLS certificate of {where} no longer matches the pinned fingerprint "
                    f"(observed {exc.actual}). If the certificate was renewed on purpose, edit the entry and "
                    "re-confirm the new fingerprint; otherwise treat it as a possible interception."
                ),
                resolved_ip=ip,
            )
        except OSError as exc:
            return _unreachable(where, product, exc, ip)
        verify = False  # trust established by the pin check above

    headers = {"Authorization": f"{auth_scheme}={token}"}
    base = f"https://{host}:{port}"
    try:
        response = httpx.get(f"{base}{VERSION_PATH}", headers=headers, timeout=timeout, verify=verify)
    except httpx.HTTPError as exc:
        if is_certificate_error(exc):
            return HealthCheckResult(
                status=ConnectionHealthStatus.TLS_MISMATCH,
                message=(
                    f"The TLS certificate of {where} is not trusted by the system CA store (self-signed {product} "
                    "certificates are the usual cause). Switch TLS trust to 'fingerprint' and confirm the "
                    "fingerprint, or install a CA-signed certificate on the server."
                ),
                resolved_ip=ip,
            )
        return _unreachable(where, product, exc, ip)

    if response.status_code == 401:
        return HealthCheckResult(
            status=ConnectionHealthStatus.AUTH_FAILED,
            message=(
                f"{product} rejected the API token (HTTP 401): token ID or secret rejected. Check that the token "
                "exists, is enabled and not expired, and that the secret is correct."
            ),
            resolved_ip=ip,
        )
    if response.status_code == 403:
        return HealthCheckResult(
            status=ConnectionHealthStatus.MISSING_PRIVILEGES,
            message=f"{product} accepted the token but denied access (HTTP 403). Grant the required privileges.",
            resolved_ip=ip,
        )
    if response.status_code != 200:
        return HealthCheckResult(
            status=ConnectionHealthStatus.UNREACHABLE,
            message=f"{product} at {where} answered with unexpected HTTP {response.status_code}: {response.text[:200]}",
            resolved_ip=ip,
        )
    version = None
    try:
        data = response.json().get("data")
        if isinstance(data, dict) and data.get("version") is not None:
            version = str(data.get("version"))
    except (ValueError, AttributeError):
        pass

    if not check_privileges:
        return HealthCheckResult(status=ConnectionHealthStatus.OK, version=version, resolved_ip=ip)

    try:
        perm_response = httpx.get(f"{base}{PERMISSIONS_PATH}", headers=headers, timeout=timeout, verify=verify)
        perm_data = perm_response.json().get("data") if perm_response.status_code == 200 else None
    except (httpx.HTTPError, ValueError, AttributeError):
        perm_data = None
    if not isinstance(perm_data, dict):
        return HealthCheckResult(
            status=ConnectionHealthStatus.OK,
            message=f"Connected, but the token's permissions could not be read from {product}; privileges not verified.",
            version=version,
            resolved_ip=ip,
        )

    missing = missing_privileges(perm_data, required)
    if missing:
        return HealthCheckResult(
            status=ConnectionHealthStatus.MISSING_PRIVILEGES,
            message=f"Connected, but the token is missing {len(missing)} required privilege(s): {', '.join(missing)}.",
            version=version,
            missing_privileges=tuple(missing),
            permissions_checked=True,
            resolved_ip=ip,
        )
    return HealthCheckResult(
        status=ConnectionHealthStatus.OK, version=version, permissions_checked=True, resolved_ip=ip
    )


def _unreachable(where: str, product: str, exc: BaseException, ip: str | None) -> HealthCheckResult:
    if isinstance(exc, httpx.TimeoutException | TimeoutError | socket.timeout):
        detail = "the connection timed out"
    else:
        detail = str(exc) or exc.__class__.__name__
    return HealthCheckResult(
        status=ConnectionHealthStatus.UNREACHABLE,
        message=(
            f"Cannot connect to {product} at {where}: {detail}. Check that the host is up, the port is correct "
            "and no firewall blocks the RESURGAM host."
        ),
        resolved_ip=ip,
    )
