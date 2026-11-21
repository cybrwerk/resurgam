"""Credentials mode token provisioning -- mocked HTTP only."""

import httpx
import pytest

from resurgam_api.connectivity.models import TrustConfig
from resurgam_api.connectivity.required_privileges import PBS_ROLES, PVE_REQUIRED_PRIVILEGES
from resurgam_api.connectivity.token_provisioning import TokenProvisioningError, provision_token
from resurgam_api.models.pbs import TlsTrustMode
from tests.proxmox_fakes import FakeProxmox, FakeResponse

CA = TrustConfig(trust_mode=TlsTrustMode.CA)
PASSWORD = "Sup3r-Secret-Pw!"
SECRET = "d63e505a-e3ec-449a-9bc7-1da610d4ccde"
TICKET = ("POST", "/api2/json/access/ticket")
TOKEN = "/api2/json/access/users/root@pam/token/resurgam"


def _ticket(ticket="PVE:root@pam:66AA::sig==", csrf="66AA:csrf", **extra):
    return FakeResponse({"data": {"ticket": ticket, "CSRFPreventionToken": csrf, "username": "root@pam", **extra}})


def _pve_routes(overrides=None):
    routes = {
        TICKET: _ticket(),
        ("GET", TOKEN): FakeResponse(None, 500, "no such token 'resurgam' for user 'root@pam'"),
        ("POST", TOKEN): FakeResponse(
            {"data": {"full-tokenid": "root@pam!resurgam", "value": SECRET, "info": {"privsep": 1}}}
        ),
        ("GET", "/api2/json/access/roles/RESURGAM"): FakeResponse(None, 500, "role 'RESURGAM' does not exist"),
        ("POST", "/api2/json/access/roles"): FakeResponse({"data": None}),
        ("PUT", "/api2/json/access/acl"): FakeResponse({"data": None}),
        ("DELETE", TOKEN): FakeResponse({"data": None}),
    }
    routes.update(overrides or {})
    return routes


def _pbs_routes(overrides=None):
    routes = {
        TICKET: _ticket(ticket="PBS:root@pam:66AA::sig", csrf="66AA:csrf"),
        ("GET", TOKEN): FakeResponse(None, 404, "no such token"),
        ("POST", TOKEN): FakeResponse({"data": {"tokenid": "root@pam!resurgam", "value": SECRET}}),
        ("PUT", "/api2/json/access/acl"): FakeResponse({"data": None}),
        ("DELETE", TOKEN): FakeResponse({"data": None}),
    }
    routes.update(overrides or {})
    return routes


def _assert_password_only_in_ticket_call(fake):
    for method, url, headers, data in fake.calls:
        blob = f"{url} {headers} {data}"
        if url.endswith("/access/ticket"):
            continue
        assert PASSWORD not in blob


def test_pve_success_creates_token_role_and_acl(monkeypatch):
    fake = FakeProxmox(routes=_pve_routes()).install(monkeypatch)
    result = provision_token("pve", "pve.example", 8006, CA, "root@pam", PASSWORD, "resurgam")

    assert result.token_id == "root@pam!resurgam"
    assert result.stored_token == f"root@pam!resurgam={SECRET}"
    assert result.granted == ("RESURGAM",)

    ticket_call = fake.called(*TICKET)[0]
    assert ticket_call[3] == {"username": "root@pam", "password": PASSWORD}
    token_call = fake.called("POST", TOKEN)[0]
    assert token_call[3]["privsep"] == "1"
    assert "RESURGAM" in token_call[3]["comment"]
    assert token_call[2]["CSRFPreventionToken"] == "66AA:csrf"
    assert token_call[2]["Cookie"].startswith("PVEAuthCookie=PVE%3Aroot%40pam")

    role_call = fake.called("POST", "/api2/json/access/roles")[0]
    assert role_call[3]["roleid"] == "RESURGAM"
    assert set(role_call[3]["privs"].split(",")) == {p.name for p in PVE_REQUIRED_PRIVILEGES}
    acl_call = fake.called("PUT", "/api2/json/access/acl")[0]
    assert acl_call[3] == {"path": "/", "roles": "RESURGAM", "tokens": "root@pam!resurgam", "propagate": "1"}
    assert fake.called("DELETE", TOKEN) == []
    _assert_password_only_in_ticket_call(fake)


def test_pve_existing_role_is_updated_not_created(monkeypatch):
    fake = FakeProxmox(
        routes=_pve_routes(
            {
                ("GET", "/api2/json/access/roles/RESURGAM"): FakeResponse({"data": {"VM.Audit": 1}}),
                ("PUT", "/api2/json/access/roles/RESURGAM"): FakeResponse({"data": None}),
            }
        )
    ).install(monkeypatch)
    provision_token("pve", "pve.example", 8006, CA, "root@pam", PASSWORD, "resurgam")
    assert fake.called("PUT", "/api2/json/access/roles/RESURGAM")
    assert fake.called("POST", "/api2/json/access/roles") == []


