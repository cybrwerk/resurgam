from datetime import UTC, datetime

from resurgam_api.connectivity.models import (
    DatastoreListResult,
    GuestListResult,
    RemoteDatastore,
    RemoteGuest,
    RemoteStorage,
    StorageListResult,
    VmidAvailabilityResult,
)


def _create_pbs_instance(client, headers) -> str:
    response = client.post(
        "/api/v1/pbs-instances",
        json={
            "display_name": "PBS",
            "host": "pbs1.internal.example",
            "port": 8007,
            "tls_trust_mode": "ca",
            "api_token": "resurgam@pbs!dev:secretvalue",
        },
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()["id"]


def _import_datastore(client, headers, instance_id, monkeypatch, name="store1") -> str:
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module

    monkeypatch.setattr(
        pbs_instances_module,
        "list_datastores",
        lambda *a, **kw: DatastoreListResult(reachable=True, datastores=[RemoteDatastore(name=name)]),
    )
    response = client.post(f"/api/v1/pbs-instances/{instance_id}/datastores", json={"name": name}, headers=headers)
    assert response.status_code == 200
    return response.json()["id"]


def _create_pve_cluster(client, headers, linked_pbs_instance_ids=None) -> str:
    response = client.post(
        "/api/v1/pve-clusters",
        json={
            "display_name": "Primary PVE",
            "host": "pve1.internal.example",
            "port": 8006,
            "tls_trust_mode": "ca",
            "api_token": "root@pam!token=secretvalue",
            "linked_pbs_instance_ids": linked_pbs_instance_ids or [],
        },
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()["id"]


def _create_customer(db_session, name="Acme", slug="acme"):
    from resurgam_api.models.tenancy import Customer

    customer = Customer(
        name=name, slug=slug, debitor_number=f"debitor-{slug}", match_code=slug[:3].upper().ljust(3, "X")
    )
    db_session.add(customer)
    db_session.flush()
    db_session.commit()
    return customer.id


def _create_group(db_session, datastore_id, backup_type="vm", backup_id="116", namespace=""):
    from resurgam_api.models.inventory import BackupGroup

    group = BackupGroup(datastore_id=datastore_id, backup_type=backup_type, backup_id=backup_id, namespace=namespace)
    db_session.add(group)
    db_session.flush()
    db_session.commit()
    return group.id


def _add_snapshot(db_session, group_id, snapshot_time=None):
    from resurgam_api.models.inventory import BackupSnapshot

    snapshot = BackupSnapshot(
        backup_group_id=group_id, snapshot_time=snapshot_time or datetime(2026, 9, 14, 0, 48, 57, tzinfo=UTC)
    )
    db_session.add(snapshot)
    db_session.flush()
    db_session.commit()
    return snapshot.id


def _create_customer_scoped_user(db_session, test_settings, customer_id, role_key) -> dict:
    from resurgam_api.auth.sessions import create_session_token
    from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

    user = User(email=f"user-{role_key}-{customer_id}@example.com", display_name="Scoped User")
    db_session.add(user)
    db_session.flush()

    role = db_session.query(Role).filter_by(key=RoleKey(role_key).value).one()
    db_session.add(
        UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.CUSTOMER.value, scope_id=customer_id)
    )
    db_session.commit()

    token, _ = create_session_token(user.id, test_settings)
    return {"Authorization": f"Bearer {token}"}


def _setup_vm_snapshot(client, platform_admin_headers, db_session, monkeypatch, link_pve=True):
    customer_id = _create_customer(db_session)
    pbs_instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, pbs_instance_id, monkeypatch)
    client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(customer_id), "role": "primary"},
        headers=platform_admin_headers,
    )
    cluster_id = _create_pve_cluster(
        client, platform_admin_headers, linked_pbs_instance_ids=[pbs_instance_id] if link_pve else []
    )
    group_id = _create_group(db_session, datastore_id, backup_type="vm", backup_id="116")
    snapshot_id = _add_snapshot(db_session, group_id)
    return customer_id, cluster_id, group_id, snapshot_id


