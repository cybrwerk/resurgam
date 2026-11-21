from resurgam_api.models.datastore import Datastore
from resurgam_api.models.pbs import PbsInstance
from resurgam_api.models.secrets import SecretPurpose, SecretReference
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider
from tests.proxmox_fakes import FakeProxmox, FakeResponse, all_privileges

_ENCRYPTION_KEY = "PIjcEQaYJrbfZSCc5nPrYO-qID45lH3qxeKsKt_ln44="


def _make_datastore(db_session, name="store1") -> Datastore:
    ref = SecretReference(
        reference_key=f"test-pve-clusters-api-{name}", description="test", purpose=SecretPurpose.INFRA_CREDENTIAL
    )
    db_session.add(ref)
    db_session.flush()
    LocalEncryptedSecretsProvider(db_session, _ENCRYPTION_KEY).store(ref.reference_key, "test@pbs!token:secret")

    instance = PbsInstance(
        display_name="PBS",
        host="pbs.internal.example",
        port=8007,
        tls_trust_mode="ca",
        api_token_secret_ref_id=ref.id,
    )
    db_session.add(instance)
    db_session.flush()

    datastore = Datastore(pbs_instance_id=instance.id, name=name)
    db_session.add(datastore)
    db_session.flush()
    return datastore


def _create_ca_mode_payload(**overrides):
    payload = {
        "display_name": "Primary PVE",
        "host": "pve1.internal.example",
        "port": 8006,
        "tls_trust_mode": "ca",
        "api_token": "root@pam!token=secretvalue",
    }
    payload.update(overrides)
    return payload


def _create_pbs_instance(client, headers, **overrides) -> str:
    payload = {
        "display_name": "Linked PBS",
        "host": "pbs1.internal.example",
        "port": 8007,
        "tls_trust_mode": "ca",
        "api_token": "test@pbs!token:secretvalue",
    }
    payload.update(overrides)
    response = client.post("/api/v1/pbs-instances", json=payload, headers=headers)
    assert response.status_code == 200
    return response.json()["id"]


def test_create_list_get_pve_cluster(client, platform_admin_headers):
    create_response = client.post(
        "/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers
    )
    assert create_response.status_code == 200
    body = create_response.json()
    assert body["display_name"] == "Primary PVE"
    assert body["linked_pbs_instance_ids"] == []
    assert "api_token" not in body

    cluster_id = body["id"]

    list_response = client.get("/api/v1/pve-clusters", headers=platform_admin_headers)
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1

    get_response = client.get(f"/api/v1/pve-clusters/{cluster_id}", headers=platform_admin_headers)
    assert get_response.status_code == 200
    assert get_response.json()["id"] == cluster_id


def test_create_with_linked_pbs_instances(client, platform_admin_headers):
    pbs_id = _create_pbs_instance(client, platform_admin_headers)

    response = client.post(
        "/api/v1/pve-clusters",
        json=_create_ca_mode_payload(linked_pbs_instance_ids=[pbs_id]),
        headers=platform_admin_headers,
    )

    assert response.status_code == 200
    assert response.json()["linked_pbs_instance_ids"] == [pbs_id]


def test_create_with_unknown_linked_pbs_instance_rejected(client, platform_admin_headers):
    fake_id = "00000000-0000-0000-0000-000000000000"

    response = client.post(
        "/api/v1/pve-clusters",
        json=_create_ca_mode_payload(linked_pbs_instance_ids=[fake_id]),
        headers=platform_admin_headers,
    )

    assert response.status_code == 400
    assert response.json()["code"] == "pbs_instance_not_found"


def test_create_with_linked_datastores(client, platform_admin_headers, db_session):
    datastore = _make_datastore(db_session)

    response = client.post(
        "/api/v1/pve-clusters",
        json=_create_ca_mode_payload(linked_datastore_ids=[str(datastore.id)]),
        headers=platform_admin_headers,
    )

    assert response.status_code == 200
    assert response.json()["linked_datastore_ids"] == [str(datastore.id)]


def test_create_with_unknown_linked_datastore_rejected(client, platform_admin_headers):
    fake_id = "00000000-0000-0000-0000-000000000000"

    response = client.post(
        "/api/v1/pve-clusters",
        json=_create_ca_mode_payload(linked_datastore_ids=[fake_id]),
        headers=platform_admin_headers,
    )

    assert response.status_code == 400
    assert response.json()["code"] == "datastore_not_found"


