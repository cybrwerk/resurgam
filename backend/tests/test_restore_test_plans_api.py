from datetime import UTC, datetime, timedelta

from resurgam_api.connectivity.models import VmidAvailabilityResult


def _create_customer(db_session, name="Acme", slug="acme"):
    from resurgam_api.models.tenancy import Customer

    customer = Customer(
        name=name, slug=slug, debitor_number=f"debitor-{slug}", match_code=slug[:3].upper().ljust(3, "X")
    )
    db_session.add(customer)
    db_session.flush()
    db_session.commit()
    return customer.id


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
    from resurgam_api.connectivity.models import DatastoreListResult, RemoteDatastore

    monkeypatch.setattr(
        pbs_instances_module,
        "list_datastores",
        lambda *a, **kw: DatastoreListResult(reachable=True, datastores=[RemoteDatastore(name=name)]),
    )
    response = client.post(f"/api/v1/pbs-instances/{instance_id}/datastores", json={"name": name}, headers=headers)
    assert response.status_code == 200
    return response.json()["id"]


def _create_lab_cluster(client, headers, appliance_template_vmid=9000) -> str:
    response = client.post(
        "/api/v1/pve-clusters",
        json={
            "display_name": "Lab PVE",
            "host": "pve1.internal.example",
            "port": 8006,
            "tls_trust_mode": "ca",
            "api_token": "root@pam!token=secretvalue",
            "is_lab_compute": True,
            "lab_appliance_template_vmid": appliance_template_vmid,
            "lab_snippet_storage_id": "local",
            "lab_appliance_uplink_bridge": "vmbr0",
        },
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()["id"]


def _create_group(db_session, datastore_id, backup_id="116"):
    from resurgam_api.models.inventory import BackupGroup

    group = BackupGroup(datastore_id=datastore_id, backup_type="vm", backup_id=backup_id, namespace="")
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


def _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch, member_count=1):
    customer_id = _create_customer(db_session)
    pbs_instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, pbs_instance_id, monkeypatch)
    cluster_id = _create_lab_cluster(client, platform_admin_headers)

    members = []
    for i in range(member_count):
        group_id = _create_group(db_session, datastore_id, backup_id=f"11{i}")
        _add_snapshot(db_session, group_id)
        members.append({"backup_group_id": str(group_id), "startup_order": i, "check_types": ["icmp"]})

    app_group = client.post(
        "/api/v1/application-groups",
        json={
            "customer_id": str(customer_id),
            "name": "AppGroup",
            "members": members,
            "lab_subnet_cidr": "10.99.0.0/24",
            "lab_gateway_ip": "10.99.0.1",
        },
        headers=platform_admin_headers,
    ).json()

    return customer_id, cluster_id, app_group["id"]


def _mock_pve_for_creation(monkeypatch, vmid=200, taken_vmids: set[int] | None = None):
    from resurgam_api.api.v1 import restore_test_plans as module

    monkeypatch.setattr(
        module, "next_free_vmid", lambda *a, **kw: VmidAvailabilityResult(reachable=True, vmid=vmid, free=True)
    )
    taken = taken_vmids or set()
    monkeypatch.setattr(
        module,
        "check_vmid_free",
        lambda host, port, trust, token, candidate, timeout=5.0: VmidAvailabilityResult(
            reachable=True, vmid=candidate, free=candidate not in taken
        ),
    )


def test_create_restore_test_plan(client, platform_admin_headers, db_session, monkeypatch):
    _, cluster_id, app_group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    _mock_pve_for_creation(monkeypatch)

    response = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group_id,
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "planned"
    assert len(body["members"]) == 1
    assert body["members"][0]["target_vmid"] == 200
    assert body["appliance_vmid"] == 201
    assert len(body["vnet_name"]) <= 8


def test_create_restore_test_plan_reprobes_vmid_per_member(client, platform_admin_headers, db_session, monkeypatch):
    """Regression test: a multi-member Application Group must get a real,
    independently-checked VMID per member (and for the appliance) --
    not just `next_free_vmid()`'s single result incremented locally,
    which can silently collide with an unrelated existing guest.
    """
    _, cluster_id, app_group_id = _setup_run_prereqs(
        client, platform_admin_headers, db_session, monkeypatch, member_count=3
    )
    # vmid 201 (what a naive local increment would assign to member #2)
    # is already taken by an unrelated guest on this cluster.
    _mock_pve_for_creation(monkeypatch, vmid=200, taken_vmids={201})

    response = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group_id,
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    members_by_order = sorted(body["members"], key=lambda m: m["target_vmid"])
    vmids = [m["target_vmid"] for m in members_by_order]

    assert vmids == [200, 202, 203]
    assert body["appliance_vmid"] == 204
    assert len(set(vmids) | {body["appliance_vmid"]}) == 4


