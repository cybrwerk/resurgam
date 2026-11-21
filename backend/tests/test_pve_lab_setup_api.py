"""Recovery Lab compute setup wizard: readiness checklist, pickers, auto-fixes,
agent install info and the agent's last-seen stamp -- all with mocked HTTP
(no real PVE is contacted)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from resurgam_api.api.v1.pve_lab_setup import LAB_AGENT_TOKEN_PLACEHOLDER, fallback_lab_agent_package_version
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.pve import PveCluster
from resurgam_api.restore_test.execution import hash_appliance_run_token
from tests.proxmox_fakes import FakeProxmox, FakeResponse, all_privileges

SECRET = "d63e505a-e3ec-449a-9bc7-1da610d4ccde"
BASE = "/api/v1/pve-clusters"


def _create_cluster(client, headers, db_session, **lab) -> str:
    created = client.post(
        BASE,
        json={
            "display_name": "Lab PVE",
            "host": "pve.lab.example",
            "port": 8006,
            "tls_trust_mode": "ca",
            "token_id": "resurgam@pve!resurgam",
            "token_secret": SECRET,
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text
    cluster_id = created.json()["id"]
    defaults = {
        "lab_sdn_zone_name": None,
        "lab_appliance_template_vmid": 9003,
        "lab_snippet_storage_id": "local",
        "lab_appliance_uplink_bridge": "vmbr0",
    }
    defaults.update(lab)
    cluster = db_session.get(PveCluster, cluster_id)
    for key, value in defaults.items():
        setattr(cluster, key, value)
    cluster.lab_agent_token_hash = hash_appliance_run_token("fixture-agent-token")
    cluster.lab_agent_last_seen_at = datetime.now(UTC) - timedelta(seconds=20)
    db_session.commit()
    return cluster_id


def _ok_routes() -> dict:
    return {
        ("GET", "/api2/json/access/permissions"): FakeResponse({"data": all_privileges("pve")}),
        ("GET", "/api2/json/nodes"): FakeResponse({"data": [{"node": "pve1", "status": "online"}]}),
        ("GET", "/api2/json/cluster/sdn/zones/resurgam"): FakeResponse(
            {"data": {"zone": "resurgam", "type": "simple"}}
        ),
        ("GET", "/api2/json/nodes/pve1/network"): FakeResponse(
            {"data": [{"iface": "vmbr0", "type": "bridge", "active": 1}, {"iface": "vmbr1", "type": "OVSBridge"}]}
        ),
        ("GET", "/api2/json/storage"): FakeResponse(
            {
                "data": [
                    {"storage": "local", "type": "dir", "content": "iso,vztmpl,backup,snippets", "path": "/var/lib/vz"},
                    {
                        "storage": "resurgam-lab-overlays",
                        "type": "dir",
                        "content": "images",
                        "path": "/var/lib/resurgam-lab-agent/overlays",
                        "nodes": "pve1",
                    },
                    {"storage": "local-lvm", "type": "lvmthin", "content": "images,rootdir"},
                ]
            }
        ),
        ("GET", "/api2/json/cluster/resources"): FakeResponse(
            {
                "data": [
                    {"vmid": 9003, "node": "pve1", "type": "qemu", "name": "lab-appliance", "template": 1},
                    {"vmid": 101, "node": "pve1", "type": "qemu", "name": "web", "template": 0},
                    {"vmid": 9100, "node": "pve1", "type": "lxc", "name": "ct-tpl", "template": 1},
                ]
            }
        ),
    }


def _items(body) -> dict:
    return {item["key"]: item for item in body["items"]}


def test_readiness_all_ok(client, platform_admin_headers, db_session, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    FakeProxmox(routes=_ok_routes()).install(monkeypatch)

    response = client.get(f"{BASE}/{cluster_id}/lab-readiness", headers=platform_admin_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert [i["key"] for i in body["items"]] == [
        "privileges",
        "sdn_zone",
        "uplink_bridge",
        "uplink_network",
        "snippet_storage",
        "overlay_storage",
        "appliance_template",
        "lab_agent",
        "backend_url",
    ]
    assert {i["key"]: i["status"] for i in body["items"]} == dict.fromkeys(_items(body), "ok")
    assert body["ready"] is True
    # Lab nodes default to the overlay storage's own node restriction.
    assert body["lab_nodes"] == ["pve1"]
    assert body["lab_nodes_source"] == "overlay_storage"
    assert _items(body)["sdn_zone"]["code"] == "zone_ok"


def test_readiness_never_writes_to_pve(client, platform_admin_headers, db_session, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    fake = FakeProxmox(routes=_ok_routes()).install(monkeypatch)
    client.get(f"{BASE}/{cluster_id}/lab-readiness", headers=platform_admin_headers)
    assert fake.calls and all(call[0] == "GET" for call in fake.calls)


def test_readiness_missing_privileges(client, platform_admin_headers, db_session, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    routes = _ok_routes()
    privileges = all_privileges("pve")
    del privileges["/"]["VM.Clone"]
    del privileges["/"]["SDN.Allocate"]
    routes[("GET", "/api2/json/access/permissions")] = FakeResponse({"data": privileges})
    FakeProxmox(routes=routes).install(monkeypatch)

    body = client.get(f"{BASE}/{cluster_id}/lab-readiness", headers=platform_admin_headers).json()

    item = _items(body)["privileges"]
    assert item["status"] == "missing"
    assert item["code"] == "privileges_missing"
    assert item["details"]["missing"] == ["VM.Clone", "SDN.Allocate"]
    assert body["ready"] is False


@pytest.mark.parametrize(
    ("zone_name", "zone_response", "code", "status"),
    [
        (
            "labzone",
            FakeResponse(None, 500, '{"message":"sdn zone \'labzone\' does not exist\\n"}'),
            "zone_not_found",
            "missing",
        ),
        (
            "vlanz",
            FakeResponse({"data": {"zone": "vlanz", "type": "vlan", "bridge": "vmbr0"}}),
            "zone_type_unsupported",
            "missing",
        ),
        (
            "simp",
            FakeResponse({"data": {"zone": "simp", "type": "simple", "nodes": "pve2"}}),
            "zone_not_on_nodes",
            "missing",
        ),
        (
            "simp",
            FakeResponse({"data": {"zone": "simp", "type": "simple", "state": "new"}}),
            "zone_pending_apply",
            "warning",
        ),
    ],
)
def test_readiness_sdn_zone_problems(
    client, platform_admin_headers, db_session, monkeypatch, zone_name, zone_response, code, status
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session, lab_sdn_zone_name=zone_name)
    routes = _ok_routes()
    routes[("GET", f"/api2/json/cluster/sdn/zones/{zone_name}")] = zone_response
    FakeProxmox(routes=routes).install(monkeypatch)

    item = _items(client.get(f"{BASE}/{cluster_id}/lab-readiness", headers=platform_admin_headers).json())["sdn_zone"]

    assert (item["status"], item["code"]) == (status, code)


def test_readiness_default_zone_absent_is_ok_because_it_is_auto_created(
    client, platform_admin_headers, db_session, monkeypatch
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    routes = _ok_routes()
    routes[("GET", "/api2/json/cluster/sdn/zones/resurgam")] = FakeResponse(
        None, 500, '{"message":"sdn zone \'resurgam\' does not exist\\n"}'
    )
    FakeProxmox(routes=routes).install(monkeypatch)

    item = _items(client.get(f"{BASE}/{cluster_id}/lab-readiness", headers=platform_admin_headers).json())["sdn_zone"]

    assert (item["status"], item["code"]) == ("ok", "default_zone_auto_created")


def test_readiness_uplink_bridge_missing_on_node_and_not_configured(
    client, platform_admin_headers, db_session, monkeypatch
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session, lab_appliance_uplink_bridge="vmbr9")
    FakeProxmox(routes=_ok_routes()).install(monkeypatch)
    item = _items(client.get(f"{BASE}/{cluster_id}/lab-readiness", headers=platform_admin_headers).json())[
        "uplink_bridge"
    ]
    assert (item["status"], item["code"]) == ("missing", "bridge_not_on_nodes")
    assert item["details"]["missing_nodes"] == ["pve1"]

    db_session.get(PveCluster, cluster_id).lab_appliance_uplink_bridge = None
    db_session.commit()
    item = _items(client.get(f"{BASE}/{cluster_id}/lab-readiness", headers=platform_admin_headers).json())[
        "uplink_bridge"
    ]
    assert (item["status"], item["code"]) == ("missing", "not_configured")


@pytest.mark.parametrize(
    ("snippet_storage", "code", "auto_fix"),
    [
        (None, "not_configured", None),
        ("nope", "storage_not_found", None),
        ("local-lvm", "storage_type_unsupported", None),
        ("nosnip", "snippets_content_missing", "add_snippets_content"),
    ],
)
def test_readiness_snippet_storage_problems(
    client, platform_admin_headers, db_session, monkeypatch, snippet_storage, code, auto_fix
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session, lab_snippet_storage_id=snippet_storage)
    routes = _ok_routes()
    storages = routes[("GET", "/api2/json/storage")]._payload["data"]
    storages.append({"storage": "nosnip", "type": "nfs", "content": "iso,backup"})
    FakeProxmox(routes=routes).install(monkeypatch)

    item = _items(client.get(f"{BASE}/{cluster_id}/lab-readiness", headers=platform_admin_headers).json())[
        "snippet_storage"
    ]

    assert (item["status"], item["code"], item["auto_fix"]) == ("missing", code, auto_fix)


def test_readiness_overlay_storage_missing_offers_creation(client, platform_admin_headers, db_session, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    routes = _ok_routes()
    storages = routes[("GET", "/api2/json/storage")]._payload["data"]
    storages[:] = [s for s in storages if s["storage"] != "resurgam-lab-overlays"]
    FakeProxmox(routes=routes).install(monkeypatch)

    body = client.get(f"{BASE}/{cluster_id}/lab-readiness", headers=platform_admin_headers).json()

    item = _items(body)["overlay_storage"]
    assert (item["status"], item["code"], item["auto_fix"]) == (
        "missing",
        "storage_not_found",
        "create_overlay_storage",
    )
    # Without an overlay storage to infer from, every online node is a lab node.
    assert body["lab_nodes_source"] == "all_online_nodes"


@pytest.mark.parametrize(
    ("entry", "code"),
    [
        ({"type": "lvmthin", "content": "images"}, "storage_type_not_dir"),
        ({"type": "dir", "content": "iso", "path": "/x"}, "images_content_missing"),
        ({"type": "dir", "content": "images", "path": "/x", "nodes": "pve2"}, "storage_not_on_nodes"),
        ({"type": "dir", "content": "images", "path": "/x", "disable": 1}, "storage_disabled"),
    ],
)
def test_readiness_overlay_storage_problems(client, platform_admin_headers, db_session, monkeypatch, entry, code):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    routes = _ok_routes()
    storages = routes[("GET", "/api2/json/storage")]._payload["data"]
    storages[:] = [s for s in storages if s["storage"] != "resurgam-lab-overlays"]
    storages.append({"storage": "resurgam-lab-overlays", **entry})
    FakeProxmox(routes=routes).install(monkeypatch)

    item = _items(client.get(f"{BASE}/{cluster_id}/lab-readiness?nodes=pve1", headers=platform_admin_headers).json())[
        "overlay_storage"
    ]

    assert (item["status"], item["code"]) == ("missing", code)


@pytest.mark.parametrize(
    ("vmid", "code", "status"),
    [
        (None, "not_configured", "missing"),
        (4242, "template_not_found", "missing"),
        (101, "not_a_template", "missing"),
        (9100, "template_not_qemu", "missing"),
    ],
)
def test_readiness_template_problems(client, platform_admin_headers, db_session, monkeypatch, vmid, code, status):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session, lab_appliance_template_vmid=vmid)
    FakeProxmox(routes=_ok_routes()).install(monkeypatch)
    item = _items(client.get(f"{BASE}/{cluster_id}/lab-readiness", headers=platform_admin_headers).json())[
        "appliance_template"
    ]
    assert (item["status"], item["code"]) == (status, code)


def test_readiness_template_on_other_node(client, platform_admin_headers, db_session, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    routes = _ok_routes()
    routes[("GET", "/api2/json/cluster/resources")] = FakeResponse(
        {"data": [{"vmid": 9003, "node": "pve2", "type": "qemu", "name": "tpl", "template": 1}]}
    )
    FakeProxmox(routes=routes).install(monkeypatch)
    item = _items(client.get(f"{BASE}/{cluster_id}/lab-readiness", headers=platform_admin_headers).json())[
        "appliance_template"
    ]
    assert (item["status"], item["code"]) == ("missing", "template_not_on_lab_node")


def test_readiness_failing_pve_call_marks_only_dependent_items_unknown(
    client, platform_admin_headers, db_session, monkeypatch
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    routes = _ok_routes()
    routes[("GET", "/api2/json/storage")] = FakeResponse(None, 500, "storage.cfg locked")
    routes[("GET", "/api2/json/cluster/resources")] = FakeResponse(None, 403, "Permission check failed")
    FakeProxmox(routes=routes).install(monkeypatch)

    body = client.get(f"{BASE}/{cluster_id}/lab-readiness", headers=platform_admin_headers).json()
    items = _items(body)

    assert items["snippet_storage"]["status"] == "unknown"
    assert items["overlay_storage"]["status"] == "unknown"
    assert "storage.cfg locked" in items["overlay_storage"]["details"]["error"]
    assert items["appliance_template"]["status"] == "unknown"
    assert "403" in items["appliance_template"]["details"]["error"]
    for key in ("privileges", "sdn_zone", "uplink_bridge", "lab_agent", "backend_url"):
        assert items[key]["status"] == "ok", key
    assert body["ready"] is False


def test_readiness_unreachable_cluster_still_reports_local_items(
    client, platform_admin_headers, db_session, monkeypatch
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    FakeProxmox(routes={}).install(monkeypatch)  # every PVE call 404s

    items = _items(client.get(f"{BASE}/{cluster_id}/lab-readiness", headers=platform_admin_headers).json())

    for key in ("privileges", "sdn_zone", "uplink_bridge", "snippet_storage", "overlay_storage", "appliance_template"):
        assert items[key]["status"] == "unknown", key
    assert items["lab_agent"]["status"] == "ok"
    assert items["backend_url"]["status"] == "ok"


def test_readiness_lab_agent_states(client, platform_admin_headers, db_session, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    FakeProxmox(routes=_ok_routes()).install(monkeypatch)
    cluster = db_session.get(PveCluster, cluster_id)

    def agent_item():
        return _items(client.get(f"{BASE}/{cluster_id}/lab-readiness", headers=platform_admin_headers).json())[
            "lab_agent"
        ]

    cluster.lab_agent_last_seen_at = datetime.now(UTC) - timedelta(minutes=30)
    db_session.commit()
    assert (agent_item()["status"], agent_item()["code"]) == ("warning", "stale")

    cluster.lab_agent_last_seen_at = None
    db_session.commit()
    assert (agent_item()["status"], agent_item()["code"]) == ("missing", "never_seen")

    cluster.lab_agent_token_hash = None
    db_session.commit()
    assert (agent_item()["status"], agent_item()["code"]) == ("missing", "token_missing")


def test_readiness_backend_url_not_configured(client, platform_admin_headers, db_session, monkeypatch, test_settings):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    FakeProxmox(routes=_ok_routes()).install(monkeypatch)
    test_settings.recovery_lab_public_backend_url = ""
    item = _items(client.get(f"{BASE}/{cluster_id}/lab-readiness", headers=platform_admin_headers).json())[
        "backend_url"
    ]
    assert (item["status"], item["code"]) == ("missing", "not_configured")


def test_lab_agent_poll_records_last_seen(client, platform_admin_headers, db_session):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    cluster = db_session.get(PveCluster, cluster_id)
    cluster.lab_agent_last_seen_at = None
    db_session.commit()
    token = client.post(f"{BASE}/{cluster_id}/lab-agent-token", headers=platform_admin_headers).json()["token"]

    assert client.get("/api/v1/lab-agent/jobs", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    db_session.refresh(cluster)
    first = cluster.lab_agent_last_seen_at
    assert first is not None and datetime.now(UTC) - first < timedelta(seconds=10)

    # A second poll right away doesn't rewrite it (30-second resolution).
    client.get("/api/v1/lab-agent/jobs", headers={"Authorization": f"Bearer {token}"})
    db_session.refresh(cluster)
    assert cluster.lab_agent_last_seen_at == first

    listed = client.get(f"{BASE}/{cluster_id}", headers=platform_admin_headers).json()
    assert listed["lab_agent_last_seen_at"] is not None


def test_lab_agent_bad_token_does_not_record_last_seen(client, platform_admin_headers, db_session):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    cluster = db_session.get(PveCluster, cluster_id)
    cluster.lab_agent_last_seen_at = None
    db_session.commit()
    assert client.get("/api/v1/lab-agent/jobs", headers={"Authorization": "Bearer wrong"}).status_code == 401
    db_session.refresh(cluster)
    assert cluster.lab_agent_last_seen_at is None


# --- pickers ---------------------------------------------------------------


def test_pickers(client, platform_admin_headers, db_session, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    routes = _ok_routes()
    routes[("GET", "/api2/json/cluster/sdn/zones")] = FakeResponse(
        {"data": [{"zone": "vl", "type": "vlan"}, {"zone": "iso", "type": "simple", "nodes": "pve1,pve2"}]}
    )
    FakeProxmox(routes=routes).install(monkeypatch)

    zones = client.get(f"{BASE}/{cluster_id}/sdn-zones", headers=platform_admin_headers).json()
    assert zones == [
        {"zone": "iso", "type": "simple", "nodes": ["pve1", "pve2"], "pending": False, "usable_for_lab": True},
        {"zone": "vl", "type": "vlan", "nodes": None, "pending": False, "usable_for_lab": False},
    ]

    bridges = client.get(f"{BASE}/{cluster_id}/nodes/pve1/bridges", headers=platform_admin_headers).json()
    assert [b["iface"] for b in bridges] == ["vmbr0", "vmbr1"]

    storages = client.get(
        f"{BASE}/{cluster_id}/storage-config?snippets_capable=true", headers=platform_admin_headers
    ).json()
    assert [s["storage"] for s in storages] == ["local", "resurgam-lab-overlays"]
    snippets = client.get(f"{BASE}/{cluster_id}/storage-config?content=snippets", headers=platform_admin_headers).json()
    assert [s["storage"] for s in snippets] == ["local"]

    templates = client.get(f"{BASE}/{cluster_id}/templates", headers=platform_admin_headers).json()
    assert [(t["vmid"], t["guest_type"]) for t in templates] == [(9003, "qemu"), (9100, "lxc")]


def test_bridge_picker_asks_pve_for_local_bridges_only(client, platform_admin_headers, db_session, monkeypatch):
    import httpx

    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    FakeProxmox(routes=_ok_routes()).install(monkeypatch)
    seen = {}
    original = httpx.get

    def spy(url, **kwargs):
        seen[url] = kwargs.get("params")
        return original(url, **kwargs)

    monkeypatch.setattr(httpx, "get", spy)
    client.get(f"{BASE}/{cluster_id}/nodes/pve1/bridges", headers=platform_admin_headers)
    assert seen["https://pve.lab.example:8006/api2/json/nodes/pve1/network"] == {"type": "any_local_bridge"}


def test_picker_upstream_error(client, platform_admin_headers, db_session, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    FakeProxmox(routes={("GET", "/api2/json/cluster/sdn/zones"): FakeResponse(None, 403, "denied")}).install(
        monkeypatch
    )
    response = client.get(f"{BASE}/{cluster_id}/sdn-zones", headers=platform_admin_headers)
    assert response.status_code == 502
    assert response.json()["code"] == "pve_permission_denied"


# --- auto-fixes ------------------------------------------------------------


def test_create_overlay_storage_payload_audit_and_recheck(client, platform_admin_headers, db_session, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    routes = _ok_routes()
    storage_list = routes[("GET", "/api2/json/storage")]._payload["data"]
    storage_list[:] = [s for s in storage_list if s["storage"] != "resurgam-lab-overlays"]

    def create(url, kwargs):
        data = kwargs["data"]
        storage_list.append({**data, "nodes": data["nodes"]})
        return FakeResponse({"data": {"storage": data["storage"], "type": "dir"}})

    routes[("POST", "/api2/json/storage")] = create
    fake = FakeProxmox(routes=routes).install(monkeypatch)

    response = client.post(
        f"{BASE}/{cluster_id}/lab-setup/overlay-storage", json={"nodes": ["pve1"]}, headers=platform_admin_headers
    )

    assert response.status_code == 200, response.text
    assert fake.called("POST", "/api2/json/storage")[0][3] == {
        "storage": "resurgam-lab-overlays",
        "type": "dir",
        "path": "/var/lib/resurgam-lab-agent/overlays",
        "content": "images",
        "nodes": "pve1",
    }
    body = response.json()
    assert body["action"] == "create_overlay_storage"
    assert body["item"]["key"] == "overlay_storage"
    assert body["item"]["status"] == "ok"
    event = db_session.query(AuditEvent).filter_by(action="pve_cluster_lab_overlay_storage_created").one()
    assert event.detail["nodes"] == ["pve1"]


def test_create_overlay_storage_refuses_existing_id(client, platform_admin_headers, db_session, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    fake = FakeProxmox(routes=_ok_routes()).install(monkeypatch)
    response = client.post(
        f"{BASE}/{cluster_id}/lab-setup/overlay-storage", json={"nodes": ["pve1"]}, headers=platform_admin_headers
    )
    assert response.status_code == 409
    assert response.json()["code"] == "storage_exists"
    assert not fake.called("POST", "/api2/json/storage")


def test_create_overlay_storage_rejects_unknown_node_and_bad_input(
    client, platform_admin_headers, db_session, monkeypatch
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    routes = _ok_routes()
    storage_list = routes[("GET", "/api2/json/storage")]._payload["data"]
    storage_list[:] = [s for s in storage_list if s["storage"] != "resurgam-lab-overlays"]
    fake = FakeProxmox(routes=routes).install(monkeypatch)

    response = client.post(
        f"{BASE}/{cluster_id}/lab-setup/overlay-storage", json={"nodes": ["ghost"]}, headers=platform_admin_headers
    )
    assert response.status_code == 400
    assert response.json()["code"] == "unknown_nodes"

    for bad in ({"nodes": []}, {"nodes": ["pve1"], "path": "relative/dir"}, {"nodes": ["pve1"], "storage_id": "-x"}):
        assert (
            client.post(
                f"{BASE}/{cluster_id}/lab-setup/overlay-storage", json=bad, headers=platform_admin_headers
            ).status_code
            == 422
        )
    assert not fake.called("POST", "/api2/json/storage")


def test_create_overlay_storage_pve_refusal_is_explained(client, platform_admin_headers, db_session, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    routes = _ok_routes()
    storage_list = routes[("GET", "/api2/json/storage")]._payload["data"]
    storage_list[:] = [s for s in storage_list if s["storage"] != "resurgam-lab-overlays"]
    routes[("POST", "/api2/json/storage")] = FakeResponse(
        None, 403, "Permission check failed (/storage, Datastore.Allocate)"
    )
    FakeProxmox(routes=routes).install(monkeypatch)
    response = client.post(
        f"{BASE}/{cluster_id}/lab-setup/overlay-storage", json={"nodes": ["pve1"]}, headers=platform_admin_headers
    )
    assert response.status_code == 502
    assert response.json()["code"] == "pve_write_failed"
    assert "Datastore.Allocate" in response.json()["message"]
    assert db_session.query(AuditEvent).filter_by(action="pve_cluster_lab_overlay_storage_created").count() == 0


def test_add_snippets_content_keeps_existing_types(client, platform_admin_headers, db_session, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session, lab_snippet_storage_id="local")
    routes = _ok_routes()
    local = routes[("GET", "/api2/json/storage")]._payload["data"][0]
    local["content"] = "iso,vztmpl,backup"
    routes[("GET", "/api2/json/storage/local")] = FakeResponse(
        {"data": {"type": "dir", "content": "iso,vztmpl,backup", "path": "/var/lib/vz", "digest": "abc123"}}
    )

    def update(url, kwargs):
        local["content"] = kwargs["data"]["content"]
        return FakeResponse({"data": None})

    routes[("PUT", "/api2/json/storage/local")] = update
    fake = FakeProxmox(routes=routes).install(monkeypatch)

    response = client.post(
        f"{BASE}/{cluster_id}/lab-setup/storage-content",
        json={"storage_id": "local", "content_type": "snippets"},
        headers=platform_admin_headers,
    )

    assert response.status_code == 200, response.text
    sent = fake.called("PUT", "/api2/json/storage/local")[0][3]
    assert sent == {"content": "iso,vztmpl,backup,snippets", "digest": "abc123"}
    body = response.json()
    assert body["content"] == ["iso", "vztmpl", "backup", "snippets"]
    assert (body["item"]["key"], body["item"]["status"]) == ("snippet_storage", "ok")
    event = db_session.query(AuditEvent).filter_by(action="pve_cluster_lab_storage_content_added").one()
    assert event.detail["content_before"] == ["iso", "vztmpl", "backup"]
    assert event.detail["content_after"] == ["iso", "vztmpl", "backup", "snippets"]


def test_add_content_is_noop_when_already_present(client, platform_admin_headers, db_session, monkeypatch):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    routes = _ok_routes()
    routes[("GET", "/api2/json/storage/local")] = FakeResponse(
        {"data": {"type": "dir", "content": "iso,snippets", "digest": "d"}}
    )
    fake = FakeProxmox(routes=routes).install(monkeypatch)
    response = client.post(
        f"{BASE}/{cluster_id}/lab-setup/storage-content",
        json={"storage_id": "local", "content_type": "snippets"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert not fake.called("PUT", "/api2/json/storage/local")
    assert db_session.query(AuditEvent).filter_by(action="pve_cluster_lab_storage_content_added").count() == 0


@pytest.mark.parametrize(
    ("storage_response", "status_code", "code"),
    [
        (FakeResponse(None, 500, "storage 'local' does not exist"), 404, "storage_not_found"),
        (
            FakeResponse({"data": {"type": "lvmthin", "content": "images", "digest": "d"}}),
            400,
            "storage_type_unsupported",
        ),
        (FakeResponse({"data": {"type": "dir", "digest": "d"}}), 409, "storage_content_unknown"),
    ],
)
def test_add_snippets_content_refusals(
    client, platform_admin_headers, db_session, monkeypatch, storage_response, status_code, code
):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    routes = _ok_routes()
    routes[("GET", "/api2/json/storage/local")] = storage_response
    fake = FakeProxmox(routes=routes).install(monkeypatch)
    response = client.post(
        f"{BASE}/{cluster_id}/lab-setup/storage-content",
        json={"storage_id": "local", "content_type": "snippets"},
        headers=platform_admin_headers,
    )
    assert response.status_code == status_code
    assert response.json()["code"] == code
    assert not fake.called("PUT", "/api2/json/storage/local")


def test_auto_fixes_require_platform_admin(client, db_session, test_settings):
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    user = User(email="viewer-labsetup@example.com", display_name="Viewer")
    db_session.add(user)
    db_session.flush()
    role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.GLOBAL.value))
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)
    headers = {"Authorization": f"Bearer {token}"}
    fake_id = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"{BASE}/{fake_id}/lab-readiness", headers=headers).status_code == 403
    assert (
        client.post(f"{BASE}/{fake_id}/lab-setup/overlay-storage", json={"nodes": ["a"]}, headers=headers).status_code
        == 403
    )
    assert (
        client.post(
            f"{BASE}/{fake_id}/lab-setup/storage-content",
            json={"storage_id": "local", "content_type": "snippets"},
            headers=headers,
        ).status_code
        == 403
    )


# --- agent install info ----------------------------------------------------


def test_agent_install_info(client, platform_admin_headers, db_session):
    cluster_id = _create_cluster(client, platform_admin_headers, db_session)
    body = client.get(
        f"{BASE}/{cluster_id}/lab-setup/agent-install?overlay_storage_id=labov&overlay_dir=/srv/labov",
        headers=platform_admin_headers,
    ).json()
    assert body["package_name"] == "resurgam-lab-agent"
    assert body["env_file_path"] == "/etc/default/resurgam-lab-agent"
    env = dict(line.split("=", 1) for line in body["env_file"].splitlines() if line and not line.startswith("#"))
    assert env == {
        "RESURGAM_BACKEND_URL": "https://resurgam.test.invalid/api/v1",
        "RESURGAM_LAB_AGENT_TOKEN": LAB_AGENT_TOKEN_PLACEHOLDER,
        "RESURGAM_LAB_AGENT_PBS_TOKEN": "",
        "RESURGAM_LAB_AGENT_OVERLAY_STORAGE_ID": "labov",
        "RESURGAM_LAB_AGENT_OVERLAY_DIR": "/srv/labov",
    }
    assert any("apt install" in step and body["package_version"] in step for step in body["install_steps"])


def test_agent_env_var_names_match_the_agent_config():
    """The env file is only useful if the agent actually reads these names."""
    config = (
        Path(__file__).resolve().parents[2] / "restore-worker/src/resurgam_worker/lab_agent/config.py"
    ).read_text()
    for name in (
        "RESURGAM_BACKEND_URL",
        "RESURGAM_LAB_AGENT_TOKEN",
        "RESURGAM_LAB_AGENT_PBS_TOKEN",
        "RESURGAM_LAB_AGENT_OVERLAY_STORAGE_ID",
        "RESURGAM_LAB_AGENT_OVERLAY_DIR",
    ):
        assert f'"{name}"' in config, name


def test_fallback_agent_package_version_matches_debian_changelog(monkeypatch):
    # Without an artifacts manifest the backend names the package version derived from the
    # release VERSION; it must be exactly what debian/changelog builds.
    monkeypatch.delenv("RESURGAM_VERSION", raising=False)
    changelog = (Path(__file__).resolve().parents[2] / "restore-worker/debian/changelog").read_text()
    first_line = changelog.splitlines()[0]
    assert f"({fallback_lab_agent_package_version()})" in first_line


def test_openapi_documents_every_lab_setup_field(client):
    spec = client.get("/api/openapi.json").json()
    schemas = spec["components"]["schemas"]
    for name in (
        "LabReadinessOut",
        "LabReadinessItemOut",
        "PveSdnZoneOut",
        "PveBridgeOut",
        "PveStorageConfigOut",
        "PveTemplateOut",
        "CreateOverlayStorageRequest",
        "AddStorageContentRequest",
        "LabSetupFixOut",
        "LabAgentInstallOut",
    ):
        for field_name, field in schemas[name]["properties"].items():
            assert field.get("description"), f"{name}.{field_name}"
    for path in (
        "/api/v1/pve-clusters/{cluster_id}/lab-readiness",
        "/api/v1/pve-clusters/{cluster_id}/lab-setup/overlay-storage",
        "/api/v1/pve-clusters/{cluster_id}/lab-setup/storage-content",
        "/api/v1/pve-clusters/{cluster_id}/lab-setup/agent-install",
    ):
        operation = next(iter(spec["paths"][path].values()))
        assert operation.get("description"), path