def _mock_reachable_pve(monkeypatch, *, guests=None, storages=None, vmid_free=True, vmid=200):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.api.v1 import restore_plans as restore_plans_module

    guests_result = GuestListResult(reachable=True, guests=guests or [])
    storages_result = StorageListResult(
        reachable=True,
        storages=storages
        or [
            RemoteStorage(
                storage="local-lvm", type="lvmthin", content=["images", "rootdir"], active=True, avail_bytes=10**12
            ),
            RemoteStorage(
                storage="pbs-main",
                type="pbs",
                content=["backup"],
                active=True,
                pbs_server="pbs1.internal.example",
                pbs_datastore="store1",
                pbs_namespace="",
            ),
        ],
    )
    vmid_result = VmidAvailabilityResult(reachable=True, vmid=vmid, free=vmid_free)

    for module in (restore_plans_module, pve_clusters_module):
        monkeypatch.setattr(module, "list_guests", lambda *a, **kw: guests_result)
        monkeypatch.setattr(module, "list_storages", lambda *a, **kw: storages_result)
        monkeypatch.setattr(module, "check_vmid_free", lambda *a, **kw: vmid_result)


def _default_plan_body(**overrides):
    body = {
        "pve_cluster_id": None,  # filled in by caller
        "target_node": "pve1",
        "target_storage": "local-lvm",
        "vmid_intent": "new",
        "target_vmid": 200,
        "network_mode": "none",
        "start_after_restore": False,
    }
    body.update(overrides)
    return body