def test_create_restore_test_plan_rejects_non_lab_cluster(client, platform_admin_headers, db_session, monkeypatch):
    customer_id = _create_customer(db_session)
    pbs_instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, pbs_instance_id, monkeypatch)
    group_id = _create_group(db_session, datastore_id)
    _add_snapshot(db_session, group_id)
    app_group = client.post(
        "/api/v1/application-groups",
        json={
            "customer_id": str(customer_id),
            "name": "AppGroup",
            "members": [{"backup_group_id": str(group_id), "startup_order": 0, "check_types": []}],
        },
        headers=platform_admin_headers,
    ).json()

    normal_cluster = client.post(
        "/api/v1/pve-clusters",
        json={
            "display_name": "Normal PVE",
            "host": "pve2.internal.example",
            "port": 8006,
            "tls_trust_mode": "ca",
            "api_token": "x=y",
        },
        headers=platform_admin_headers,
    ).json()

    response = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group["id"],
            "pve_cluster_id": normal_cluster["id"],
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "not_a_lab_cluster"


def _mock_pve_for_execution(monkeypatch):
    from resurgam_api.api.v1 import restore_test_plans as module

    monkeypatch.setattr(module, "get_sdn_zone", lambda *a, **kw: None)
    monkeypatch.setattr(module, "create_sdn_zone", lambda *a, **kw: None)
    monkeypatch.setattr(module, "apply_sdn_changes", lambda *a, **kw: None)
    monkeypatch.setattr(module, "create_pool", lambda *a, **kw: None)
    monkeypatch.setattr(module, "create_sdn_vnet", lambda *a, **kw: None)
    return module


def _provision_appliance(client, agent_headers, plan_id, monkeypatch):
    """Drives Phase B's deferred appliance flow to completion: mocks the
    PVE clone/config calls `lab_agent.py`'s write-snippet confirmation
    makes, then plays the agent's own real two-call sequence
    (`GET /lab-agent/appliance-jobs` -> `POST .../write-snippet`) so the
    plan leaves `provisioning_appliance` exactly as production does.
    """
    from resurgam_api.api.v1 import lab_agent as lab_agent_module
    from resurgam_api.connectivity.models import PveTaskStatus

    monkeypatch.setattr(lab_agent_module, "clone_vm", lambda *a, **kw: "UPID:pve:1:qmclone:900:root@pam:")
    monkeypatch.setattr(
        lab_agent_module,
        "get_task_status",
        lambda *a, **kw: PveTaskStatus(reachable=True, status="stopped", exitstatus="OK"),
    )
    monkeypatch.setattr(lab_agent_module, "configure_lab_appliance_network", lambda *a, **kw: None)
    monkeypatch.setattr(lab_agent_module, "start_guest", lambda *a, **kw: "UPID:pve:2:qmstart:900:root@pam:")

    jobs = client.get("/api/v1/lab-agent/appliance-jobs", headers=agent_headers).json()
    assert any(j["plan_id"] == plan_id for j in jobs)
    response = client.post(f"/api/v1/lab-agent/appliance-jobs/{plan_id}/write-snippet", headers=agent_headers)
    assert response.status_code == 204


def test_execute_restore_test_plan_starts_first_member(client, platform_admin_headers, db_session, monkeypatch):
    _, cluster_id, app_group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    _mock_pve_for_creation(monkeypatch)
    plan = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group_id,
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    ).json()

    _mock_pve_for_execution(monkeypatch)
    response = client.post(f"/api/v1/lab-runs/{plan['id']}/execute", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["appliance_run_token"]
    assert body["plan"]["status"] == "provisioning_appliance"
    assert body["plan"]["members"][0]["member_status"] == "queued"


def test_execute_restore_test_plan_rejects_double_execute(client, platform_admin_headers, db_session, monkeypatch):
    _, cluster_id, app_group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    _mock_pve_for_creation(monkeypatch)
    plan = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group_id,
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    ).json()
    _mock_pve_for_execution(monkeypatch)
    client.post(f"/api/v1/lab-runs/{plan['id']}/execute", headers=platform_admin_headers)

    response = client.post(f"/api/v1/lab-runs/{plan['id']}/execute", headers=platform_admin_headers)
    assert response.status_code == 409
    assert response.json()["code"] == "already_executed"