def test_pbs_success_grants_built_in_roles(monkeypatch):
    fake = FakeProxmox(routes=_pbs_routes()).install(monkeypatch)
    result = provision_token("pbs", "pbs.example", 8007, CA, "root@pam", PASSWORD, "resurgam")

    assert result.stored_token == f"root@pam!resurgam:{SECRET}"
    assert result.granted == PBS_ROLES
    acl_calls = fake.called("PUT", "/api2/json/access/acl")
    assert [c[3]["role"] for c in acl_calls] == list(PBS_ROLES)
    assert all(c[3]["auth-id"] == "root@pam!resurgam" and c[3]["path"] == "/" for c in acl_calls)
    assert fake.called("POST", TOKEN)[0][2]["Cookie"].startswith("PBSAuthCookie=")
    _assert_password_only_in_ticket_call(fake)


def test_wrong_password(monkeypatch):
    FakeProxmox(routes={TICKET: FakeResponse(None, 401, "authentication failure")}).install(monkeypatch)
    with pytest.raises(TokenProvisioningError) as exc:
        provision_token("pve", "pve.example", 8006, CA, "root@pam", PASSWORD, "resurgam")
    assert exc.value.code == "credentials_rejected"
    assert PASSWORD not in exc.value.message


@pytest.mark.parametrize(
    ("product", "ticket"),
    [
        ("pve", _ticket(ticket="PVE:!tfa!%7B%22totp%22%3Atrue%7D:66AA::sig", NeedTFA=1)),
        ("pbs", _ticket(ticket='PBS:!tfa!{"challenge":{}}:66AA::sig', csrf="invalid")),
    ],
)
def test_tfa_account_is_reported_plainly(monkeypatch, product, ticket):
    fake = FakeProxmox(routes={TICKET: ticket}).install(monkeypatch)
    with pytest.raises(TokenProvisioningError) as exc:
        provision_token(product, "host.example", 8006, CA, "root@pam", PASSWORD, "resurgam")
    assert exc.value.code == "tfa_required"
    assert "two-factor" in exc.value.message
    assert len(fake.calls) == 1  # nothing attempted after the partial ticket


def test_token_name_exists(monkeypatch):
    fake = FakeProxmox(routes=_pve_routes({("GET", TOKEN): FakeResponse({"data": {"privsep": 1}})})).install(
        monkeypatch
    )
    with pytest.raises(TokenProvisioningError) as exc:
        provision_token("pve", "pve.example", 8006, CA, "root@pam", PASSWORD, "resurgam")
    assert exc.value.code == "token_exists"
    assert exc.value.status_code == 409
    assert "another token name" in exc.value.message
    assert fake.called("POST", TOKEN) == []


def test_token_exists_reported_by_create_call(monkeypatch):
    FakeProxmox(routes=_pbs_routes({("POST", TOKEN): FakeResponse({"message": "token already exists"}, 400)})).install(
        monkeypatch
    )
    with pytest.raises(TokenProvisioningError) as exc:
        provision_token("pbs", "pbs.example", 8007, CA, "root@pam", PASSWORD, "resurgam")
    assert exc.value.code == "token_exists"


@pytest.mark.parametrize("product", ["pve", "pbs"])
def test_acl_write_forbidden_removes_the_new_token(monkeypatch, product):
    routes = _pve_routes if product == "pve" else _pbs_routes
    fake = FakeProxmox(routes=routes({("PUT", "/api2/json/access/acl"): FakeResponse(None, 403, "forbidden")})).install(
        monkeypatch
    )
    with pytest.raises(TokenProvisioningError) as exc:
        provision_token(product, "host.example", 8006, CA, "root@pam", PASSWORD, "resurgam")
    assert exc.value.code == "permission_denied"
    assert "removed again" in exc.value.message
    assert len(fake.called("DELETE", TOKEN)) == 1


def test_pve_role_creation_forbidden(monkeypatch):
    fake = FakeProxmox(
        routes=_pve_routes({("POST", "/api2/json/access/roles"): FakeResponse(None, 403, "Permission check failed")})
    ).install(monkeypatch)
    with pytest.raises(TokenProvisioningError) as exc:
        provision_token("pve", "pve.example", 8006, CA, "root@pam", PASSWORD, "resurgam")
    assert exc.value.code == "permission_denied"
    assert "Sys.Modify" in exc.value.message
    assert fake.called("PUT", "/api2/json/access/acl") == []


def test_unreachable_host_dns(monkeypatch):
    FakeProxmox(resolve_to=None).install(monkeypatch)
    with pytest.raises(TokenProvisioningError) as exc:
        provision_token("pve", "nope.invalid", 8006, CA, "root@pam", PASSWORD, "resurgam")
    assert exc.value.code == "unreachable"
    assert exc.value.status_code == 502


def test_unreachable_host_connect_refused(monkeypatch):
    FakeProxmox(raise_on={TICKET: httpx.ConnectError("Connection refused")}).install(monkeypatch)
    with pytest.raises(TokenProvisioningError) as exc:
        provision_token("pbs", "pbs.example", 8007, CA, "root@pam", PASSWORD, "resurgam")
    assert exc.value.code == "unreachable"
    assert PASSWORD not in exc.value.message


@pytest.mark.parametrize(
    ("username", "token_name"),
    [("root", "resurgam"), ("root@pam", "bad name"), ("root@pam", "1abc")],
)
def test_invalid_input_rejected_before_any_call(monkeypatch, username, token_name):
    fake = FakeProxmox().install(monkeypatch)
    with pytest.raises(TokenProvisioningError) as exc:
        provision_token("pve", "pve.example", 8006, CA, username, PASSWORD, token_name)
    assert exc.value.code == "invalid_input"
    assert fake.calls == []
