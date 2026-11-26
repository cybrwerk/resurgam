import uuid

"""EPIC 9 (manifest §11.2) tests: four-eyes approval and PVE-API restore
execution/polling. The underlying PVE write calls (`start_vm_restore`,
`get_task_status`, etc.) are mocked at the `api.v1.restore_execution`
module boundary, same convention as `test_restore_plans_api.py`'s
`_mock_reachable_pve` for the read-only calls — what's tested here is
RESURGAM's own approval-gate/state-machine/RBAC wiring around them.
"""

from datetime import UTC, datetime

from resurgam_api.connectivity.models import (
    DatastoreListResult,
    GuestListResult,
    PveTaskStatus,
    RemoteDatastore,
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


def _create_pve_cluster(client, headers, linked_pbs_instance_ids=None, require_restore_approval=False) -> str:
    response = client.post(
        "/api/v1/pve-clusters",
        json={
            "display_name": "Primary PVE",
            "host": "pve1.internal.example",
            "port": 8006,
            "tls_trust_mode": "ca",
            "api_token": "root@pam!token=secretvalue",
            "linked_pbs_instance_ids": linked_pbs_instance_ids or [],
            "require_restore_approval": require_restore_approval,
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

    user = User(email=f"user-{role_key}-{customer_id}-{uuid.uuid4()}@example.com", display_name="Scoped User")
    db_session.add(user)
    db_session.flush()

    role = db_session.query(Role).filter_by(key=RoleKey(role_key).value).one()
    db_session.add(
        UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.CUSTOMER.value, scope_id=customer_id)
    )
    db_session.commit()

    token, _ = create_session_token(user.id, test_settings)
    return {"Authorization": f"Bearer {token}"}, user.id


def _setup_vm_snapshot(client, platform_admin_headers, db_session, monkeypatch, require_restore_approval=False):
    customer_id = _create_customer(db_session)
    pbs_instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, pbs_instance_id, monkeypatch)
    client.post(
        f"/api/v1/datastores/{datastore_id}/assign-customer",
        json={"customer_id": str(customer_id), "role": "primary"},
        headers=platform_admin_headers,
    )
    cluster_id = _create_pve_cluster(
        client,
        platform_admin_headers,
        linked_pbs_instance_ids=[pbs_instance_id],
        require_restore_approval=require_restore_approval,
    )
    group_id = _create_group(db_session, datastore_id, backup_type="vm", backup_id="116")
    snapshot_id = _add_snapshot(db_session, group_id)
    return customer_id, cluster_id, group_id, snapshot_id


def _mock_reachable_pve(monkeypatch, *, vmid_free=True, vmid=200):
    from resurgam_api.api.v1 import pve_clusters as pve_clusters_module
    from resurgam_api.api.v1 import restore_plans as restore_plans_module

    guests_result = GuestListResult(reachable=True, guests=[])
    storages_result = StorageListResult(
        reachable=True,
        storages=[
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

    from resurgam_api.api.v1 import restore_execution as restore_execution_module

    monkeypatch.setattr(restore_execution_module, "list_storages", lambda *a, **kw: storages_result)


def _default_plan_body(**overrides):
    body = {
        "pve_cluster_id": None,
        "target_node": "pve1",
        "target_storage": "local-lvm",
        "vmid_intent": "new",
        "target_vmid": 200,
        "network_mode": "none",
        "start_after_restore": False,
    }
    body.update(overrides)
    return body


def _create_plan(client, headers, cluster_id, snapshot_id, **overrides):
    body = _default_plan_body(backup_snapshot_id=str(snapshot_id), pve_cluster_id=cluster_id, **overrides)
    response = client.post("/api/v1/restore-plans", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _mock_pve_writes(monkeypatch, *, upid="UPID:pve1:00001234:00000ABC:12345678:qmrestore:200:resurgam@pve!dev:"):
    from resurgam_api.api.v1 import restore_execution as module

    calls = {
        "start_vm_restore": 0,
        "set_vm_network_config": 0,
        "network_updates": [],
        "start_guest": 0,
        "get_task_status": [],
        "start_ct_restore": 0,
        "get_vm_config": 0,
    }

    def fake_start_vm_restore(*a, **kw):
        calls["start_vm_restore"] += 1
        return upid

    def fake_start_ct_restore(*a, **kw):
        calls["start_ct_restore"] += 1
        return upid

    def fake_set_guest_network_config(*a, **kw):
        calls["set_vm_network_config"] += 1
        calls["network_updates"].append(kw["nets"])

    def fake_start_guest(*a, **kw):
        calls["start_guest"] += 1
        return upid

    def fake_get_vm_config(*a, **kw):
        calls["get_vm_config"] += 1
        return {
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,tag=5",
            "net1": "e1000=AA:BB:CC:DD:EE:00,bridge=vmbr0,tag=35",
            "name": "x",
        }

    monkeypatch.setattr(module, "start_vm_restore", fake_start_vm_restore)
    monkeypatch.setattr(module, "start_ct_restore", fake_start_ct_restore)
    monkeypatch.setattr(module, "set_guest_network_config", fake_set_guest_network_config)
    monkeypatch.setattr(module, "start_guest", fake_start_guest)
    monkeypatch.setattr(module, "get_vm_config", fake_get_vm_config)
    return calls


def _mock_task_status(monkeypatch, calls, statuses):
    """`statuses` is a list of PveTaskStatus consumed in order, one per
    `get_task_status` call, holding the last value once exhausted.
    """
    from resurgam_api.api.v1 import restore_execution as module

    state = {"i": 0}

    def fake_get_task_status(*a, **kw):
        calls["get_task_status"].append(1)
        i = min(state["i"], len(statuses) - 1)
        state["i"] += 1
        return statuses[i]

    monkeypatch.setattr(module, "get_task_status", fake_get_task_status)


# --- approval gate -----------------------------------------------------


def test_self_approval_is_forbidden(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch, require_restore_approval=True
    )
    _mock_reachable_pve(monkeypatch)
    plan = _create_plan(client, platform_admin_headers, cluster_id, snapshot_id)
    assert plan["approval_status"] == "pending"

    # platform_admin created it -- platform_admin is NOT exempt from the self-approval check
    response = client.post(f"/api/v1/restore-plans/{plan['id']}/approve", headers=platform_admin_headers)
    assert response.status_code == 403
    assert response.json()["code"] == "self_approval_forbidden"


def test_a_different_restore_operator_can_approve(
    client, platform_admin_headers, db_session, test_settings, monkeypatch
):
    customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch, require_restore_approval=True
    )
    _mock_reachable_pve(monkeypatch)
    creator_headers, _creator_id = _create_customer_scoped_user(
        db_session, test_settings, customer_id, "restore_operator"
    )
    plan = _create_plan(client, creator_headers, cluster_id, snapshot_id)

    approver_headers, _approver_id = _create_customer_scoped_user(
        db_session, test_settings, customer_id, "restore_operator"
    )
    response = client.post(f"/api/v1/restore-plans/{plan['id']}/approve", headers=approver_headers)
    assert response.status_code == 200
    assert response.json()["approval_status"] == "approved"


def test_approve_out_of_scope_customer_gets_404(client, platform_admin_headers, db_session, test_settings, monkeypatch):
    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch, require_restore_approval=True
    )
    _mock_reachable_pve(monkeypatch)
    plan = _create_plan(client, platform_admin_headers, cluster_id, snapshot_id)

    other_customer_id = _create_customer(db_session, name="Other", slug="other")
    other_headers, _ = _create_customer_scoped_user(db_session, test_settings, other_customer_id, "restore_operator")
    response = client.post(f"/api/v1/restore-plans/{plan['id']}/approve", headers=other_headers)
    assert response.status_code == 404


def test_reject_is_terminal_and_blocks_execute(client, platform_admin_headers, db_session, test_settings, monkeypatch):
    customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch, require_restore_approval=True
    )
    _mock_reachable_pve(monkeypatch)
    creator_headers, _ = _create_customer_scoped_user(db_session, test_settings, customer_id, "restore_operator")
    plan = _create_plan(client, creator_headers, cluster_id, snapshot_id)

    approver_headers, _ = _create_customer_scoped_user(db_session, test_settings, customer_id, "restore_operator")
    reject_response = client.post(
        f"/api/v1/restore-plans/{plan['id']}/reject", json={"reason": "wrong target"}, headers=approver_headers
    )
    assert reject_response.status_code == 200
    assert reject_response.json()["approval_status"] == "rejected"

    execute_response = client.post(f"/api/v1/restore-plans/{plan['id']}/execute", headers=creator_headers)
    assert execute_response.status_code == 409
    assert execute_response.json()["code"] == "approval_rejected"


def test_execute_blocked_while_approval_pending(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch, require_restore_approval=True
    )
    _mock_reachable_pve(monkeypatch)
    plan = _create_plan(client, platform_admin_headers, cluster_id, snapshot_id)

    response = client.post(f"/api/v1/restore-plans/{plan['id']}/execute", headers=platform_admin_headers)
    assert response.status_code == 409
    assert response.json()["code"] == "approval_pending"


# --- execute / double-execute -------------------------------------------


def test_execute_starts_restore_and_stores_upid(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _mock_reachable_pve(monkeypatch)
    plan = _create_plan(client, platform_admin_headers, cluster_id, snapshot_id)
    calls = _mock_pve_writes(monkeypatch)

    response = client.post(f"/api/v1/restore-plans/{plan['id']}/execute", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["execution_status"] == "restore_requested"
    assert body["upid"]
    assert calls["start_vm_restore"] == 1


def test_execute_fails_loudly_when_no_matching_pbs_storage(client, platform_admin_headers, db_session, monkeypatch):
    """Regression test: if `list_storages` has no PVE "pbs"-type storage
    whose `pbs_datastore` matches the snapshot's real datastore, the
    restore must fail with a clear error -- not silently build the
    archive volid against `plan.target_storage` (the restore
    *destination* disk storage), which would restore from the wrong
    place entirely.
    """
    from resurgam_api.api.v1 import restore_execution as restore_execution_module

    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _mock_reachable_pve(monkeypatch)
    plan = _create_plan(client, platform_admin_headers, cluster_id, snapshot_id)

    # No "pbs"-type storage in this node's list at all -- e.g. the PBS
    # storage is defined only on other cluster nodes.
    no_pbs_storage = StorageListResult(
        reachable=True,
        storages=[
            RemoteStorage(storage="local-lvm", type="lvmthin", content=["images"], active=True, avail_bytes=10**12)
        ],
    )
    monkeypatch.setattr(restore_execution_module, "list_storages", lambda *a, **kw: no_pbs_storage)

    response = client.post(f"/api/v1/restore-plans/{plan['id']}/execute", headers=platform_admin_headers)
    assert response.status_code == 502
    assert response.json()["code"] == "pbs_storage_not_found"


def test_double_execute_is_rejected(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _mock_reachable_pve(monkeypatch)
    plan = _create_plan(client, platform_admin_headers, cluster_id, snapshot_id)
    _mock_pve_writes(monkeypatch)

    first = client.post(f"/api/v1/restore-plans/{plan['id']}/execute", headers=platform_admin_headers)
    assert first.status_code == 200

    second = client.post(f"/api/v1/restore-plans/{plan['id']}/execute", headers=platform_admin_headers)
    assert second.status_code == 409
    assert second.json()["code"] == "already_executed"


def _mock_stop_task(monkeypatch, *, raises=None):
    from resurgam_api.api.v1 import restore_execution as module

    calls = {"stop_task": 0}

    def fake_stop_task(*a, **kw):
        calls["stop_task"] += 1
        if raises is not None:
            raise raises

    monkeypatch.setattr(module, "stop_task", fake_stop_task)
    return calls


# --- abort -----------------------------------------------------------------


def test_abort_stops_task_and_marks_restore_failed(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _mock_reachable_pve(monkeypatch)
    plan = _create_plan(client, platform_admin_headers, cluster_id, snapshot_id)
    _mock_pve_writes(monkeypatch)
    client.post(f"/api/v1/restore-plans/{plan['id']}/execute", headers=platform_admin_headers)
    calls = _mock_stop_task(monkeypatch)

    response = client.post(f"/api/v1/restore-plans/{plan['id']}/execution/abort", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["execution_status"] == "restore_failed"
    assert "aborted" in body["execution_error"].lower()
    assert calls["stop_task"] == 1


def test_abort_before_execute_returns_409(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _mock_reachable_pve(monkeypatch)
    plan = _create_plan(client, platform_admin_headers, cluster_id, snapshot_id)

    response = client.post(f"/api/v1/restore-plans/{plan['id']}/execution/abort", headers=platform_admin_headers)
    assert response.status_code == 409
    assert response.json()["code"] == "execution_not_running"


def test_abort_after_completed_returns_409(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.connectivity.models import PveTaskStatus

    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _mock_reachable_pve(monkeypatch)
    plan = _create_plan(client, platform_admin_headers, cluster_id, snapshot_id)
    calls = _mock_pve_writes(monkeypatch)
    client.post(f"/api/v1/restore-plans/{plan['id']}/execute", headers=platform_admin_headers)
    _mock_task_status(monkeypatch, calls, [PveTaskStatus(reachable=True, status="stopped", exitstatus="OK")])
    client.post(f"/api/v1/restore-plans/{plan['id']}/execution/poll", headers=platform_admin_headers)

    response = client.post(f"/api/v1/restore-plans/{plan['id']}/execution/abort", headers=platform_admin_headers)
    assert response.status_code == 409
    assert response.json()["code"] == "execution_not_running"


def test_abort_propagates_pve_unreachable_without_changing_plan(
    client, platform_admin_headers, db_session, monkeypatch
):
    from resurgam_api.connectivity.pve_api import PveWriteError

    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _mock_reachable_pve(monkeypatch)
    plan = _create_plan(client, platform_admin_headers, cluster_id, snapshot_id)
    _mock_pve_writes(monkeypatch)
    client.post(f"/api/v1/restore-plans/{plan['id']}/execute", headers=platform_admin_headers)
    _mock_stop_task(monkeypatch, raises=PveWriteError("connection refused"))

    response = client.post(f"/api/v1/restore-plans/{plan['id']}/execution/abort", headers=platform_admin_headers)
    assert response.status_code == 502
    assert response.json()["code"] == "pve_unreachable"

    unchanged = client.get(f"/api/v1/restore-plans/{plan['id']}", headers=platform_admin_headers)
    assert unchanged.json()["execution_status"] == "restore_requested"


# --- poll / finalize -----------------------------------------------------


def test_poll_transitions_running_then_finalizes_once_on_completion(
    client, platform_admin_headers, db_session, monkeypatch
):
    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _mock_reachable_pve(monkeypatch)
    plan = _create_plan(client, platform_admin_headers, cluster_id, snapshot_id, network_mode="none")
    calls = _mock_pve_writes(monkeypatch)
    client.post(f"/api/v1/restore-plans/{plan['id']}/execute", headers=platform_admin_headers)

    _mock_task_status(
        monkeypatch,
        calls,
        [
            PveTaskStatus(reachable=True, status="running", exitstatus=None),
            PveTaskStatus(reachable=True, status="stopped", exitstatus="OK"),
        ],
    )

    first_poll = client.post(f"/api/v1/restore-plans/{plan['id']}/execution/poll", headers=platform_admin_headers)
    assert first_poll.status_code == 200
    assert first_poll.json()["execution_status"] == "restore_running"
    assert calls["set_vm_network_config"] == 0

    second_poll = client.post(f"/api/v1/restore-plans/{plan['id']}/execution/poll", headers=platform_admin_headers)
    assert second_poll.status_code == 200
    body = second_poll.json()
    assert body["execution_status"] == "completed"
    # network_mode="none" -> reads back the real NICs and disconnects every one, exactly once
    # (found live 2026-11-21: only net0 used to be touched).
    assert calls["get_vm_config"] == 1
    assert calls["set_vm_network_config"] == 1
    assert calls["network_updates"] == [
        {
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,tag=5,link_down=1",
            "net1": "e1000=AA:BB:CC:DD:EE:00,bridge=vmbr0,tag=35,link_down=1",
        }
    ]
    assert calls["start_guest"] == 0  # start_after_restore was False

    # A further poll on a terminal plan must be a safe no-op -- no duplicate finalize.
    third_poll = client.post(f"/api/v1/restore-plans/{plan['id']}/execution/poll", headers=platform_admin_headers)
    assert third_poll.status_code == 200
    assert third_poll.json()["execution_status"] == "completed"
    assert calls["set_vm_network_config"] == 1
    assert len(calls["get_task_status"]) == 2  # the third poll never called PVE again


def test_poll_marks_restore_failed_on_nonzero_exitstatus(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _mock_reachable_pve(monkeypatch)
    plan = _create_plan(client, platform_admin_headers, cluster_id, snapshot_id)
    calls = _mock_pve_writes(monkeypatch)
    client.post(f"/api/v1/restore-plans/{plan['id']}/execute", headers=platform_admin_headers)

    _mock_task_status(monkeypatch, calls, [PveTaskStatus(reachable=True, status="stopped", exitstatus="some error")])

    response = client.post(f"/api/v1/restore-plans/{plan['id']}/execution/poll", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["execution_status"] == "restore_failed"
    assert body["execution_error"]
    assert calls["set_vm_network_config"] == 0


def test_poll_before_execute_returns_409(client, platform_admin_headers, db_session, monkeypatch):
    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _mock_reachable_pve(monkeypatch)
    plan = _create_plan(client, platform_admin_headers, cluster_id, snapshot_id)

    response = client.post(f"/api/v1/restore-plans/{plan['id']}/execution/poll", headers=platform_admin_headers)
    assert response.status_code == 409
    assert response.json()["code"] == "not_started"


def test_poll_marks_stale_execution_as_failed_when_pve_unreachable_too_long(
    client, platform_admin_headers, db_session, monkeypatch
):
    """With no separate worker/scheduler for this feature, a plan we've
    been *unable to reach PVE about* for far longer than any transient
    outage should take must self-heal on the next poll -- but only
    because PVE itself can't be reached, not merely because time passed.
    """
    from resurgam_api.models.restore import RestorePlan

    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _mock_reachable_pve(monkeypatch)
    plan = _create_plan(client, platform_admin_headers, cluster_id, snapshot_id)
    _mock_pve_writes(monkeypatch)
    client.post(f"/api/v1/restore-plans/{plan['id']}/execute", headers=platform_admin_headers)

    from datetime import datetime, timedelta

    db_plan = db_session.get(RestorePlan, plan["id"])
    db_plan.executed_at = datetime.now(UTC) - timedelta(hours=999)
    db_session.commit()

    from resurgam_api.api.v1 import restore_execution as module

    monkeypatch.setattr(
        module,
        "get_task_status",
        lambda *a, **kw: PveTaskStatus(reachable=False, error="connection refused"),
    )

    response = client.post(f"/api/v1/restore-plans/{plan['id']}/execution/poll", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["execution_status"] == "restore_failed"
    assert "Could not reach PVE" in body["execution_error"]


def test_poll_does_not_force_fail_a_long_running_restore_pve_still_confirms(
    client, platform_admin_headers, db_session, monkeypatch
):
    """The core bug fix: a restore that's been running a very long time
    is NOT force-failed by the staleness clock as long as PVE itself keeps
    answering -- only a prolonged inability to reach PVE at all is treated
    as stuck. Real restores of large disks can legitimately take hours.
    """
    from resurgam_api.models.restore import RestorePlan

    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(
        client, platform_admin_headers, db_session, monkeypatch
    )
    _mock_reachable_pve(monkeypatch)
    plan = _create_plan(client, platform_admin_headers, cluster_id, snapshot_id)
    calls = _mock_pve_writes(monkeypatch)
    client.post(f"/api/v1/restore-plans/{plan['id']}/execute", headers=platform_admin_headers)

    from datetime import datetime, timedelta

    db_plan = db_session.get(RestorePlan, plan["id"])
    db_plan.executed_at = datetime.now(UTC) - timedelta(hours=999)
    db_session.commit()

    _mock_task_status(monkeypatch, calls, [PveTaskStatus(reachable=True, status="running", exitstatus=None)])

    response = client.post(f"/api/v1/restore-plans/{plan['id']}/execution/poll", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["execution_status"] == "restore_running"
    assert body["execution_last_confirmed_at"] is not None