def _agent_headers(client, platform_admin_headers, cluster_id):
    token = client.post(f"/api/v1/pve-clusters/{cluster_id}/lab-agent-token", headers=platform_admin_headers).json()[
        "token"
    ]
    return {"Authorization": f"Bearer {token}"}


def test_poll_never_starts_a_run_that_was_not_executed(client, platform_admin_headers, db_session, monkeypatch):
    """Live finding 2026-11-21: execute was refused (no lab network on the
    group), then one poll queued the first member anyway -- the agent created
    its VM on a VNet that never existed. A `planned` run must stay untouched
    by poll; only execute (which builds the isolation first) may start it."""
    customer_id = _create_customer(db_session)
    pbs_instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, pbs_instance_id, monkeypatch)
    cluster_id = _create_lab_cluster(client, platform_admin_headers)
    group_a = _create_group(db_session, datastore_id, backup_id="100")
    _add_snapshot(db_session, group_a)
    app_group = client.post(
        "/api/v1/application-groups",
        json={
            "customer_id": str(customer_id),
            "name": "NoLabNetwork",
            "members": [{"backup_group_id": str(group_a), "startup_order": 0, "check_types": ["guest_agent"]}],
        },
        headers=platform_admin_headers,
    ).json()
    _mock_pve_for_creation(monkeypatch)
    plan = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group["id"],
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    ).json()

    refused = client.post(f"/api/v1/lab-runs/{plan['id']}/execute", headers=platform_admin_headers)
    assert refused.status_code == 400
    assert refused.json()["code"] == "lab_network_not_configured"

    polled = client.post(f"/api/v1/lab-runs/{plan['id']}/poll", headers=platform_admin_headers)
    assert polled.status_code == 200
    assert polled.json()["status"] == "planned"
    assert polled.json()["members"][0]["member_status"] == "pending"
    agent_headers = _agent_headers(client, platform_admin_headers, cluster_id)
    assert client.get("/api/v1/lab-agent/jobs", headers=agent_headers).json() == []


