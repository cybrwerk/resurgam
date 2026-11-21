"""PBS-instance / PVE-cluster onboarding: separate token fields, legacy
normalisation, credentials mode, required privileges, classified test --
all with mocked HTTP."""

import pytest

from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.pbs import PbsInstance
from resurgam_api.models.pve import PveCluster
from resurgam_api.models.secrets import SecretReference
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider
from tests.proxmox_fakes import FakeProxmox, FakeResponse, all_privileges

SECRET = "d63e505a-e3ec-449a-9bc7-1da610d4ccde"
PASSWORD = "Sup3r-Secret-Pw!"
ENDPOINT = {"pbs": "/api/v1/pbs-instances", "pve": "/api/v1/pve-clusters"}
MODEL = {"pbs": PbsInstance, "pve": PveCluster}
SEP = {"pbs": ":", "pve": "="}


def _payload(product, **overrides):
    payload = {
        "display_name": f"{product} one",
        "host": f"{product}.internal.example",
        "port": 8007 if product == "pbs" else 8006,
        "tls_trust_mode": "ca",
    }
    payload.update(overrides)
    return payload


def _stored(db_session, test_settings, product, entity_id):
    row = db_session.get(MODEL[product], entity_id)
    ref = db_session.get(SecretReference, row.api_token_secret_ref_id)
    db_session.refresh(ref)
    return LocalEncryptedSecretsProvider(db_session, test_settings.secrets_local_encryption_key).retrieve(
        ref.reference_key
    )


