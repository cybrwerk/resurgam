"""Classified connection check (connectivity/health.py) -- mocked DNS + HTTP only."""

import ssl

import httpx
import pytest

from resurgam_api.connectivity.health import apply_health_result, check_pbs_health, check_pve_health
from resurgam_api.connectivity.models import TrustConfig
from resurgam_api.connectivity.required_privileges import (
    PBS_REQUIRED_PRIVILEGES,
    PVE_REQUIRED_PRIVILEGES,
    missing_privileges,
)
from resurgam_api.models.pbs import ConnectionHealthStatus, TlsTrustMode
from resurgam_api.net.tls_pinning import TlsFingerprintMismatchError
from tests.proxmox_fakes import FakeProxmox, FakeResponse, all_privileges

CA = TrustConfig(trust_mode=TlsTrustMode.CA)
PINNED = TrustConfig(trust_mode=TlsTrustMode.FINGERPRINT, fingerprint_sha256="aa" * 32)
VERSION = ("GET", "/api2/json/version")
PERMS = ("GET", "/api2/json/access/permissions")


def test_ok_with_all_privileges_pve(monkeypatch):
    fake = FakeProxmox(
        routes={
            VERSION: FakeResponse({"data": {"version": "9.0.3"}}),
            PERMS: FakeResponse({"data": all_privileges("pve")}),
        }
    ).install(monkeypatch)
    result = check_pve_health("pve.example", 8006, CA, "root@pam!resurgam=uuid")
    assert result.status == ConnectionHealthStatus.OK
    assert result.reachable and result.permissions_checked
    assert result.version == "9.0.3"
    assert result.resolved_ip == "192.0.2.10"
    assert fake.calls[0][2]["Authorization"] == "PVEAPIToken=root@pam!resurgam=uuid"


def test_pbs_uses_pbs_auth_scheme(monkeypatch):
    fake = FakeProxmox(
        routes={
            VERSION: FakeResponse({"data": {"version": "4.0"}}),
            PERMS: FakeResponse({"data": all_privileges("pbs")}),
        }
    ).install(monkeypatch)
    result = check_pbs_health("pbs.example", 8007, CA, "root@pam!resurgam:uuid")
    assert result.status == ConnectionHealthStatus.OK
    assert fake.calls[0][2]["Authorization"] == "PBSAPIToken=root@pam!resurgam:uuid"


def test_dns_failure_is_unreachable(monkeypatch):
    FakeProxmox(resolve_to=None).install(monkeypatch)
    result = check_pve_health("nope.invalid", 8006, CA, "t")
    assert result.status == ConnectionHealthStatus.UNREACHABLE
    assert "nope.invalid" in result.message and "resolve" in result.message
    assert result.resolved_ip is None
    assert not result.reachable


def test_tcp_refused_is_unreachable_with_ip_and_port(monkeypatch):
    FakeProxmox(raise_on={VERSION: httpx.ConnectError("[Errno 111] Connection refused")}).install(monkeypatch)
    result = check_pbs_health("pbs.example", 8007, CA, "t")
    assert result.status == ConnectionHealthStatus.UNREACHABLE
    assert "192.0.2.10" in result.message and "8007" in result.message


def test_timeout_is_unreachable(monkeypatch):
    FakeProxmox(raise_on={VERSION: httpx.ConnectTimeout("timed out")}).install(monkeypatch)
    result = check_pbs_health("pbs.example", 8007, CA, "t")
    assert result.status == ConnectionHealthStatus.UNREACHABLE
    assert "timed out" in result.message


def test_untrusted_ca_certificate_is_tls_mismatch(monkeypatch):
    error = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate")
    error.__cause__ = ssl.SSLCertVerificationError("self-signed certificate")
    FakeProxmox(raise_on={VERSION: error}).install(monkeypatch)
    result = check_pve_health("pve.example", 8006, CA, "t")
    assert result.status == ConnectionHealthStatus.TLS_MISMATCH
    assert "fingerprint" in result.message


def test_pinned_fingerprint_mismatch_is_tls_mismatch(monkeypatch):
    from resurgam_api.connectivity import health as health_module

    def mismatch(host, port, expected, timeout=5.0):
        raise TlsFingerprintMismatchError(host, port, expected, "bb" * 32)

    monkeypatch.setattr(health_module, "verify_pinned_fingerprint", mismatch)
    fake = FakeProxmox().install(monkeypatch)
    result = check_pbs_health("pbs.example", 8007, PINNED, "t")
    assert result.status == ConnectionHealthStatus.TLS_MISMATCH
    assert "bb" * 32 in result.message
    assert fake.calls == []  # no request sent with an untrusted certificate