def test_poll_queues_next_member_once_current_clears_its_gate(client, platform_admin_headers, db_session, monkeypatch):
    """A two-member Application Group: member 0 must reach `PROBING` (via
    the real `resurgam-lab-agent` claim/status endpoints, matching how it
    actually happens in production) before `poll` queues member 1 --
    proving the `startup_order` gate, not just that `poll` runs.
    """
    customer_id = _create_customer(db_session)
    pbs_instance_id = _create_pbs_instance(client, platform_admin_headers)
    datastore_id = _import_datastore(client, platform_admin_headers, pbs_instance_id, monkeypatch)
    cluster_id = _create_lab_cluster(client, platform_admin_headers)
    group_a = _create_group(db_session, datastore_id, backup_id="100")
    group_b = _create_group(db_session, datastore_id, backup_id="101")
    _add_snapshot(db_session, group_a)
    _add_snapshot(db_session, group_b)
    app_group = client.post(
        "/api/v1/application-groups",
        json={
            "customer_id": str(customer_id),
            "name": "TwoTier",
            "members": [
                {"backup_group_id": str(group_a), "startup_order": 0, "check_types": ["icmp"]},
                {"backup_group_id": str(group_b), "startup_order": 1, "check_types": ["icmp"]},
            ],
            "lab_subnet_cidr": "10.99.0.0/24",
            "lab_gateway_ip": "10.99.0.1",
        },
        headers=platform_admin_headers,
    ).json()

    _mock_pve_for_creation(monkeypatch)
    plan = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group["id"],
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    ).json()
    _mock_pve_for_execution(monkeypatch)
    executed = client.post(f"/api/v1/lab-runs/{plan['id']}/execute", headers=platform_admin_headers).json()
    member_0_id = executed["plan"]["members"][0]["id"]

    agent_headers = _agent_headers(client, platform_admin_headers, cluster_id)
    _provision_appliance(client, agent_headers, plan["id"], monkeypatch)

    # Polling now, before member 0 has cleared its gate, must not queue member 1.
    early_poll = client.post(f"/api/v1/lab-runs/{plan['id']}/poll", headers=platform_admin_headers).json()
    assert early_poll["members"][1]["member_status"] == "pending"

    client.post(f"/api/v1/lab-agent/jobs/{member_0_id}/claim", headers=agent_headers)
    client.post(f"/api/v1/lab-agent/jobs/{member_0_id}/status", json={"status": "booted"}, headers=agent_headers)

    response = client.post(f"/api/v1/lab-runs/{plan['id']}/poll", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["members"][0]["member_status"] == "probing"
    assert body["members"][1]["member_status"] == "queued"
    assert body["status"] == "running"


def test_probe_results_requires_valid_run_token(client, platform_admin_headers, db_session, monkeypatch):
    _, cluster_id, app_group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    _mock_pve_for_creation(monkeypatch)
    plan = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group_id,
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    ).json()
    _mock_pve_for_execution(monkeypatch)
    executed = client.post(f"/api/v1/lab-runs/{plan['id']}/execute", headers=platform_admin_headers).json()

    member_id = executed["plan"]["members"][0]["id"]
    real_token = executed["appliance_run_token"]

    bad = client.post(
        f"/api/v1/lab-runs/{plan['id']}/probe-results",
        json={
            "member_id": member_id,
            "results": [{"check_type": "icmp", "status": "pass", "checked_at": "2026-09-19T00:00:00Z"}],
        },
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert bad.status_code == 401

    good = client.post(
        f"/api/v1/lab-runs/{plan['id']}/probe-results",
        json={
            "member_id": member_id,
            "results": [{"check_type": "icmp", "status": "pass", "checked_at": "2026-09-19T00:00:00Z"}],
        },
        headers={"Authorization": f"Bearer {real_token}"},
    )
    assert good.status_code == 200
    body = good.json()
    assert body["members"][0]["member_status"] == "passed"
    assert body["status"] == "passed"


def test_probe_results_token_does_not_authenticate_a_different_run(
    client, platform_admin_headers, db_session, monkeypatch
):
    """The critical isolation boundary: one run's token must not work
    against another run's callback endpoint."""
    _customer_id, cluster_id, app_group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    _mock_pve_for_creation(monkeypatch)

    plan_a = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group_id,
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    ).json()
    _mock_pve_for_execution(monkeypatch)
    executed_a = client.post(f"/api/v1/lab-runs/{plan_a['id']}/execute", headers=platform_admin_headers).json()
    token_a = executed_a["appliance_run_token"]

    _mock_pve_for_creation(monkeypatch, vmid=300)
    plan_b = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group_id,
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    ).json()
    _mock_pve_for_execution(monkeypatch)
    executed_b = client.post(f"/api/v1/lab-runs/{plan_b['id']}/execute", headers=platform_admin_headers).json()
    member_b_id = executed_b["plan"]["members"][0]["id"]

    cross_run_attempt = client.post(
        f"/api/v1/lab-runs/{plan_b['id']}/probe-results",
        json={
            "member_id": member_b_id,
            "results": [{"check_type": "icmp", "status": "pass", "checked_at": "2026-09-19T00:00:00Z"}],
        },
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert cross_run_attempt.status_code == 401


def _mock_pve_for_cleanup(monkeypatch, member_vmids: set[int], *, existing_vmids: set[int] | None = None):
    """Mocks every PVE call `cleanup_restore_test_plan` makes once VM
    destroy was added: pool-membership confirms every given vmid as a
    real `resurgam-lab` member, `list_guests` confirms `existing_vmids`
    (defaults to the same set -- "these are real and about to be
    destroyed") as still present on the cluster, stop/destroy are
    no-ops, and the vnet delete + SDN reload succeed.
    """
    from resurgam_api.api.v1 import restore_test_plans as module
    from resurgam_api.connectivity.models import GuestListResult, PoolMembersResult, PveTaskStatus, RemoteGuest

    if existing_vmids is None:
        existing_vmids = member_vmids

    monkeypatch.setattr(module, "stop_task", lambda *a, **kw: None)
    monkeypatch.setattr(
        module,
        "get_pool_members",
        lambda *a, **kw: PoolMembersResult(reachable=True, member_vmids=frozenset(member_vmids)),
    )
    monkeypatch.setattr(
        module,
        "list_guests",
        lambda *a, **kw: GuestListResult(
            reachable=True, guests=[RemoteGuest(vmid=v, node="pve1", guest_type="qemu") for v in existing_vmids]
        ),
    )
    monkeypatch.setattr(module, "stop_vm", lambda *a, **kw: "UPID:pve:1:qmstop:200:root@pam:")
    monkeypatch.setattr(
        module, "get_task_status", lambda *a, **kw: PveTaskStatus(reachable=True, status="stopped", exitstatus="OK")
    )
    monkeypatch.setattr(module, "destroy_vm", lambda *a, **kw: None)
    monkeypatch.setattr(module, "delete_sdn_vnet", lambda *a, **kw: None)
    return module


def test_cleanup_marks_plan_completed(client, platform_admin_headers, db_session, monkeypatch):
    _, cluster_id, app_group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    _mock_pve_for_creation(monkeypatch)
    plan = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group_id,
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    ).json()
    _mock_pve_for_execution(monkeypatch)
    client.post(f"/api/v1/lab-runs/{plan['id']}/execute", headers=platform_admin_headers)

    _mock_pve_for_cleanup(monkeypatch, {200, 201})

    response = client.post(f"/api/v1/lab-runs/{plan['id']}/cleanup", headers=platform_admin_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "completed"


def test_cleanup_destroys_vms_before_deleting_the_vnet(client, platform_admin_headers, db_session, monkeypatch):
    """Raised directly by the user: cleanup must not tear down the
    network while the member VM(s)/probe appliance are still running --
    both must be destroyed first.
    """
    _, cluster_id, app_group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    _mock_pve_for_creation(monkeypatch)
    plan = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group_id,
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    ).json()
    _mock_pve_for_execution(monkeypatch)
    client.post(f"/api/v1/lab-runs/{plan['id']}/execute", headers=platform_admin_headers)

    module = _mock_pve_for_cleanup(monkeypatch, {200, 201})
    call_order = []
    monkeypatch.setattr(module, "destroy_vm", lambda *a, vmid, **kw: call_order.append(("destroy_vm", vmid)))
    monkeypatch.setattr(module, "delete_sdn_vnet", lambda *a, **kw: call_order.append(("delete_sdn_vnet", None)))

    response = client.post(f"/api/v1/lab-runs/{plan['id']}/cleanup", headers=platform_admin_headers)
    assert response.status_code == 200
    assert call_order == [("destroy_vm", 200), ("destroy_vm", 201), ("delete_sdn_vnet", None)]