@pytest.mark.parametrize("product", ["pbs", "pve"])
def test_create_with_separate_fields_assembles_correct_separator(
    client, platform_admin_headers, db_session, test_settings, product
):
    response = client.post(
        ENDPOINT[product],
        json=_payload(product, token_id="resurgam@pam!resurgam", token_secret=SECRET),
        headers=platform_admin_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_id"] == "resurgam@pam!resurgam"
    assert SECRET not in response.text
    assert _stored(db_session, test_settings, product, body["id"]) == f"resurgam@pam!resurgam{SEP[product]}{SECRET}"

    listed = client.get(ENDPOINT[product], headers=platform_admin_headers).json()
    assert listed[0]["token_id"] == "resurgam@pam!resurgam"


@pytest.mark.parametrize(("product", "wrong"), [("pve", ":"), ("pbs", "=")])
def test_legacy_api_token_with_wrong_separator_is_normalised(
    client, platform_admin_headers, db_session, test_settings, product, wrong
):
    response = client.post(
        ENDPOINT[product],
        json=_payload(product, api_token=f"root@pam!t{wrong}{SECRET}"),
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert _stored(db_session, test_settings, product, response.json()["id"]) == f"root@pam!t{SEP[product]}{SECRET}"


@pytest.mark.parametrize("product", ["pbs", "pve"])
def test_invalid_token_id_is_explained_without_echoing_secret(client, platform_admin_headers, product):
    response = client.post(
        ENDPOINT[product],
        json=_payload(product, token_id=f"root@pam!t={SECRET}", token_secret=SECRET),
        headers=platform_admin_headers,
    )
    assert response.status_code == 422
    assert "user@realm!tokenname" in response.json()["message"]
    assert SECRET not in response.text


@pytest.mark.parametrize("product", ["pbs", "pve"])
def test_token_is_required_and_only_one_style_allowed(client, platform_admin_headers, product):
    missing = client.post(ENDPOINT[product], json=_payload(product), headers=platform_admin_headers)
    assert missing.status_code == 422
    assert "token" in missing.json()["message"]

    both = client.post(
        ENDPOINT[product],
        json=_payload(product, token_id="a@pam!b", token_secret=SECRET, api_token=f"a@pam!b:{SECRET}"),
        headers=platform_admin_headers,
    )
    assert both.status_code == 422
    assert "only one way" in both.json()["message"]

    no_secret = client.post(
        ENDPOINT[product], json=_payload(product, token_id="a@pam!b"), headers=platform_admin_headers
    )
    assert no_secret.status_code == 422
    assert "token_secret" in no_secret.json()["message"]


@pytest.mark.parametrize("product", ["pbs", "pve"])
def test_update_secret_only_keeps_token_id(client, platform_admin_headers, db_session, test_settings, product):
    created = client.post(
        ENDPOINT[product],
        json=_payload(product, token_id="root@pam!resurgam", token_secret="old-secret"),
        headers=platform_admin_headers,
    ).json()
    response = client.patch(
        f"{ENDPOINT[product]}/{created['id']}", json={"token_secret": SECRET}, headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert _stored(db_session, test_settings, product, created["id"]) == f"root@pam!resurgam{SEP[product]}{SECRET}"

    renamed = client.patch(
        f"{ENDPOINT[product]}/{created['id']}", json={"token_id": "svc@pve!other"}, headers=platform_admin_headers
    )
    assert renamed.json()["token_id"] == "svc@pve!other"
    assert _stored(db_session, test_settings, product, created["id"]) == f"svc@pve!other{SEP[product]}{SECRET}"

    untouched = client.patch(
        f"{ENDPOINT[product]}/{created['id']}",
        json={"display_name": "renamed", "token_secret": ""},
        headers=platform_admin_headers,
    )
    assert untouched.status_code == 200
    assert _stored(db_session, test_settings, product, created["id"]) == f"svc@pve!other{SEP[product]}{SECRET}"


@pytest.mark.parametrize("product", ["pbs", "pve"])
def test_required_privileges_endpoint(client, platform_admin_headers, product):
    response = client.get(f"{ENDPOINT[product]}/required-privileges", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    names = {p["name"] for p in body["privileges"]}
    assert all(p["reason"] for p in body["privileges"])
    assert body["default_token_name"] == "resurgam"
    if product == "pve":
        assert {"VM.GuestAgent.Audit", "SDN.Allocate", "Pool.Audit"} <= names
        assert body["roles"] == ["RESURGAM"]
        assert any(c.startswith("pveum role add RESURGAM") for c in body["manual_commands"])
    else:
        assert {"Remote.Read", "Datastore.Prune", "Sys.Audit"} <= names
        assert body["roles"] == ["Audit", "DatastoreAdmin", "RemoteAdmin"]
        assert any("generate-token" in c for c in body["manual_commands"])


def _credentials_routes(product):
    token_path = "/api2/json/access/users/root@pam/token/resurgam"
    value = (
        {"full-tokenid": "root@pam!resurgam", "value": SECRET}
        if product == "pve"
        else {
            "tokenid": "root@pam!resurgam",
            "value": SECRET,
        }
    )
    return {
        ("POST", "/api2/json/access/ticket"): FakeResponse(
            {"data": {"ticket": f"{product.upper()}:root@pam:66AA::sig", "CSRFPreventionToken": "66AA:c"}}
        ),
        ("GET", token_path): FakeResponse(None, 500, "no such token"),
        ("POST", token_path): FakeResponse({"data": value}),
        ("GET", "/api2/json/access/roles/RESURGAM"): FakeResponse(None, 500, "no such role"),
        ("POST", "/api2/json/access/roles"): FakeResponse({"data": None}),
        ("PUT", "/api2/json/access/acl"): FakeResponse({"data": None}),
    }


@pytest.mark.parametrize("product", ["pbs", "pve"])
def test_create_in_credentials_mode_stores_token_and_discards_password(
    client, platform_admin_headers, db_session, test_settings, monkeypatch, product
):
    FakeProxmox(routes=_credentials_routes(product)).install(monkeypatch)
    response = client.post(
        ENDPOINT[product],
        json=_payload(product, credentials={"username": "root@pam", "password": PASSWORD}),
        headers=platform_admin_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_id"] == "root@pam!resurgam"
    assert _stored(db_session, test_settings, product, body["id"]) == f"root@pam!resurgam{SEP[product]}{SECRET}"
    assert PASSWORD not in response.text
    for event in db_session.query(AuditEvent).all():
        assert PASSWORD not in str(event.detail)


@pytest.mark.parametrize("product", ["pbs", "pve"])
def test_credentials_mode_tfa_account_creates_nothing(client, platform_admin_headers, db_session, monkeypatch, product):
    routes = _credentials_routes(product)
    routes[("POST", "/api2/json/access/ticket")] = FakeResponse(
        {"data": {"ticket": f"{product.upper()}:!tfa!xyz:66AA::sig", "CSRFPreventionToken": "invalid", "NeedTFA": 1}}
    )
    FakeProxmox(routes=routes).install(monkeypatch)
    response = client.post(
        ENDPOINT[product],
        json=_payload(product, credentials={"username": "root@pam", "password": PASSWORD}),
        headers=platform_admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "tfa_required"
    assert "two-factor" in response.json()["message"]
    assert db_session.query(MODEL[product]).count() == 0


@pytest.mark.parametrize("product", ["pbs", "pve"])
def test_test_connection_classifies_401_as_auth_failed(client, platform_admin_headers, monkeypatch, product):
    created = client.post(
        ENDPOINT[product],
        json=_payload(product, token_id="root@pam!resurgam", token_secret=SECRET),
        headers=platform_admin_headers,
    ).json()
    FakeProxmox(routes={("GET", "/api2/json/version"): FakeResponse(None, 401, "authentication failure")}).install(
        monkeypatch
    )
    response = client.post(f"{ENDPOINT[product]}/{created['id']}/test-connection", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "auth_failed"
    assert body["reachable"] is False
    assert "token ID or secret rejected" in body["error"]
    stored = client.get(f"{ENDPOINT[product]}/{created['id']}", headers=platform_admin_headers).json()
    assert stored["last_health_status"] == "auth_failed"
    assert stored["last_health_check_at"] is not None


@pytest.mark.parametrize("product", ["pbs", "pve"])
def test_test_connection_reports_missing_privileges(client, platform_admin_headers, monkeypatch, product):
    created = client.post(
        ENDPOINT[product],
        json=_payload(product, token_id="root@pam!resurgam", token_secret=SECRET),
        headers=platform_admin_headers,
    ).json()
    privileges = all_privileges(product)
    del privileges["/"]["Sys.Audit"]
    FakeProxmox(
        routes={
            ("GET", "/api2/json/version"): FakeResponse({"data": {"version": "9.0"}}),
            ("GET", "/api2/json/access/permissions"): FakeResponse({"data": privileges}),
        }
    ).install(monkeypatch)
    body = client.post(f"{ENDPOINT[product]}/{created['id']}/test-connection", headers=platform_admin_headers).json()
    assert body["status"] == "missing_privileges"
    assert body["missing_privileges"] == ["Sys.Audit"]
    assert body["version"] == "9.0"


def test_live_pve_endpoint_maps_401_to_auth_failed_not_unreachable(client, platform_admin_headers, monkeypatch):
    created = client.post(
        ENDPOINT["pve"],
        json=_payload("pve", token_id="root@pam!resurgam", token_secret=SECRET),
        headers=platform_admin_headers,
    ).json()
    FakeProxmox(routes={("GET", "/api2/json/nodes"): FakeResponse(None, 401, "")}).install(monkeypatch)
    response = client.get(f"{ENDPOINT['pve']}/{created['id']}/nodes", headers=platform_admin_headers)
    assert response.status_code == 502
    assert response.json()["code"] == "pve_auth_failed"
    assert "token ID or secret rejected" in response.json()["message"]