def test_pinned_ok_skips_httpx_verification(monkeypatch):
    from resurgam_api.connectivity import health as health_module

    monkeypatch.setattr(health_module, "verify_pinned_fingerprint", lambda *a, **kw: None)
    seen = {}

    def version(url, kwargs):
        seen["verify"] = kwargs["verify"]
        return FakeResponse({"data": {"version": "4.0"}})

    FakeProxmox(routes={VERSION: version, PERMS: FakeResponse({"data": all_privileges("pbs")})}).install(monkeypatch)
    assert check_pbs_health("pbs.example", 8007, PINNED, "t").status == ConnectionHealthStatus.OK
    assert seen["verify"] is False


def test_http_401_is_auth_failed(monkeypatch):
    FakeProxmox(routes={VERSION: FakeResponse(None, 401, "authentication failure")}).install(monkeypatch)
    result = check_pve_health("pve.example", 8006, CA, "root@pam!t:wrong-separator")
    assert result.status == ConnectionHealthStatus.AUTH_FAILED
    assert "token ID or secret rejected" in result.message
    assert not result.reachable


def test_http_403_on_version_is_missing_privileges(monkeypatch):
    FakeProxmox(routes={VERSION: FakeResponse(None, 403, "forbidden")}).install(monkeypatch)
    assert check_pbs_health("pbs.example", 8007, CA, "t").status == ConnectionHealthStatus.MISSING_PRIVILEGES


def test_missing_privileges_are_listed(monkeypatch):
    privileges = all_privileges("pve")
    del privileges["/"]["SDN.Allocate"]
    del privileges["/"]["VM.GuestAgent.Audit"]
    FakeProxmox(
        routes={VERSION: FakeResponse({"data": {"version": "9"}}), PERMS: FakeResponse({"data": privileges})}
    ).install(monkeypatch)
    result = check_pve_health("pve.example", 8006, CA, "t")
    assert result.status == ConnectionHealthStatus.MISSING_PRIVILEGES
    assert set(result.missing_privileges) == {"SDN.Allocate", "VM.GuestAgent.Audit"}
    assert result.reachable
    assert "SDN.Allocate" in result.message


def test_unreadable_permissions_still_ok_but_says_so(monkeypatch):
    FakeProxmox(
        routes={VERSION: FakeResponse({"data": {"version": "9"}}), PERMS: FakeResponse(None, 500, "x")}
    ).install(monkeypatch)
    result = check_pve_health("pve.example", 8006, CA, "t")
    assert result.status == ConnectionHealthStatus.OK
    assert not result.permissions_checked
    assert "not verified" in result.message


def test_check_privileges_false_skips_permissions_call(monkeypatch):
    fake = FakeProxmox(routes={VERSION: FakeResponse({"data": {"version": "9"}})}).install(monkeypatch)
    result = check_pve_health("pve.example", 8006, CA, "t", check_privileges=False)
    assert result.status == ConnectionHealthStatus.OK
    assert fake.called(*PERMS) == []


def test_unexpected_status_is_unreachable(monkeypatch):
    FakeProxmox(routes={VERSION: FakeResponse(None, 595, "no route")}).install(monkeypatch)
    result = check_pve_health("pve.example", 8006, CA, "t")
    assert result.status == ConnectionHealthStatus.UNREACHABLE
    assert "595" in result.message


def test_apply_health_result_sets_row_fields(monkeypatch):
    FakeProxmox(routes={VERSION: FakeResponse(None, 401, "x")}).install(monkeypatch)
    result = check_pbs_health("pbs.example", 8007, CA, "t")

    class Row:
        last_health_check_at = None
        last_health_status = "unknown"
        version = "old"

    row = Row()
    apply_health_result(row, result)
    assert row.last_health_status == "auth_failed"
    assert row.last_health_check_at == result.checked_at
    assert row.version == "old"  # unknown version leaves the stored one alone


@pytest.mark.parametrize(
    ("privileges", "expected_missing"),
    [
        ({"/": {"Sys.Audit": True}, "/storage": {}}, {"Datastore.Allocate", "Datastore.AllocateSpace"}),
        ({"/": {}, "/storage": {"Datastore.Allocate": True, "Datastore.AllocateSpace": True}}, set()),
    ],
)
def test_missing_privileges_uses_most_specific_listed_path(privileges, expected_missing):
    storage = tuple(p for p in PVE_REQUIRED_PRIVILEGES if p.path == "/storage")
    assert set(missing_privileges(privileges, storage)) == expected_missing


def test_pbs_list_contains_sync_job_privileges():
    names = {p.name for p in PBS_REQUIRED_PRIVILEGES}
    assert {"Remote.Read", "Datastore.Prune", "Datastore.Backup"} <= names