def test_cleanup_refuses_to_destroy_a_vmid_not_in_the_lab_pool_but_still_real(
    client, platform_admin_headers, db_session, monkeypatch
):
    """The guardrail raised directly by the user: never trust a stored
    `target_vmid`/`appliance_vmid` blindly -- only destroy a vmid
    confirmed a real `resurgam-lab` pool member. Vmid 201 (the
    appliance) is deliberately kept out of the pool *but* still present
    in `list_guests` -- a real, unrelated VM sitting at that vmid, the
    actual danger case this guardrail exists for.
    """
    _, cluster_id, app_group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    _mock_pve_for_creation(monkeypatch)
    plan = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group_id,
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    ).json()
    _mock_pve_for_execution(monkeypatch)
    client.post(f"/api/v1/lab-runs/{plan['id']}/execute", headers=platform_admin_headers)

    module = _mock_pve_for_cleanup(monkeypatch, {200}, existing_vmids={200, 201})
    destroyed = []
    monkeypatch.setattr(module, "destroy_vm", lambda *a, vmid, **kw: destroyed.append(vmid))

    response = client.post(f"/api/v1/lab-runs/{plan['id']}/cleanup", headers=platform_admin_headers)
    assert response.status_code == 200
    assert destroyed == [200]
    assert "not a member of the" in response.json()["error"]
    assert "201" in response.json()["error"]


def test_cleanup_silently_skips_a_vmid_already_destroyed_by_a_previous_pass(
    client, platform_admin_headers, db_session, monkeypatch
):
    """Real bug found live (2026-09-27): a second cleanup call on a run
    whose VMs a previous pass already destroyed showed a scary "Refused
    to destroy... not a member of the pool" error -- a destroyed VM is,
    trivially, also absent from the pool, but that's not the guardrail's
    real target (a real, unrelated VM at that vmid). `list_guests`
    confirming the vmid doesn't exist at all must be treated as an
    already-done no-op, not an error.
    """
    _, cluster_id, app_group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    _mock_pve_for_creation(monkeypatch)
    plan = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group_id,
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    ).json()
    _mock_pve_for_execution(monkeypatch)
    client.post(f"/api/v1/lab-runs/{plan['id']}/execute", headers=platform_admin_headers)

    # Neither vmid is in the pool anymore, and neither exists on PVE
    # anymore either -- both already destroyed by an earlier cleanup pass.
    module = _mock_pve_for_cleanup(monkeypatch, set(), existing_vmids=set())
    destroyed = []
    monkeypatch.setattr(module, "destroy_vm", lambda *a, vmid, **kw: destroyed.append(vmid))

    response = client.post(f"/api/v1/lab-runs/{plan['id']}/cleanup", headers=platform_admin_headers)
    assert response.status_code == 200
    assert destroyed == []
    assert response.json()["error"] is None


