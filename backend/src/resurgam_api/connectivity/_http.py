"""Shared HTTP request plumbing for `pbs_api.py`/`pve_api.py` -- both
speak the same shape of API (pinned-or-CA TLS trust, a Bearer-style
token header, a `{"data": ...}` JSON envelope), differing only in the
auth header's scheme name and each individual call's method/params/
success-status set. Factored out here so a fix to TLS-preflight
behavior, timeout handling, or error-message formatting is made once,
not duplicated (and allowed to silently diverge, as `_put_data`'s
JSON-decode-failure handling already had between the two modules)
across up to 8 near-identical implementations.
"""

import httpx

from resurgam_api.connectivity.models import TrustConfig
from resurgam_api.models.pbs import TlsTrustMode
from resurgam_api.net.tls_pinning import TlsFingerprintMismatchError, verify_pinned_fingerprint


def prepare_verify(host: str, port: int, trust: TrustConfig, timeout: float) -> tuple[bool, str | None]:
    """Resolve the httpx `verify` argument for a pinned-or-CA trust config.

    For fingerprint trust, runs the raw-socket preflight pin check first
    and returns an error message on failure; the caller must not send a
    request if an error is returned. Returns (verify, error).
    """
    if trust.trust_mode != TlsTrustMode.FINGERPRINT:
        return True, None

    if not trust.fingerprint_sha256:
        return False, "fingerprint trust mode requires a fingerprint"
    try:
        verify_pinned_fingerprint(host, port, trust.fingerprint_sha256, timeout=timeout)
    except TlsFingerprintMismatchError as exc:
        return False, str(exc)
    except OSError as exc:
        return False, f"TLS preflight failed: {exc}"
    return False, None  # trust already established out-of-band; skip httpx's own verification


def request_json(
    method: str,
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    path: str,
    timeout: float,
    *,
    auth_scheme: str,
    params: dict | None = None,
    data: dict | None = None,
    ok_statuses: tuple[int, ...] = (200,),
    unwrap_data: bool = True,
) -> tuple[list | dict | str | None, str | None]:
    """Sends one API request and returns either its `data` field
    (`unwrap_data=True`, the common case) or the full parsed JSON body
    (`unwrap_data=False`, needed by `pbs_api.get_task_log`, whose real
    envelope carries a `total` count as a *sibling* of `data`), or an
    error message. `auth_scheme` is `"PVEAPIToken"` or `"PBSAPIToken"` --
    the one real difference between the two APIs this serves.
    """
    verify, prep_error = prepare_verify(host, port, trust, timeout)
    if prep_error is not None:
        return None, prep_error

    url = f"https://{host}:{port}{path}"
    headers = {"Authorization": f"{auth_scheme}={token}"}
    # Dispatched via getattr (not `httpx.request(method, ...)`) so that
    # existing tests monkeypatching `httpx.get`/`.post`/`.put`/`.delete`
    # directly keep intercepting these calls exactly as before. Each
    # verb function's real kwarg set differs (httpx.get/delete have no
    # `data` param at all) -- passed precisely per verb, matching what
    # each of the five call sites this replaces already sent. GET and
    # DELETE both take `params` (e.g. `destroy_vm`'s `purge`/`destroy-
    # unreferenced-disks` query flags on its DELETE call) -- PVE's own
    # write endpoints use query params, not a request body, for DELETE.
    send = getattr(httpx, method.lower())
    kwargs: dict = {"headers": headers, "timeout": timeout, "verify": verify}
    if method.upper() in ("GET", "DELETE"):
        kwargs["params"] = params
    else:
        kwargs["data"] = data

    try:
        response = send(url, **kwargs)
    except httpx.HTTPError as exc:
        return None, str(exc)

    if response.status_code not in ok_statuses:
        return None, f"HTTP {response.status_code}: {response.text[:200]}"

    try:
        body = response.json()
    except ValueError as exc:
        return None, f"invalid JSON response: {exc}"

    return (body if not unwrap_data else body.get("data")), None