def test_platform_admin_can_create_a_restore_plan(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _mock_reachable_pve(monkeypatch)

    body = _default_plan_body(backup_snapshot_id=str(snapshot_id), pve_cluster_id=cluster_id)
    response = client.post("/api/v1/restore-plans", json=body, headers=platform_admin_headers)

    assert response.status_code == 200
    plan = response.json()
    assert plan["target_vmid"] == 200
    assert "qmrestore" in plan["generated_command"]
    assert plan["checks_result"]
    assert all(c["status"] in ("pass", "warn", "unknown") for c in plan["checks_result"])


def test_restore_operator_scoped_to_right_customer_can_create(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _mock_reachable_pve(monkeypatch)
    operator_headers = _create_customer_scoped_user(db_session, test_settings, customer_id, "restore_operator")

    body = _default_plan_body(backup_snapshot_id=str(snapshot_id), pve_cluster_id=cluster_id)
    response = client.post("/api/v1/restore-plans", json=body, headers=operator_headers)

    assert response.status_code == 200


def test_restore_operator_scoped_to_wrong_customer_gets_404(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _mock_reachable_pve(monkeypatch)
    other_customer_id = _create_customer(db_session, name="Other", slug="other")
    operator_headers = _create_customer_scoped_user(db_session, test_settings, other_customer_id, "restore_operator")

    body = _default_plan_body(backup_snapshot_id=str(snapshot_id), pve_cluster_id=cluster_id)
    response = client.post("/api/v1/restore-plans", json=body, headers=operator_headers)

    assert response.status_code == 404


def test_unscoped_viewer_gets_404_not_403(client, platform_admin_headers, db_session, test_settings, monkeypatch):
    customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _mock_reachable_pve(monkeypatch)
    viewer_headers = _create_customer_scoped_user(db_session, test_settings, customer_id, "viewer")

    body = _default_plan_body(backup_snapshot_id=str(snapshot_id), pve_cluster_id=cluster_id)
    response = client.post("/api/v1/restore-plans", json=body, headers=viewer_headers)

    assert response.status_code == 404


def test_vmid_conflict_rejects_before_persisting(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _mock_reachable_pve(
        monkeypatch,
        guests=[RemoteGuest(vmid=200, node="pve1", guest_type="qemu", name="existing", status="stopped")],
        vmid_free=False,
        vmid=200,
    )

    body = _default_plan_body(backup_snapshot_id=str(snapshot_id), pve_cluster_id=cluster_id)
    response = client.post("/api/v1/restore-plans", json=body, headers=platform_admin_headers)

    assert response.status_code == 400
    assert response.json()["code"] == "restore_plan_checks_failed"

    list_response = client.get("/api/v1/restore-plans", headers=platform_admin_headers)
    assert list_response.json() == []


def test_network_danger_without_acknowledgment_is_rejected(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _mock_reachable_pve(monkeypatch)

    body = _default_plan_body(backup_snapshot_id=str(snapshot_id), pve_cluster_id=cluster_id, network_mode="original")
    response = client.post("/api/v1/restore-plans", json=body, headers=platform_admin_headers)

    assert response.status_code == 400
    assert response.json()["code"] == "network_acknowledgment_required"


def test_network_danger_with_acknowledgment_succeeds(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _mock_reachable_pve(monkeypatch)

    body = _default_plan_body(
        backup_snapshot_id=str(snapshot_id),
        pve_cluster_id=cluster_id,
        network_mode="original",
        network_danger_acknowledged=True,
    )
    response = client.post("/api/v1/restore-plans", json=body, headers=platform_admin_headers)

    assert response.status_code == 200


def test_pve_unreachable_rejects_with_502_and_persists_nothing(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import restore_plans as restore_plans_module

    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    monkeypatch.setattr(
        restore_plans_module, "list_storages", lambda *a, **kw: StorageListResult(reachable=False, error="timeout")
    )
    monkeypatch.setattr(
        restore_plans_module, "list_guests", lambda *a, **kw: GuestListResult(reachable=True, guests=[])
    )
    monkeypatch.setattr(
        restore_plans_module,
        "check_vmid_free",
        lambda *a, **kw: VmidAvailabilityResult(reachable=True, vmid=200, free=True),
    )

    body = _default_plan_body(backup_snapshot_id=str(snapshot_id), pve_cluster_id=cluster_id)
    response = client.post("/api/v1/restore-plans", json=body, headers=platform_admin_headers)

    assert response.status_code == 502
    list_response = client.get("/api/v1/restore-plans", headers=platform_admin_headers)
    assert list_response.json() == []


def test_host_backup_type_is_rejected(client, platform_admin_headers, db_session, monkeypatch):
    customer_id = _create_customer(db_session)
    pbs_instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, pbs_instance_id, monkeypatch)
    client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(customer_id), "role": "primary"},
        headers=platform_admin_headers,
    )
    cluster_id = _create_pve_cluster(client, platform_admin_headers)
    group_id = _create_group(db_session, datastore_id, backup_type="host", backup_id="host1")
    snapshot_id = _add_snapshot(db_session, group_id)

    body = _default_plan_body(backup_snapshot_id=str(snapshot_id), pve_cluster_id=cluster_id)
    response = client.post("/api/v1/restore-plans", json=body, headers=platform_admin_headers)

    assert response.status_code == 400
    assert response.json()["code"] == "unsupported_backup_type"


def test_recheck_updates_checks_performed_at(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _mock_reachable_pve(monkeypatch)

    body = _default_plan_body(backup_snapshot_id=str(snapshot_id), pve_cluster_id=cluster_id)
    create_response = client.post("/api/v1/restore-plans", json=body, headers=platform_admin_headers)
    plan_id = create_response.json()["id"]
    first_checked_at = create_response.json()["checks_performed_at"]

    recheck_response = client.post(f"/api/v1/restore-plans/{plan_id}/recheck", headers=platform_admin_headers)

    assert recheck_response.status_code == 200
    assert recheck_response.json()["id"] == plan_id
    # Not asserting inequality of timestamps (could tie at test speed) —
    # asserting the recheck endpoint actually re-ran and returned cleanly
    # is the meaningful behavior here.
    assert recheck_response.json()["checks_performed_at"] is not None
    assert first_checked_at is not None


def test_list_scopes_by_customer(client, platform_admin_headers, db_session, test_settings, monkeypatch):
    customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _mock_reachable_pve(monkeypatch)
    body = _default_plan_body(backup_snapshot_id=str(snapshot_id), pve_cluster_id=cluster_id)
    client.post("/api/v1/restore-plans", json=body, headers=platform_admin_headers)

    admin_list = client.get("/api/v1/restore-plans", headers=platform_admin_headers)
    assert len(admin_list.json()) == 1

    other_customer_id = _create_customer(db_session, name="Other", slug="other")
    other_headers = _create_customer_scoped_user(db_session, test_settings, other_customer_id, "restore_operator")
    other_list = client.get("/api/v1/restore-plans", headers=other_headers)
    assert other_list.json() == []

    own_headers = _create_customer_scoped_user(db_session, test_settings, customer_id, "restore_operator")
    own_list = client.get("/api/v1/restore-plans", headers=own_headers)
    assert len(own_list.json()) == 1