def test_cleanup_when_pool_unreachable_destroys_nothing(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.connectivity.models import PoolMembersResult

    _, cluster_id, app_group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    _mock_pve_for_creation(monkeypatch)
    plan = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group_id,
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    ).json()
    _mock_pve_for_execution(monkeypatch)
    client.post(f"/api/v1/lab-runs/{plan['id']}/execute", headers=platform_admin_headers)

    module = _mock_pve_for_cleanup(monkeypatch, {200, 201})
    monkeypatch.setattr(
        module, "get_pool_members", lambda *a, **kw: PoolMembersResult(reachable=False, error="timeout")
    )
    destroyed = []
    monkeypatch.setattr(module, "destroy_vm", lambda *a, vmid, **kw: destroyed.append(vmid))

    response = client.post(f"/api/v1/lab-runs/{plan['id']}/cleanup", headers=platform_admin_headers)
    assert response.status_code == 200
    assert destroyed == []
    assert "Could not verify" in response.json()["error"]


def test_cleanup_preserves_a_failed_outcome_instead_of_overwriting_it_to_completed(
    client, platform_admin_headers, db_session, monkeypatch
):
    """A real bug found live (2026-09-23, UX/CISO audit): a run whose
    member genuinely FAILED its probe (e.g. ICMP blocked by a Windows
    guest's own firewall -- an expected, correct failure) came out of
    `cleanup` relabeled `completed`, rendering identically to a run that
    actually passed. `cleanup` must preserve the real PASSED/PARTIAL/
    FAILED verdict `aggregate_run_status` already computed, not discard
    it.
    """
    from resurgam_api.api.v1 import restore_test_plans as module

    _, cluster_id, app_group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    _mock_pve_for_creation(monkeypatch)
    plan = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group_id,
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    ).json()
    _mock_pve_for_execution(monkeypatch)
    executed = client.post(f"/api/v1/lab-runs/{plan['id']}/execute", headers=platform_admin_headers).json()
    member_id = executed["plan"]["members"][0]["id"]
    real_token = executed["appliance_run_token"]

    failed = client.post(
        f"/api/v1/lab-runs/{plan['id']}/probe-results",
        json={
            "member_id": member_id,
            "results": [{"check_type": "icmp", "status": "fail", "checked_at": "2026-09-23T00:00:00Z"}],
        },
        headers={"Authorization": f"Bearer {real_token}"},
    ).json()
    assert failed["status"] == "failed"

    monkeypatch.setattr(module, "stop_task", lambda *a, **kw: None)
    monkeypatch.setattr(module, "delete_sdn_vnet", lambda *a, **kw: None)

    response = client.post(f"/api/v1/lab-runs/{plan['id']}/cleanup", headers=platform_admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["members"][0]["member_status"] == "failed"


def test_lab_agent_job_lists_queued_member_with_pbs_coordinates(
    client, platform_admin_headers, db_session, monkeypatch
):
    _, cluster_id, app_group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    _mock_pve_for_creation(monkeypatch)
    plan = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group_id,
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    ).json()
    _mock_pve_for_execution(monkeypatch)
    executed = client.post(f"/api/v1/lab-runs/{plan['id']}/execute", headers=platform_admin_headers).json()
    member_id = executed["plan"]["members"][0]["id"]

    agent_headers = _agent_headers(client, platform_admin_headers, cluster_id)
    response = client.get("/api/v1/lab-agent/jobs", headers=agent_headers)
    assert response.status_code == 200
    jobs = response.json()
    assert len(jobs) == 1
    assert jobs[0]["member_id"] == member_id
    assert jobs[0]["pbs_datastore"] == "store1"
    assert jobs[0]["backup_id"] == "110"
    assert jobs[0]["vnet_name"] == plan["vnet_name"]
    assert jobs[0]["pool"] == "resurgam-lab"


def test_lab_agent_jobs_require_a_valid_agent_token(client, db_session):
    response = client.get("/api/v1/lab-agent/jobs", headers={"Authorization": "Bearer nonsense"})
    assert response.status_code == 401