def test_update_replaces_linked_datastores(client, platform_admin_headers, db_session):
    first_store = _make_datastore(db_session, name="store-a")
    second_store = _make_datastore(db_session, name="store-b")

    created = client.post(
        "/api/v1/pve-clusters",
        json=_create_ca_mode_payload(linked_datastore_ids=[str(first_store.id)]),
        headers=platform_admin_headers,
    ).json()
    assert created["linked_datastore_ids"] == [str(first_store.id)]

    response = client.patch(
        f"/api/v1/pve-clusters/{created['id']}",
        json={"linked_datastore_ids": [str(second_store.id)]},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["linked_datastore_ids"] == [str(second_store.id)]


def test_update_omitting_linked_datastore_ids_leaves_them_unchanged(client, platform_admin_headers, db_session):
    datastore = _make_datastore(db_session)

    created = client.post(
        "/api/v1/pve-clusters",
        json=_create_ca_mode_payload(linked_datastore_ids=[str(datastore.id)]),
        headers=platform_admin_headers,
    ).json()

    response = client.patch(
        f"/api/v1/pve-clusters/{created['id']}",
        json={"display_name": "Renamed"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["linked_datastore_ids"] == [str(datastore.id)]
    assert response.json()["display_name"] == "Renamed"


def test_non_platform_admin_role_is_forbidden(client, db_session):
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, UserRoleAssignment

    login = client.post(
        "/api/v1/auth/breakglass-login",
        json={"username": "test-admin", "password": "test-password"},
    )
    token = login.json()["access_token"]
    user_id = client.get("/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"}).json()["user_id"]

    # Break-glass always holds platform_admin (ADR 0028); this test needs a plain viewer.
    db_session.query(UserRoleAssignment).filter_by(user_id=user_id).delete()
    viewer_role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(UserRoleAssignment(user_id=user_id, role_id=viewer_role.id, scope_type=ScopeType.GLOBAL.value))
    db_session.commit()

    response = client.post(
        "/api/v1/pve-clusters",
        json=_create_ca_mode_payload(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_create_with_wrong_confirmed_fingerprint_is_rejected(client, platform_admin_headers, local_tls_server):
    host, port, _real_fingerprint = local_tls_server

    response = client.post(
        "/api/v1/pve-clusters",
        json=_create_ca_mode_payload(
            host=host, port=port, tls_trust_mode="fingerprint", tls_fingerprint_sha256="0" * 64
        ),
        headers=platform_admin_headers,
    )

    assert response.status_code == 409
    assert client.get("/api/v1/pve-clusters", headers=platform_admin_headers).json() == []


def test_test_connection_updates_health_status(client, platform_admin_headers, monkeypatch):
    create_response = client.post(
        "/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers
    )
    cluster_id = create_response.json()["id"]

    FakeProxmox(
        routes={
            ("GET", "/api2/json/version"): FakeResponse({"data": {"version": "8.3.1"}}),
            ("GET", "/api2/json/access/permissions"): FakeResponse({"data": all_privileges("pve")}),
        }
    ).install(monkeypatch)

    response = client.post(f"/api/v1/pve-clusters/{cluster_id}/test-connection", headers=platform_admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["reachable"] is True
    assert body["status"] == "ok"
    assert body["version"] == "8.3.1"
    assert body["error"] is None
    assert body["missing_privileges"] == []

    get_response = client.get(f"/api/v1/pve-clusters/{cluster_id}", headers=platform_admin_headers)
    assert get_response.json()["last_health_status"] == "ok"
    assert get_response.json()["version"] == "8.3.1"


def test_disable_and_enable(client, platform_admin_headers):
    create_response = client.post(
        "/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers
    )
    cluster_id = create_response.json()["id"]

    disable_response = client.post(f"/api/v1/pve-clusters/{cluster_id}/disable", headers=platform_admin_headers)
    assert disable_response.json()["disabled"] is True

    enable_response = client.post(f"/api/v1/pve-clusters/{cluster_id}/enable", headers=platform_admin_headers)
    assert enable_response.json()["disabled"] is False


def test_delete_removes_cluster_secret_and_links(client, platform_admin_headers, db_session):
    pbs_id = _create_pbs_instance(client, platform_admin_headers)
    create_response = client.post(
        "/api/v1/pve-clusters",
        json=_create_ca_mode_payload(linked_pbs_instance_ids=[pbs_id]),
        headers=platform_admin_headers,
    )
    cluster_id = create_response.json()["id"]

    from resurgam_api.models.pve import PveCluster, PveClusterPbsInstance
    from resurgam_api.models.secrets import LocalSecretValue, SecretReference

    cluster = db_session.get(PveCluster, cluster_id)
    reference_key = db_session.get(SecretReference, cluster.api_token_secret_ref_id).reference_key

    delete_response = client.delete(f"/api/v1/pve-clusters/{cluster_id}", headers=platform_admin_headers)
    assert delete_response.status_code == 204

    assert db_session.get(PveCluster, cluster_id) is None
    assert db_session.get(LocalSecretValue, reference_key) is None
    assert db_session.query(PveClusterPbsInstance).filter_by(pve_cluster_id=cluster_id).count() == 0


def test_get_nonexistent_cluster_returns_404(client, platform_admin_headers):
    response = client.get("/api/v1/pve-clusters/00000000-0000-0000-0000-000000000000", headers=platform_admin_headers)
    assert response.status_code == 404


def test_update_changes_only_given_fields(client, platform_admin_headers):
    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    response = client.patch(
        f"/api/v1/pve-clusters/{created['id']}",
        json={"display_name": "Renamed PVE"},
        headers=platform_admin_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "Renamed PVE"
    assert body["host"] == created["host"]


def test_update_can_clear_lab_sdn_zone_and_template_vmid(client, platform_admin_headers):
    """Regression test: `lab_sdn_zone_name`/`lab_appliance_template_vmid`
    are meant to be clearable back to null (e.g. "stop using an admin-
    configured SDN zone, fall back to RESURGAM's own default"), unlike
    every other field on this endpoint -- an explicit `null` must not be
    treated the same as the field being omitted.
    """
    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    set_response = client.patch(
        f"/api/v1/pve-clusters/{created['id']}",
        json={"lab_sdn_zone_name": "customzone", "lab_appliance_template_vmid": 9000},
        headers=platform_admin_headers,
    )
    assert set_response.status_code == 200
    body = set_response.json()
    assert body["lab_sdn_zone_name"] == "customzone"
    assert body["lab_appliance_template_vmid"] == 9000

    clear_response = client.patch(
        f"/api/v1/pve-clusters/{created['id']}",
        json={"lab_sdn_zone_name": None, "lab_appliance_template_vmid": None},
        headers=platform_admin_headers,
    )
    assert clear_response.status_code == 200
    cleared_body = clear_response.json()
    assert cleared_body["lab_sdn_zone_name"] is None
    assert cleared_body["lab_appliance_template_vmid"] is None

    # Omitting the fields entirely (not sending them at all) must still
    # leave the current value unchanged -- the established convention
    # for every other field on this endpoint.
    display_name_only = client.patch(
        f"/api/v1/pve-clusters/{created['id']}",
        json={"display_name": "Still lab-configured"},
        headers=platform_admin_headers,
    )
    assert display_name_only.status_code == 200
    assert display_name_only.json()["lab_sdn_zone_name"] is None


def test_update_rotates_api_token_secret(client, platform_admin_headers, db_session, test_settings):
    from resurgam_api.models.pve import PveCluster
    from resurgam_api.models.secrets import SecretReference
    from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    response = client.patch(
        f"/api/v1/pve-clusters/{created['id']}",
        json={"api_token": "new@pve!token=newsecret"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200

    cluster = db_session.get(PveCluster, created["id"])
    secret_ref = db_session.get(SecretReference, cluster.api_token_secret_ref_id)
    provider = LocalEncryptedSecretsProvider(db_session, test_settings.secrets_local_encryption_key)
    assert provider.retrieve(secret_ref.reference_key) == "new@pve!token=newsecret"


def test_update_replaces_linked_pbs_instances(client, platform_admin_headers):
    first_pbs = _create_pbs_instance(client, platform_admin_headers, host="pbs1.internal.example")
    second_pbs = _create_pbs_instance(client, platform_admin_headers, host="pbs2.internal.example")
    created = client.post(
        "/api/v1/pve-clusters",
        json=_create_ca_mode_payload(linked_pbs_instance_ids=[first_pbs]),
        headers=platform_admin_headers,
    ).json()
    assert created["linked_pbs_instance_ids"] == [first_pbs]

    response = client.patch(
        f"/api/v1/pve-clusters/{created['id']}",
        json={"linked_pbs_instance_ids": [second_pbs]},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["linked_pbs_instance_ids"] == [second_pbs]


def test_update_rejects_unknown_linked_pbs_instance(client, platform_admin_headers):
    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    response = client.patch(
        f"/api/v1/pve-clusters/{created['id']}",
        json={"linked_pbs_instance_ids": ["00000000-0000-0000-0000-000000000000"]},
        headers=platform_admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "pbs_instance_not_found"


def test_update_nonexistent_cluster_returns_404(client, platform_admin_headers):
    response = client.patch(
        "/api/v1/pve-clusters/00000000-0000-0000-0000-000000000000",
        json={"display_name": "x"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 404


def test_list_nodes_returns_live_data(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import NodeListResult, RemoteNode

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    monkeypatch.setattr(
        pve_clusters_module,
        "list_nodes",
        lambda *a, **kw: NodeListResult(reachable=True, nodes=[RemoteNode(node="pve1", status="online")]),
    )

    response = client.get(f"/api/v1/pve-clusters/{created['id']}/nodes", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json() == [{"node": "pve1", "status": "online", "cpu": None, "maxmem": None, "uptime": None}]


def test_list_nodes_unreachable_returns_502(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import NodeListResult

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    monkeypatch.setattr(
        pve_clusters_module, "list_nodes", lambda *a, **kw: NodeListResult(reachable=False, error="timeout")
    )

    response = client.get(f"/api/v1/pve-clusters/{created['id']}/nodes", headers=platform_admin_headers)
    assert response.status_code == 502
    assert response.json()["code"] == "pve_unreachable"


def test_list_node_storages_returns_live_data(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import RemoteStorage, StorageListResult

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    monkeypatch.setattr(
        pve_clusters_module,
        "list_storages",
        lambda *a, **kw: StorageListResult(
            reachable=True, storages=[RemoteStorage(storage="local-lvm", type="lvmthin", content=["images"])]
        ),
    )

    response = client.get(f"/api/v1/pve-clusters/{created['id']}/nodes/pve1/storages", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json()[0]["storage"] == "local-lvm"


def test_list_guests_returns_live_data(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import GuestListResult, RemoteGuest

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    monkeypatch.setattr(
        pve_clusters_module,
        "list_guests",
        lambda *a, **kw: GuestListResult(
            reachable=True,
            guests=[RemoteGuest(vmid=100, node="pve1", guest_type="qemu", name="test", status="stopped")],
        ),
    )

    response = client.get(f"/api/v1/pve-clusters/{created['id']}/guests", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json()[0]["vmid"] == 100


def test_list_pools_returns_live_data(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import PoolListResult, RemotePool

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    monkeypatch.setattr(
        pve_clusters_module,
        "list_pools",
        lambda *a, **kw: PoolListResult(
            reachable=True, pools=[RemotePool(poolid="resurgam-lab", comment="RESURGAM Recovery Lab")]
        ),
    )

    response = client.get(f"/api/v1/pve-clusters/{created['id']}/pools", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json() == [{"poolid": "resurgam-lab", "comment": "RESURGAM Recovery Lab"}]


def test_vmid_check_returns_live_data(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import VmidAvailabilityResult

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    monkeypatch.setattr(
        pve_clusters_module,
        "check_vmid_free",
        lambda *a, **kw: VmidAvailabilityResult(reachable=True, vmid=999, free=True),
    )

    response = client.get(
        f"/api/v1/pve-clusters/{created['id']}/vmid-check", params={"vmid": 999}, headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert response.json() == {"vmid": 999, "free": True}


def test_live_probe_endpoints_require_platform_admin(client, db_session):
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, UserRoleAssignment

    login = client.post(
        "/api/v1/auth/breakglass-login",
        json={"username": "test-admin", "password": "test-password"},
    )
    token = login.json()["access_token"]
    user_id = client.get("/api/v1/auth/whoami", headers={"Authorization": f"Bearer {token}"}).json()["user_id"]

    # Break-glass always holds platform_admin (ADR 0028); this test needs a plain viewer.
    db_session.query(UserRoleAssignment).filter_by(user_id=user_id).delete()
    viewer_role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(UserRoleAssignment(user_id=user_id, role_id=viewer_role.id, scope_type=ScopeType.GLOBAL.value))
    db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    fake_id = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/api/v1/pve-clusters/{fake_id}/nodes", headers=headers).status_code == 403
    assert client.get(f"/api/v1/pve-clusters/{fake_id}/nodes/pve1/storages", headers=headers).status_code == 403
    assert client.get(f"/api/v1/pve-clusters/{fake_id}/guests", headers=headers).status_code == 403
    assert (
        client.get(f"/api/v1/pve-clusters/{fake_id}/vmid-check", params={"vmid": 1}, headers=headers).status_code == 403
    )
    assert client.get(f"/api/v1/pve-clusters/{fake_id}/backup-jobs", headers=headers).status_code == 403
    assert client.post(f"/api/v1/pve-clusters/{fake_id}/backup-jobs/job1/enable", headers=headers).status_code == 403
    assert client.post(f"/api/v1/pve-clusters/{fake_id}/backup-jobs/job1/disable", headers=headers).status_code == 403


def test_list_backup_jobs_returns_live_data(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import VzdumpJob, VzdumpJobListResult

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    monkeypatch.setattr(
        pve_clusters_module,
        "list_vzdump_jobs",
        lambda *a, **kw: VzdumpJobListResult(
            reachable=True,
            jobs=[VzdumpJob(id="backup-abc123", enabled=True, schedule="02:30", storage="pbs-main")],
        ),
    )

    response = client.get(f"/api/v1/pve-clusters/{created['id']}/backup-jobs", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "backup-abc123",
            "enabled": True,
            "schedule": "02:30",
            "storage": "pbs-main",
            "node": None,
            "comment": None,
            "all_guests": False,
            "vmid": None,
            "pool": None,
            "exclude": None,
            "mode": None,
            "compress": None,
            "notes_template": None,
            "mailnotification": None,
            "mailto": None,
            "prune_keep_last": None,
            "prune_keep_hourly": None,
            "prune_keep_daily": None,
            "prune_keep_weekly": None,
            "prune_keep_monthly": None,
            "prune_keep_yearly": None,
            "customer_id": None,
            "reenable_at": None,
            "reenable_last_error": None,
        }
    ]


def test_list_backup_jobs_unreachable_returns_502(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import VzdumpJobListResult

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    monkeypatch.setattr(
        pve_clusters_module, "list_vzdump_jobs", lambda *a, **kw: VzdumpJobListResult(reachable=False, error="timeout")
    )

    response = client.get(f"/api/v1/pve-clusters/{created['id']}/backup-jobs", headers=platform_admin_headers)
    assert response.status_code == 502
    assert response.json()["code"] == "pve_unreachable"


def test_disable_backup_job_calls_pve_and_records_audit(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import VzdumpJob, VzdumpJobListResult
    from resurgam_api.models.audit import AuditEvent

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    calls = []
    monkeypatch.setattr(
        pve_clusters_module, "set_vzdump_job_enabled", lambda *a, job_id, enabled, **kw: calls.append((job_id, enabled))
    )
    monkeypatch.setattr(
        pve_clusters_module,
        "list_vzdump_jobs",
        lambda *a, **kw: VzdumpJobListResult(
            reachable=True, jobs=[VzdumpJob(id="backup-abc123", enabled=False, schedule="02:30", storage="pbs-main")]
        ),
    )

    response = client.post(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs/backup-abc123/disable", headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert calls == [("backup-abc123", False)]

    event = db_session.query(AuditEvent).filter_by(action="pve_vzdump_job_disabled").one()
    assert event.detail == {"job_id": "backup-abc123", "schedule": "02:30"}


def test_enable_backup_job_write_failure_returns_502(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.pve_api import PveWriteError

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    def _raise(*a, **kw):
        raise PveWriteError("HTTP 403: Forbidden")

    monkeypatch.setattr(pve_clusters_module, "set_vzdump_job_enabled", _raise)

    response = client.post(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs/backup-abc123/enable", headers=platform_admin_headers
    )
    assert response.status_code == 502
    assert response.json()["code"] == "pve_write_failed"


# --- vzdump job CRUD + capability gating (docs/adr/0023-backup-and-maintenance-job-administration) -----


def test_backup_job_capabilities_reports_real_grants(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import PermissionsResult

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()
    monkeypatch.setattr(
        pve_clusters_module,
        "get_own_permissions",
        lambda *a, **kw: PermissionsResult(reachable=True, privileges={"/": {"Sys.Audit": 1, "Sys.Modify": 1}}),
    )

    response = client.get(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs/capabilities", headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert response.json() == {"can_read": True, "can_write": True}


def test_backup_job_capabilities_read_only_token(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import PermissionsResult

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()
    monkeypatch.setattr(
        pve_clusters_module,
        "get_own_permissions",
        lambda *a, **kw: PermissionsResult(reachable=True, privileges={"/": {"Sys.Audit": 1}}),
    )

    response = client.get(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs/capabilities", headers=platform_admin_headers
    )
    assert response.json() == {"can_read": True, "can_write": False}


def test_get_backup_job_returns_full_detail(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import VzdumpJob

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()
    monkeypatch.setattr(
        pve_clusters_module,
        "get_vzdump_job",
        lambda *a, **kw: VzdumpJob(id="backup-abc123", enabled=True, schedule="02:30", mode="snapshot"),
    )

    response = client.get(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs/backup-abc123", headers=platform_admin_headers
    )
    assert response.status_code == 200
    assert response.json()["mode"] == "snapshot"


def test_get_backup_job_not_found_returns_404(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()
    monkeypatch.setattr(pve_clusters_module, "get_vzdump_job", lambda *a, **kw: None)

    response = client.get(f"/api/v1/pve-clusters/{created['id']}/backup-jobs/nope", headers=platform_admin_headers)
    assert response.status_code == 404


def test_create_backup_job_generates_id_and_audits(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import VzdumpJob
    from resurgam_api.models.audit import AuditEvent

    created_cluster = client.post(
        "/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers
    ).json()

    create_calls = []

    def fake_create(*a, job, **kw):
        create_calls.append(job)

    def fake_get(*a, job_id, **kw):
        return VzdumpJob(id=job_id, enabled=True, schedule="03:00", storage="pbs-main")

    monkeypatch.setattr(pve_clusters_module, "create_vzdump_job", fake_create)
    monkeypatch.setattr(pve_clusters_module, "get_vzdump_job", fake_get)

    response = client.post(
        f"/api/v1/pve-clusters/{created_cluster['id']}/backup-jobs",
        json={"schedule": "03:00", "storage": "pbs-main", "all_guests": True},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["schedule"] == "03:00"
    # RESURGAM must generate and send its own id -- confirmed live
    # 2026-09-26 that POST /cluster/backup's response is empty and real
    # PVE-generated IDs can't be matched back afterward.
    assert create_calls[0].id.startswith("resurgam-")
    assert create_calls[0].all_guests is True

    event = db_session.query(AuditEvent).filter_by(action="pve_backup_job_created").one()
    assert event.detail["schedule"] == "03:00"


def test_create_backup_job_write_failure_returns_502(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.pve_api import PveWriteError

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    def _raise(*a, **kw):
        raise PveWriteError("HTTP 403: Permission check failed (/storage/pbs-main, Datastore.Allocate)")

    monkeypatch.setattr(pve_clusters_module, "create_vzdump_job", _raise)

    response = client.post(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs", json={"schedule": "03:00"}, headers=platform_admin_headers
    )
    assert response.status_code == 502
    assert response.json()["code"] == "pve_write_failed"
    # The real PVE error text must reach the operator, not a generic
    # message -- this is the ADR 0018 storage-scoped permission gap the
    # up-front capability check can't predict on its own.
    assert "Datastore.Allocate" in response.json()["message"]


def test_update_backup_job_sends_only_changed_fields(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import VzdumpJob
    from resurgam_api.models.audit import AuditEvent

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    update_calls = []
    monkeypatch.setattr(
        pve_clusters_module, "update_vzdump_job", lambda *a, job_id, changes, **kw: update_calls.append(changes)
    )
    monkeypatch.setattr(
        pve_clusters_module,
        "get_vzdump_job",
        lambda *a, **kw: VzdumpJob(id="backup-abc123", enabled=True, schedule="05:00"),
    )

    response = client.put(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs/backup-abc123",
        json={"schedule": "05:00"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert update_calls == [{"schedule": "05:00"}]

    event = db_session.query(AuditEvent).filter_by(action="pve_backup_job_updated").one()
    assert event.detail == {"job_id": "backup-abc123", "changed_fields": ["schedule"]}


def test_update_backup_job_no_fields_returns_400(client, platform_admin_headers):
    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    response = client.put(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs/backup-abc123", json={}, headers=platform_admin_headers
    )
    assert response.status_code == 400
    assert response.json()["code"] == "no_changes"


def test_update_backup_job_sends_complete_prune_keep_set_together(client, platform_admin_headers, monkeypatch):
    """PVE's `prune-backups` is one property-string field on the wire,
    not independently-settable sub-keys -- touching any one `prune_keep_*`
    field must send the whole encoded set, not just that one field.
    """
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import VzdumpJob

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    update_calls = []
    monkeypatch.setattr(
        pve_clusters_module, "update_vzdump_job", lambda *a, job_id, changes, **kw: update_calls.append(changes)
    )
    monkeypatch.setattr(
        pve_clusters_module,
        "get_vzdump_job",
        lambda *a, **kw: VzdumpJob(id="backup-abc123", enabled=True, schedule="05:00"),
    )

    response = client.put(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs/backup-abc123",
        json={"prune_keep_daily": 7, "prune_keep_weekly": 4},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert update_calls == [{"prune-backups": "keep-daily=7,keep-weekly=4"}]


def test_update_backup_job_clear_prune_backups_is_atomic(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import VzdumpJob

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    update_calls = []
    monkeypatch.setattr(
        pve_clusters_module, "update_vzdump_job", lambda *a, job_id, changes, **kw: update_calls.append(changes)
    )
    monkeypatch.setattr(
        pve_clusters_module, "get_vzdump_job", lambda *a, **kw: VzdumpJob(id="backup-abc123", enabled=True)
    )

    response = client.put(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs/backup-abc123",
        json={"clear_prune_backups": True, "schedule": "06:00"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    # clear_prune_backups is atomic -- any other field in the same request
    # is ignored, matching pbs maintenance-mode's own clear semantics.
    assert update_calls == [{"delete": "prune-backups"}]


def test_delete_backup_job_audits_and_returns_204(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.models.audit import AuditEvent

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    delete_calls = []
    monkeypatch.setattr(pve_clusters_module, "delete_vzdump_job", lambda *a, job_id, **kw: delete_calls.append(job_id))

    response = client.delete(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs/backup-abc123", headers=platform_admin_headers
    )
    assert response.status_code == 204
    assert delete_calls == ["backup-abc123"]

    event = db_session.query(AuditEvent).filter_by(action="pve_backup_job_deleted").one()
    assert event.detail == {"job_id": "backup-abc123"}


def test_delete_backup_job_write_failure_returns_502(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.pve_api import PveWriteError

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    def _raise(*a, **kw):
        raise PveWriteError("HTTP 400: no such job")

    monkeypatch.setattr(pve_clusters_module, "delete_vzdump_job", _raise)

    response = client.delete(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs/backup-abc123", headers=platform_admin_headers
    )
    assert response.status_code == 502


# --- vzdump job customer-scope override (docs/adr/0023-backup-and-maintenance-job-administration) ---


def _create_customer(db_session, **overrides):
    from resurgam_api.models.tenancy import Customer

    payload = {"name": "Acme AG", "slug": "acme", "debitor_number": "1000", "match_code": "ACM"}
    payload.update(overrides)
    customer = Customer(**payload)
    db_session.add(customer)
    db_session.flush()
    return customer


def test_assign_backup_job_customer_creates_override(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import VzdumpJob
    from resurgam_api.models.audit import AuditEvent

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()
    customer = _create_customer(db_session)
    monkeypatch.setattr(
        pve_clusters_module,
        "get_vzdump_job",
        lambda *a, **kw: VzdumpJob(id="backup-abc123", enabled=True, schedule="02:30"),
    )

    response = client.post(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs/backup-abc123/assign-customer",
        json={"customer_id": str(customer.id)},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["customer_id"] == str(customer.id)

    event = db_session.query(AuditEvent).filter_by(action="pve_backup_job_customer_assigned").one()
    assert event.detail == {"job_id": "backup-abc123", "customer_id": str(customer.id)}


def test_assign_backup_job_customer_upserts_existing_override(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import VzdumpJob

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()
    first = _create_customer(db_session, name="Acme AG", slug="acme", debitor_number="1000", match_code="ACM")
    second = _create_customer(db_session, name="Beta AG", slug="beta", debitor_number="1001", match_code="BET")
    monkeypatch.setattr(
        pve_clusters_module, "get_vzdump_job", lambda *a, **kw: VzdumpJob(id="backup-abc123", enabled=True)
    )

    client.post(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs/backup-abc123/assign-customer",
        json={"customer_id": str(first.id)},
        headers=platform_admin_headers,
    )
    response = client.post(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs/backup-abc123/assign-customer",
        json={"customer_id": str(second.id)},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["customer_id"] == str(second.id)

    from resurgam_api.models.pve import PveBackupJobCustomerAssignment

    rows = db_session.query(PveBackupJobCustomerAssignment).filter_by(job_id="backup-abc123").all()
    assert len(rows) == 1
    assert rows[0].customer_id == second.id


def test_assign_backup_job_customer_unknown_customer_returns_400(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import VzdumpJob

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()
    monkeypatch.setattr(
        pve_clusters_module, "get_vzdump_job", lambda *a, **kw: VzdumpJob(id="backup-abc123", enabled=True)
    )

    response = client.post(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs/backup-abc123/assign-customer",
        json={"customer_id": "00000000-0000-0000-0000-000000000000"},
        headers=platform_admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "customer_not_found"


def test_assign_backup_job_customer_unknown_job_returns_404(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()
    customer = _create_customer(db_session)
    monkeypatch.setattr(pve_clusters_module, "get_vzdump_job", lambda *a, **kw: None)

    response = client.post(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs/nope/assign-customer",
        json={"customer_id": str(customer.id)},
        headers=platform_admin_headers,
    )
    assert response.status_code == 404


def test_unassign_backup_job_customer_removes_override(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import VzdumpJob
    from resurgam_api.models.audit import AuditEvent
    from resurgam_api.models.pve import PveBackupJobCustomerAssignment

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()
    customer = _create_customer(db_session)
    monkeypatch.setattr(
        pve_clusters_module, "get_vzdump_job", lambda *a, **kw: VzdumpJob(id="backup-abc123", enabled=True)
    )
    client.post(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs/backup-abc123/assign-customer",
        json={"customer_id": str(customer.id)},
        headers=platform_admin_headers,
    )

    response = client.post(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs/backup-abc123/unassign-customer",
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["customer_id"] is None
    assert db_session.query(PveBackupJobCustomerAssignment).filter_by(job_id="backup-abc123").one_or_none() is None

    event = db_session.query(AuditEvent).filter_by(action="pve_backup_job_customer_unassigned").one()
    assert event.detail == {"job_id": "backup-abc123"}


def test_unassign_backup_job_customer_when_none_set_is_a_noop(client, platform_admin_headers, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import VzdumpJob

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()
    monkeypatch.setattr(
        pve_clusters_module, "get_vzdump_job", lambda *a, **kw: VzdumpJob(id="backup-abc123", enabled=True)
    )

    response = client.post(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs/backup-abc123/unassign-customer",
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["customer_id"] is None


def test_delete_backup_job_removes_customer_override(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.connectivity.models import VzdumpJob
    from resurgam_api.models.pve import PveBackupJobCustomerAssignment

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()
    customer = _create_customer(db_session)
    monkeypatch.setattr(
        pve_clusters_module, "get_vzdump_job", lambda *a, **kw: VzdumpJob(id="backup-abc123", enabled=True)
    )
    client.post(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs/backup-abc123/assign-customer",
        json={"customer_id": str(customer.id)},
        headers=platform_admin_headers,
    )
    monkeypatch.setattr(pve_clusters_module, "delete_vzdump_job", lambda *a, **kw: None)

    response = client.delete(
        f"/api/v1/pve-clusters/{created['id']}/backup-jobs/backup-abc123", headers=platform_admin_headers
    )
    assert response.status_code == 204
    assert db_session.query(PveBackupJobCustomerAssignment).filter_by(job_id="backup-abc123").one_or_none() is None


def test_backup_job_endpoints_require_platform_admin(client, platform_admin_headers, db_session, test_settings):
    """Every backup-job endpoint stays behind the highest bar, matching
    ADR 0018's own explicit reasoning: infra mutation of this kind
    shouldn't be the first thing to loosen that requirement.

    Uses a genuinely separate, directly-minted user rather than a second
    breakglass login -- breakglass always resolves to the *same* fixed
    identity `platform_admin_headers` already granted global
    platform_admin in this same test transaction (see
    `test_datastores_api.py::_create_customer_scoped_viewer`'s docstring
    for the exact same gotcha found there first).
    """
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    user = User(email="viewer-backup-jobs@example.com", display_name="Scoped Viewer")
    db_session.add(user)
    db_session.flush()
    viewer_role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(UserRoleAssignment(user_id=user.id, role_id=viewer_role.id, scope_type=ScopeType.GLOBAL.value))
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)
    non_admin_headers = {"Authorization": f"Bearer {token}"}

    assert (
        client.get(
            f"/api/v1/pve-clusters/{created['id']}/backup-jobs/capabilities", headers=non_admin_headers
        ).status_code
        == 403
    )
    assert (
        client.post(f"/api/v1/pve-clusters/{created['id']}/backup-jobs", json={}, headers=non_admin_headers).status_code
        == 403
    )
    assert (
        client.delete(f"/api/v1/pve-clusters/{created['id']}/backup-jobs/x", headers=non_admin_headers).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/pve-clusters/{created['id']}/backup-jobs/x/assign-customer", json={}, headers=non_admin_headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/pve-clusters/{created['id']}/backup-jobs/x/unassign-customer", headers=non_admin_headers
        ).status_code
        == 403
    )


def test_generate_lab_agent_token_returns_plaintext_once_and_hashes_at_rest(client, platform_admin_headers, db_session):
    from resurgam_api.models.pve import PveCluster

    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    response = client.post(f"/api/v1/pve-clusters/{created['id']}/lab-agent-token", headers=platform_admin_headers)
    assert response.status_code == 200
    token = response.json()["token"]
    assert len(token) > 20

    cluster = db_session.get(PveCluster, created["id"])
    assert cluster.lab_agent_token_hash is not None
    assert cluster.lab_agent_token_hash != token


def test_generate_lab_agent_token_rotation_invalidates_the_previous_token(client, platform_admin_headers, db_session):
    created = client.post("/api/v1/pve-clusters", json=_create_ca_mode_payload(), headers=platform_admin_headers).json()

    first = client.post(f"/api/v1/pve-clusters/{created['id']}/lab-agent-token", headers=platform_admin_headers).json()[
        "token"
    ]
    second = client.post(
        f"/api/v1/pve-clusters/{created['id']}/lab-agent-token", headers=platform_admin_headers
    ).json()["token"]
    assert first != second

    assert client.get("/api/v1/lab-agent/jobs", headers={"Authorization": f"Bearer {first}"}).status_code == 401
    assert client.get("/api/v1/lab-agent/jobs", headers={"Authorization": f"Bearer {second}"}).status_code == 200


def test_generate_lab_agent_token_requires_platform_admin(client, db_session, test_settings):
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    user = User(email="viewer-labagenttoken@example.com", display_name="Viewer")
    db_session.add(user)
    db_session.flush()
    role = db_session.query(Role).filter_by(key=RoleKey.VIEWER.value).one()
    db_session.add(UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.GLOBAL.value))
    db_session.commit()
    token, _ = create_session_token(user.id, test_settings)
    headers = {"Authorization": f"Bearer {token}"}

    fake_id = "00000000-0000-0000-0000-000000000000"
    assert client.post(f"/api/v1/pve-clusters/{fake_id}/lab-agent-token", headers=headers).status_code == 403