def test_lab_agent_token_only_sees_its_own_clusters_jobs(client, platform_admin_headers, db_session, monkeypatch):
    """One cluster's agent token must not see or claim another
    cluster's jobs -- the same isolation discipline as the per-run
    probe-results token."""
    _, cluster_a_id, app_group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    cluster_b = client.post(
        "/api/v1/pve-clusters",
        json={
            "display_name": "Other Lab PVE",
            "host": "pve3.internal.example",
            "port": 8006,
            "tls_trust_mode": "ca",
            "api_token": "root@pam!token2=secretvalue",
            "is_lab_compute": True,
            "lab_appliance_template_vmid": 9001,
        },
        headers=platform_admin_headers,
    ).json()

    _mock_pve_for_creation(monkeypatch)
    plan = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group_id,
            "pve_cluster_id": cluster_a_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    ).json()
    _mock_pve_for_execution(monkeypatch)
    executed = client.post(f"/api/v1/lab-runs/{plan['id']}/execute", headers=platform_admin_headers).json()
    member_id = executed["plan"]["members"][0]["id"]

    other_agent_headers = _agent_headers(client, platform_admin_headers, cluster_b["id"])
    jobs = client.get("/api/v1/lab-agent/jobs", headers=other_agent_headers).json()
    assert jobs == []

    claim_attempt = client.post(f"/api/v1/lab-agent/jobs/{member_id}/claim", headers=other_agent_headers)
    assert claim_attempt.status_code == 404


def test_lab_agent_claim_then_status_advances_member_to_probing(
    client, platform_admin_headers, db_session, monkeypatch
):
    _, cluster_id, app_group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    _mock_pve_for_creation(monkeypatch)
    plan = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group_id,
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    ).json()
    _mock_pve_for_execution(monkeypatch)
    executed = client.post(f"/api/v1/lab-runs/{plan['id']}/execute", headers=platform_admin_headers).json()
    member_id = executed["plan"]["members"][0]["id"]

    agent_headers = _agent_headers(client, platform_admin_headers, cluster_id)
    claimed = client.post(f"/api/v1/lab-agent/jobs/{member_id}/claim", headers=agent_headers)
    assert claimed.status_code == 204

    double_claim = client.post(f"/api/v1/lab-agent/jobs/{member_id}/claim", headers=agent_headers)
    assert double_claim.status_code == 409

    status = client.post(f"/api/v1/lab-agent/jobs/{member_id}/status", json={"status": "booted"}, headers=agent_headers)
    assert status.status_code == 204

    plan_after = client.get(f"/api/v1/lab-runs/{plan['id']}", headers=platform_admin_headers).json()
    assert plan_after["members"][0]["member_status"] == "probing"


def _booted_guest_agent_member(client, platform_admin_headers, db_session, monkeypatch, *, reachable):
    """An executed one-member run whose member only asks for Level 1
    (`guest_agent`) and has just reported `booted`."""
    from resurgam_api.api.v1 import restore_test_plans as module
    from resurgam_api.models.application_group import ApplicationGroupMember

    _, cluster_id, app_group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    for member in db_session.query(ApplicationGroupMember).filter_by(application_group_id=app_group_id):
        member.check_types = ["guest_agent"]
    db_session.commit()
    _mock_pve_for_creation(monkeypatch)
    plan = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group_id,
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    ).json()
    _mock_pve_for_execution(monkeypatch)
    executed = client.post(f"/api/v1/lab-runs/{plan['id']}/execute", headers=platform_admin_headers).json()
    member_id = executed["plan"]["members"][0]["id"]
    agent_headers = _agent_headers(client, platform_admin_headers, cluster_id)
    _provision_appliance(client, agent_headers, plan["id"], monkeypatch)
    client.post(f"/api/v1/lab-agent/jobs/{member_id}/claim", headers=agent_headers)
    client.post(f"/api/v1/lab-agent/jobs/{member_id}/status", json={"status": "booted"}, headers=agent_headers)
    monkeypatch.setattr(module, "agent_ping", lambda *a, **kw: reachable)
    monkeypatch.setattr(module, "agent_network_get_interfaces", lambda *a, **kw: [])
    return plan["id"], member_id


def test_level1_waits_for_a_booting_guest_instead_of_failing(client, platform_admin_headers, db_session, monkeypatch):
    """Live finding 2026-11-21: a single guest-agent ping about 15 s after
    the start failed Level 1 for every guest booting from its backup."""
    plan_id, _member_id = _booted_guest_agent_member(
        client, platform_admin_headers, db_session, monkeypatch, reachable=False
    )

    polled = client.post(f"/api/v1/lab-runs/{plan_id}/poll", headers=platform_admin_headers).json()

    assert polled["members"][0]["member_status"] == "probing"
    assert polled["members"][0]["probe_results"] == []


def test_level1_fails_once_the_grace_period_is_over(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import restore_test_plans as module
    from resurgam_api.models.restore_test import RestoreTestPlanMember

    plan_id, member_id = _booted_guest_agent_member(
        client, platform_admin_headers, db_session, monkeypatch, reachable=False
    )
    member = db_session.get(RestoreTestPlanMember, member_id)
    member.started_at = datetime.now(UTC) - module.GUEST_AGENT_GRACE - timedelta(seconds=1)
    db_session.commit()

    polled = client.post(f"/api/v1/lab-runs/{plan_id}/poll", headers=platform_admin_headers).json()

    [result] = polled["members"][0]["probe_results"]
    assert result["check_type"] == "guest_agent" and result["status"] == "fail"
    assert "15 minutes" in result["detail"]
    assert polled["members"][0]["member_status"] == "failed"


def test_level1_passes_as_soon_as_the_guest_agent_answers(client, platform_admin_headers, db_session, monkeypatch):
    plan_id, _member_id = _booted_guest_agent_member(
        client, platform_admin_headers, db_session, monkeypatch, reachable=True
    )

    polled = client.post(f"/api/v1/lab-runs/{plan_id}/poll", headers=platform_admin_headers).json()

    [result] = polled["members"][0]["probe_results"]
    assert result["status"] == "pass"
    assert polled["members"][0]["member_status"] == "passed"


def test_lab_agent_status_failure_marks_member_failed(client, platform_admin_headers, db_session, monkeypatch):
    _, cluster_id, app_group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    _mock_pve_for_creation(monkeypatch)
    plan = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group_id,
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    ).json()
    _mock_pve_for_execution(monkeypatch)
    executed = client.post(f"/api/v1/lab-runs/{plan['id']}/execute", headers=platform_admin_headers).json()
    member_id = executed["plan"]["members"][0]["id"]

    agent_headers = _agent_headers(client, platform_admin_headers, cluster_id)
    client.post(f"/api/v1/lab-agent/jobs/{member_id}/claim", headers=agent_headers)
    status = client.post(
        f"/api/v1/lab-agent/jobs/{member_id}/status",
        json={"status": "failed", "error": "map failed"},
        headers=agent_headers,
    )
    assert status.status_code == 204

    plan_after = client.get(f"/api/v1/lab-runs/{plan['id']}", headers=platform_admin_headers).json()
    assert plan_after["members"][0]["member_status"] == "failed"
    assert plan_after["status"] == "failed"


def test_appliance_provisioning_failure_is_not_clobbered_by_later_member_status_reports(
    client,
    platform_admin_headers,
    db_session,
    monkeypatch,
):
    """Real bug found live (2026-09-22): a plan FAILED at the appliance
    clone/config stage (infrastructure-level, independent of members)
    was silently overwritten back to a member-derived status
    (e.g. "running") the moment the next member callback arrived, since
    both `report_lab_agent_job_status` and `poll` unconditionally
    recomputed `plan.status` from `aggregate_run_status`. Once FAILED at
    the appliance level, the plan must stay FAILED.
    """
    from resurgam_api.api.v1 import lab_agent as lab_agent_module
    from resurgam_api.connectivity.pve_api import PveWriteError

    _, cluster_id, app_group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    _mock_pve_for_creation(monkeypatch)
    plan = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": app_group_id,
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=platform_admin_headers,
    ).json()
    _mock_pve_for_execution(monkeypatch)
    executed = client.post(f"/api/v1/lab-runs/{plan['id']}/execute", headers=platform_admin_headers).json()
    member_id = executed["plan"]["members"][0]["id"]

    agent_headers = _agent_headers(client, platform_admin_headers, cluster_id)

    def _raise(*a, **kw):
        raise PveWriteError("clone failed")

    monkeypatch.setattr(lab_agent_module, "clone_vm", _raise)
    jobs = client.get("/api/v1/lab-agent/appliance-jobs", headers=agent_headers).json()
    assert len(jobs) == 1
    write_response = client.post(f"/api/v1/lab-agent/appliance-jobs/{plan['id']}/write-snippet", headers=agent_headers)
    assert write_response.status_code == 204

    plan_after_clone_failure = client.get(f"/api/v1/lab-runs/{plan['id']}", headers=platform_admin_headers).json()
    assert plan_after_clone_failure["status"] == "failed"

    client.post(f"/api/v1/lab-agent/jobs/{member_id}/claim", headers=agent_headers)
    client.post(f"/api/v1/lab-agent/jobs/{member_id}/status", json={"status": "booted"}, headers=agent_headers)

    plan_after_member_report = client.get(f"/api/v1/lab-runs/{plan['id']}", headers=platform_admin_headers).json()
    assert plan_after_member_report["status"] == "failed"

    poll_response = client.post(f"/api/v1/lab-runs/{plan['id']}/poll", headers=platform_admin_headers)
    assert poll_response.json()["status"] == "failed"
