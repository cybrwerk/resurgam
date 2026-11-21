"""Probe appliance uplink: VLAN tag + IPv4 mode (DHCP / static range) --
validation, per-run address allocation (incl. real concurrency), the NIC
config applied to the cloned appliance, release on cleanup, readiness, and
migration 0055 up/down. PVE is mocked."""

import re
import threading
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from resurgam_api.errors import ResurgamApiError
from resurgam_api.lab_setup.uplink import (
    PURPOSE_LAB_RUN,
    PURPOSE_TEMPLATE_BUILD,
    UplinkConfig,
    allocate_uplink_ip,
    overlapping_lab_subnets,
    release_uplink_ip,
    uplink_ipconfig,
    uplink_nameserver,
    uplink_nic,
    validate_uplink_config,
)
from resurgam_api.models.pve import LabUplinkIpAllocation, PveCluster
from resurgam_api.models.secrets import SecretReference
from tests.conftest import TEST_DATABASE_URL
from tests.proxmox_fakes import FakeProxmox, FakeResponse, all_privileges
from tests.test_restore_test_plans_api import (
    _agent_headers,
    _mock_pve_for_cleanup,
    _mock_pve_for_creation,
    _mock_pve_for_execution,
    _setup_run_prereqs,
)

STATIC = {
    "lab_appliance_uplink_ip_mode": "static_range",
    "lab_appliance_uplink_ip_range_start": "192.168.40.50",
    "lab_appliance_uplink_ip_range_end": "192.168.40.51",
    "lab_appliance_uplink_ip_prefix_length": 24,
    "lab_appliance_uplink_gateway": "192.168.40.1",
    "lab_appliance_uplink_dns_servers": ["192.168.40.2"],
}


def _cfg(**overrides) -> UplinkConfig:
    values = {
        "bridge": "vmbr0",
        "ip_mode": "static_range",
        "range_start": "192.168.40.50",
        "range_end": "192.168.40.60",
        "prefix_length": 24,
        "gateway": "192.168.40.1",
        "dns_servers": ["192.168.40.2"],
    }
    values.update(overrides)
    return UplinkConfig(**values)


# --- validation & config strings ---------------------------------------------


def test_valid_configs_have_no_problems():
    assert validate_uplink_config(UplinkConfig(bridge="vmbr0")) == []
    assert validate_uplink_config(UplinkConfig(bridge="vmbr0", vlan_tag=4094)) == []
    assert validate_uplink_config(_cfg()) == []
    assert validate_uplink_config(_cfg(range_start="192.168.40.60", range_end="192.168.40.60")) == []


@pytest.mark.parametrize(
    ("overrides", "field", "fragment"),
    [
        ({"range_end": "192.168.41.10"}, "lab_appliance_uplink_ip_range_end", "same /24 subnet"),
        ({"range_start": "192.168.40.70"}, "lab_appliance_uplink_ip_range_end", "before its start"),
        ({"gateway": "10.0.0.1"}, "lab_appliance_uplink_gateway", "not inside"),
        ({"gateway": "192.168.40.55"}, "lab_appliance_uplink_gateway", "inside the range"),
        ({"gateway": "192.168.40.255"}, "lab_appliance_uplink_gateway", "broadcast"),
        ({"range_start": "192.168.40.0"}, "lab_appliance_uplink_ip_range_start", "network or broadcast"),
        ({"range_start": "300.1.1.1"}, "lab_appliance_uplink_ip_range_start", "not an IPv4 address"),
        ({"range_start": None}, "lab_appliance_uplink_ip_range_start", "required"),
        ({"gateway": None}, "lab_appliance_uplink_gateway", "required"),
        ({"prefix_length": None}, "lab_appliance_uplink_ip_prefix_length", "required"),
        ({"prefix_length": 31}, "lab_appliance_uplink_ip_prefix_length", "between 8 and 30"),
        ({"dns_servers": []}, "lab_appliance_uplink_dns_servers", "at least one DNS"),
        ({"dns_servers": ["dns.example"]}, "lab_appliance_uplink_dns_servers", "not an IPv4"),
        ({"dns_servers": ["1.1.1.1"] * 4}, "lab_appliance_uplink_dns_servers", "At most 3"),
        ({"vlan_tag": 0}, "lab_appliance_uplink_vlan_tag", "1 and 4094"),
        ({"vlan_tag": 4095}, "lab_appliance_uplink_vlan_tag", "1 and 4094"),
    ],
)
def test_invalid_configs_name_the_field_and_the_fix(overrides, field, fragment):
    problems = validate_uplink_config(_cfg(**overrides))
    assert any(p.field == field and fragment in p.message for p in problems), problems


def test_nic_and_cloud_init_strings():
    # PVE apidoc `net[n]`: `... [,tag=<integer>]`; `ipconfig[n]`: `ip=dhcp` or `ip=<cidr>,gw=<ip>`.
    assert uplink_nic("vmbr0", None) == "model=virtio,bridge=vmbr0"
    assert uplink_nic("vmbr0", 42) == "model=virtio,bridge=vmbr0,tag=42"
    assert uplink_ipconfig(UplinkConfig(bridge="vmbr0"), None) == "ip=dhcp"
    assert uplink_ipconfig(_cfg(), "192.168.40.50") == "ip=192.168.40.50/24,gw=192.168.40.1"
    assert uplink_nameserver(_cfg(dns_servers=["192.168.40.2", "9.9.9.9"])) == "192.168.40.2 9.9.9.9"
    assert uplink_nameserver(UplinkConfig(bridge="vmbr0")) is None
    with pytest.raises(ValueError):
        uplink_ipconfig(_cfg(), None)


def test_overlap_detection():
    cfg = _cfg()
    assert overlapping_lab_subnets(cfg, [("A", "10.99.0.0/24"), ("B", "192.168.40.0/25")]) == [("B", "192.168.40.0/25")]
    assert overlapping_lab_subnets(UplinkConfig(bridge="vmbr0"), [("B", "192.168.40.0/25")]) == []


# --- API --------------------------------------------------------------------------


def _cluster(client, headers, **body) -> dict:
    response = client.post(
        "/api/v1/pve-clusters",
        json={
            "display_name": "Lab",
            "host": "pve.lab.example",
            "port": 8006,
            "tls_trust_mode": "ca",
            "api_token": "root@pam!token=secretvalue",
            "lab_appliance_uplink_bridge": "vmbr0",
            **body,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_api_round_trip_and_partial_updates(client, platform_admin_headers):
    created = _cluster(client, platform_admin_headers)
    assert created["lab_appliance_uplink_ip_mode"] == "dhcp"
    assert created["lab_appliance_uplink_vlan_tag"] is None
    assert created["lab_appliance_uplink_dns_servers"] == []

    url = f"/api/v1/pve-clusters/{created['id']}"
    updated = client.patch(url, json={"lab_appliance_uplink_vlan_tag": 42, **STATIC}, headers=platform_admin_headers)
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["lab_appliance_uplink_vlan_tag"] == 42
    assert body["lab_appliance_uplink_ip_range_end"] == "192.168.40.51"
    assert body["lab_appliance_uplink_dns_servers"] == ["192.168.40.2"]

    # A partial update is validated against the merged state.
    widened = client.patch(
        url, json={"lab_appliance_uplink_ip_range_end": "192.168.40.99"}, headers=platform_admin_headers
    )
    assert widened.status_code == 200
    bad = client.patch(url, json={"lab_appliance_uplink_gateway": "192.168.40.60"}, headers=platform_admin_headers)
    assert bad.status_code == 422
    assert bad.json()["code"] == "invalid_uplink_network"
    assert bad.json()["details"]["errors"][0]["field"] == "lab_appliance_uplink_gateway"

    cleared = client.patch(
        url,
        json={"lab_appliance_uplink_vlan_tag": None, "lab_appliance_uplink_ip_mode": "dhcp"},
        headers=platform_admin_headers,
    )
    assert cleared.status_code == 200
    assert cleared.json()["lab_appliance_uplink_vlan_tag"] is None
    assert cleared.json()["lab_appliance_uplink_ip_mode"] == "dhcp"


def test_api_rejects_invalid_values(client, platform_admin_headers):
    assert (
        client.post(
            "/api/v1/pve-clusters",
            json={
                "display_name": "Lab",
                "host": "pve.lab.example",
                "port": 8006,
                "tls_trust_mode": "ca",
                "api_token": "root@pam!token=secretvalue",
                "lab_appliance_uplink_vlan_tag": 5000,
            },
            headers=platform_admin_headers,
        ).status_code
        == 422
    )
    incomplete = client.post(
        "/api/v1/pve-clusters",
        json={
            "display_name": "Lab",
            "host": "pve.lab.example",
            "port": 8006,
            "tls_trust_mode": "ca",
            "api_token": "root@pam!token=secretvalue",
            "lab_appliance_uplink_ip_mode": "static_range",
        },
        headers=platform_admin_headers,
    )
    assert incomplete.status_code == 422
    fields = {e["field"] for e in incomplete.json()["details"]["errors"]}
    assert {"lab_appliance_uplink_ip_range_start", "lab_appliance_uplink_gateway"} <= fields


# --- allocation ---------------------------------------------------------------------


def _static_cluster(db_session, client, headers, **overrides) -> PveCluster:
    created = _cluster(client, headers, **{**STATIC, **overrides})
    return db_session.get(PveCluster, created["id"])


def test_allocation_distinct_idempotent_exhausted_and_released(client, platform_admin_headers, db_session):
    cluster = _static_cluster(db_session, client, platform_admin_headers)
    run_a, run_b, run_c = _plan_ids(db_session, cluster, 3)

    first = allocate_uplink_ip(db_session, cluster, purpose=PURPOSE_LAB_RUN, restore_test_plan_id=run_a)
    second = allocate_uplink_ip(db_session, cluster, purpose=PURPOSE_LAB_RUN, restore_test_plan_id=run_b)
    assert {first, second} == {"192.168.40.50", "192.168.40.51"}
    assert allocate_uplink_ip(db_session, cluster, purpose=PURPOSE_LAB_RUN, restore_test_plan_id=run_a) == first

    with pytest.raises(ResurgamApiError) as exhausted:
        allocate_uplink_ip(db_session, cluster, purpose=PURPOSE_LAB_RUN, restore_test_plan_id=run_c)
    assert exhausted.value.code == "lab_uplink_range_exhausted"
    assert exhausted.value.status_code == 409
    assert "Clean up finished lab runs" in exhausted.value.message

    release_uplink_ip(db_session, restore_test_plan_id=run_a)
    assert allocate_uplink_ip(db_session, cluster, purpose=PURPOSE_LAB_RUN, restore_test_plan_id=run_c) == first


def test_dhcp_allocates_nothing(client, platform_admin_headers, db_session):
    cluster = db_session.get(PveCluster, _cluster(client, platform_admin_headers)["id"])
    assert allocate_uplink_ip(db_session, cluster, purpose=PURPOSE_TEMPLATE_BUILD) is None
    assert db_session.query(LabUplinkIpAllocation).count() == 0


def test_stale_template_build_allocations_are_reclaimed(client, platform_admin_headers, db_session):
    cluster = _static_cluster(db_session, client, platform_admin_headers)
    allocate_uplink_ip(db_session, cluster, purpose=PURPOSE_TEMPLATE_BUILD)
    allocate_uplink_ip(db_session, cluster, purpose=PURPOSE_TEMPLATE_BUILD)
    db_session.execute(text("UPDATE lab_uplink_ip_allocations SET created_at = now() - interval '3 hours'"))
    assert allocate_uplink_ip(db_session, cluster, purpose=PURPOSE_TEMPLATE_BUILD) == "192.168.40.50"


def test_unique_constraint_backs_up_the_lock(client, platform_admin_headers, db_session):
    from sqlalchemy.exc import IntegrityError

    cluster = _static_cluster(db_session, client, platform_admin_headers)
    db_session.add(LabUplinkIpAllocation(pve_cluster_id=cluster.id, ip_address="192.168.40.50", purpose="lab_run"))
    db_session.flush()
    db_session.add(LabUplinkIpAllocation(pve_cluster_id=cluster.id, ip_address="192.168.40.50", purpose="lab_run"))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def _plan_ids(db_session, cluster, count) -> list[uuid.UUID]:
    """Bare RestoreTestPlan rows (the allocation FK needs real plans)."""
    from resurgam_api.models.application_group import ApplicationGroup
    from resurgam_api.models.restore_test import RestoreTestPlan
    from resurgam_api.models.tenancy import Customer

    customer = Customer(
        name=f"C{uuid.uuid4().hex[:6]}",
        slug=f"c{uuid.uuid4().hex[:8]}",
        debitor_number=f"d-{uuid.uuid4().hex[:8]}",
        match_code="CXX",
    )
    db_session.add(customer)
    db_session.flush()
    group = ApplicationGroup(customer_id=customer.id, name="G")
    db_session.add(group)
    db_session.flush()
    ids = []
    for _ in range(count):
        plan = RestoreTestPlan(
            application_group_id=group.id,
            customer_id=customer.id,
            pve_cluster_id=cluster.id,
            vnet_name="labx",
            status="planned",
        )
        db_session.add(plan)
        db_session.flush()
        ids.append(plan.id)
    db_session.commit()
    return ids


def test_concurrent_allocations_never_collide(engine):
    """Two sessions on two connections allocate at the same time; the row
    lock serializes them and each run gets its own address."""
    factory = sessionmaker(bind=engine)
    setup = factory()
    secret = SecretReference(reference_key=f"test-{uuid.uuid4()}", description="t", purpose="infra_credential")
    setup.add(secret)
    setup.flush()
    cluster = PveCluster(
        display_name="concurrency",
        host="pve.concurrency.example",
        port=8006,
        tls_trust_mode="ca",
        api_token_secret_ref_id=secret.id,
        lab_appliance_uplink_bridge="vmbr0",
        **STATIC,
    )
    setup.add(cluster)
    setup.commit()
    plan_ids = _plan_ids(setup, cluster, 2)
    cluster_id, secret_id = cluster.id, secret.id
    setup.close()

    barrier = threading.Barrier(2)
    results: dict[int, str] = {}
    errors: list[Exception] = []

    def allocate(index: int) -> None:
        session = factory()
        try:
            target = session.get(PveCluster, cluster_id)
            barrier.wait()
            results[index] = allocate_uplink_ip(
                session, target, purpose=PURPOSE_LAB_RUN, restore_test_plan_id=plan_ids[index]
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001 -- surfaced by the assertion below
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=allocate, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    try:
        assert errors == []
        assert sorted(results.values()) == ["192.168.40.50", "192.168.40.51"]
    finally:
        cleanup = factory()
        from resurgam_api.models.application_group import ApplicationGroup
        from resurgam_api.models.restore_test import RestoreTestPlan

        plans = cleanup.query(RestoreTestPlan).filter(RestoreTestPlan.id.in_(plan_ids)).all()
        group_ids = {p.application_group_id for p in plans}
        customer_ids = {p.customer_id for p in plans}
        for plan in plans:
            cleanup.delete(plan)
        cleanup.flush()
        cleanup.query(ApplicationGroup).filter(ApplicationGroup.id.in_(group_ids)).delete(synchronize_session=False)
        cleanup.execute(text("DELETE FROM customers WHERE id = ANY(:ids)"), {"ids": list(customer_ids)})
        cleanup.query(PveCluster).filter_by(id=cluster_id).delete()
        cleanup.query(SecretReference).filter_by(id=secret_id).delete()
        cleanup.commit()
        cleanup.close()


# --- lab runs -------------------------------------------------------------------


def _make_static(db_session, cluster_id, **overrides) -> None:
    cluster = db_session.get(PveCluster, cluster_id)
    values = {**STATIC, "lab_appliance_uplink_vlan_tag": 42, **overrides}
    for key, value in values.items():
        setattr(cluster, key, value)
    db_session.commit()


def _create_run(client, headers, cluster_id, group_id):
    plan = client.post(
        "/api/v1/lab-runs",
        json={
            "application_group_id": group_id,
            "pve_cluster_id": cluster_id,
            "target_node": "pve1",
            "target_storage": "local-lvm",
        },
        headers=headers,
    )
    assert plan.status_code == 200, plan.text
    return plan.json()


def test_runs_get_distinct_addresses_applied_to_the_appliance_and_freed_on_cleanup(
    client, platform_admin_headers, db_session, monkeypatch
):
    from resurgam_api.api.v1 import lab_agent as lab_agent_module
    from resurgam_api.connectivity.models import PveTaskStatus

    _, cluster_id, group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    _make_static(db_session, cluster_id)
    _mock_pve_for_creation(monkeypatch, vmid=300)
    run_a = _create_run(client, platform_admin_headers, cluster_id, group_id)
    _mock_pve_for_creation(monkeypatch, vmid=310)
    run_b = _create_run(client, platform_admin_headers, cluster_id, group_id)
    _mock_pve_for_creation(monkeypatch, vmid=320)
    run_c = _create_run(client, platform_admin_headers, cluster_id, group_id)

    _mock_pve_for_execution(monkeypatch)
    for run in (run_a, run_b):
        assert client.post(f"/api/v1/lab-runs/{run['id']}/execute", headers=platform_admin_headers).status_code == 200
    addresses = {a.restore_test_plan_id: a.ip_address for a in db_session.query(LabUplinkIpAllocation).all()}
    assert sorted(addresses.values()) == ["192.168.40.50", "192.168.40.51"]

    # Range of two is exhausted: the third run is refused before PVE is touched.
    vnet_calls = []
    from resurgam_api.api.v1 import restore_test_plans as module

    monkeypatch.setattr(module, "create_sdn_vnet", lambda *a, **kw: vnet_calls.append(kw))
    exhausted = client.post(f"/api/v1/lab-runs/{run_c['id']}/execute", headers=platform_admin_headers)
    assert exhausted.status_code == 409
    assert exhausted.json()["code"] == "lab_uplink_range_exhausted"
    assert vnet_calls == []

    # The appliance clone gets net1 with the VLAN tag and run A's own address.
    applied = {}
    monkeypatch.setattr(lab_agent_module, "clone_vm", lambda *a, **kw: "UPID:pve:1:qmclone:900:root@pam:")
    monkeypatch.setattr(
        lab_agent_module,
        "get_task_status",
        lambda *a, **kw: PveTaskStatus(reachable=True, status="stopped", exitstatus="OK"),
    )
    monkeypatch.setattr(lab_agent_module, "configure_lab_appliance_network", lambda *a, **kw: applied.update(kw))
    monkeypatch.setattr(lab_agent_module, "start_guest", lambda *a, **kw: "UPID:pve:2:qmstart:900:root@pam:")
    agent = _agent_headers(client, platform_admin_headers, cluster_id)
    written = client.post(f"/api/v1/lab-agent/appliance-jobs/{run_a['id']}/write-snippet", headers=agent)
    assert written.status_code == 204
    address_a = addresses[uuid.UUID(run_a["id"])]
    assert applied["net1"] == "model=virtio,bridge=vmbr0,tag=42"
    assert applied["ipconfig1"] == f"ip={address_a}/24,gw=192.168.40.1"
    assert applied["nameserver"] == "192.168.40.2"
    assert applied["net0"].startswith("model=virtio,bridge=lab")

    # Cleanup frees run A's address; run C can now execute and gets it.
    _mock_pve_for_cleanup(monkeypatch, {300, 301})
    assert client.post(f"/api/v1/lab-runs/{run_a['id']}/cleanup", headers=platform_admin_headers).status_code == 200
    assert db_session.query(LabUplinkIpAllocation).filter_by(restore_test_plan_id=uuid.UUID(run_a["id"])).count() == 0
    _mock_pve_for_execution(monkeypatch)
    assert client.post(f"/api/v1/lab-runs/{run_c['id']}/execute", headers=platform_admin_headers).status_code == 200
    assert (
        db_session.query(LabUplinkIpAllocation).filter_by(restore_test_plan_id=uuid.UUID(run_c["id"])).one().ip_address
        == address_a
    )


def test_dhcp_appliance_config_is_unchanged(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import lab_agent as lab_agent_module
    from resurgam_api.connectivity.models import PveTaskStatus

    _, cluster_id, group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    _mock_pve_for_creation(monkeypatch)
    run = _create_run(client, platform_admin_headers, cluster_id, group_id)
    _mock_pve_for_execution(monkeypatch)
    client.post(f"/api/v1/lab-runs/{run['id']}/execute", headers=platform_admin_headers)
    applied = {}
    monkeypatch.setattr(lab_agent_module, "clone_vm", lambda *a, **kw: "UPID")
    monkeypatch.setattr(
        lab_agent_module,
        "get_task_status",
        lambda *a, **kw: PveTaskStatus(reachable=True, status="stopped", exitstatus="OK"),
    )
    monkeypatch.setattr(lab_agent_module, "configure_lab_appliance_network", lambda *a, **kw: applied.update(kw))
    monkeypatch.setattr(lab_agent_module, "start_guest", lambda *a, **kw: "UPID")
    agent = _agent_headers(client, platform_admin_headers, cluster_id)
    client.post(f"/api/v1/lab-agent/appliance-jobs/{run['id']}/write-snippet", headers=agent)
    assert applied["net1"] == "model=virtio,bridge=vmbr0"
    assert applied["ipconfig1"] == "ip=dhcp"
    assert applied["nameserver"] is None
    assert db_session.query(LabUplinkIpAllocation).count() == 0


def test_clone_failure_frees_the_address(client, platform_admin_headers, db_session, monkeypatch):
    from resurgam_api.api.v1 import lab_agent as lab_agent_module
    from resurgam_api.connectivity.pve_api import PveWriteError

    _, cluster_id, group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    _make_static(db_session, cluster_id)
    _mock_pve_for_creation(monkeypatch)
    run = _create_run(client, platform_admin_headers, cluster_id, group_id)
    _mock_pve_for_execution(monkeypatch)
    client.post(f"/api/v1/lab-runs/{run['id']}/execute", headers=platform_admin_headers)
    assert db_session.query(LabUplinkIpAllocation).count() == 1

    def boom(*a, **kw):
        raise PveWriteError("HTTP 500: storage full")

    monkeypatch.setattr(lab_agent_module, "clone_vm", boom)
    agent = _agent_headers(client, platform_admin_headers, cluster_id)
    client.post(f"/api/v1/lab-agent/appliance-jobs/{run['id']}/write-snippet", headers=agent)
    db_session.expire_all()
    assert db_session.query(LabUplinkIpAllocation).count() == 0


def test_lab_subnet_overlapping_the_uplink_is_refused(client, platform_admin_headers, db_session, monkeypatch):
    _, cluster_id, group_id = _setup_run_prereqs(client, platform_admin_headers, db_session, monkeypatch)
    # The Application Group's lab subnet is 10.99.0.0/24.
    _make_static(
        db_session,
        cluster_id,
        lab_appliance_uplink_ip_range_start="10.99.0.100",
        lab_appliance_uplink_ip_range_end="10.99.0.110",
        lab_appliance_uplink_gateway="10.99.0.254",
        lab_appliance_uplink_dns_servers=["10.99.0.253"],
    )
    _mock_pve_for_creation(monkeypatch)
    run = _create_run(client, platform_admin_headers, cluster_id, group_id)
    _mock_pve_for_execution(monkeypatch)
    response = client.post(f"/api/v1/lab-runs/{run['id']}/execute", headers=platform_admin_headers)
    assert response.status_code == 400
    assert response.json()["code"] == "lab_subnet_overlaps_uplink"


# --- readiness -------------------------------------------------------------------


def _readiness_routes(bridge_entry: dict) -> dict:
    return {
        ("GET", "/api2/json/access/permissions"): FakeResponse({"data": all_privileges("pve")}),
        ("GET", "/api2/json/nodes"): FakeResponse({"data": [{"node": "pve1", "status": "online"}]}),
        ("GET", "/api2/json/cluster/sdn/zones/resurgam"): FakeResponse(
            {"data": {"zone": "resurgam", "type": "simple"}}
        ),
        ("GET", "/api2/json/nodes/pve1/network"): FakeResponse({"data": [bridge_entry]}),
        ("GET", "/api2/json/storage"): FakeResponse({"data": []}),
        ("GET", "/api2/json/cluster/resources"): FakeResponse({"data": []}),
    }


def _uplink_item(client, headers, cluster_id, monkeypatch, bridge_entry) -> dict:
    FakeProxmox(routes=_readiness_routes(bridge_entry)).install(monkeypatch)
    body = client.get(f"/api/v1/pve-clusters/{cluster_id}/lab-readiness?nodes=pve1", headers=headers).json()
    return next(i for i in body["items"] if i["key"] == "uplink_network")


@pytest.mark.parametrize(
    ("bridge", "tag", "status", "code"),
    [
        ({"iface": "vmbr0", "type": "bridge", "active": 1}, None, "ok", "dhcp_ok"),
        ({"iface": "vmbr0", "type": "bridge", "active": 1, "bridge_vlan_aware": 1}, 42, "ok", "dhcp_ok"),
        (
            {"iface": "vmbr0", "type": "bridge", "active": 1, "bridge_vlan_aware": 1, "bridge_vids": "2-4094"},
            42,
            "ok",
            "dhcp_ok",
        ),
        (
            {"iface": "vmbr0", "type": "bridge", "active": 1, "bridge_vlan_aware": 1, "bridge_vids": "10 20-30"},
            42,
            "missing",
            "vlan_not_allowed",
        ),
        ({"iface": "vmbr0", "type": "bridge", "active": 1}, 42, "warning", "bridge_not_vlan_aware"),
        ({"iface": "vmbr0", "type": "OVSBridge", "active": 1}, 42, "ok", "dhcp_ok"),
    ],
)
def test_readiness_vlan_support(client, platform_admin_headers, db_session, monkeypatch, bridge, tag, status, code):
    created = _cluster(client, platform_admin_headers, lab_appliance_uplink_vlan_tag=tag)
    item = _uplink_item(client, platform_admin_headers, created["id"], monkeypatch, bridge)
    assert (item["status"], item["code"]) == (status, code), item


def test_readiness_static_range_states(client, platform_admin_headers, db_session, monkeypatch):
    bridge = {"iface": "vmbr0", "type": "bridge", "active": 1}
    cluster = _static_cluster(db_session, client, platform_admin_headers)
    item = _uplink_item(client, platform_admin_headers, cluster.id, monkeypatch, bridge)
    assert (item["status"], item["code"]) == ("ok", "static_ok")
    assert item["details"]["range_size"] == 2

    for plan_id in _plan_ids(db_session, cluster, 2):
        allocate_uplink_ip(db_session, cluster, purpose=PURPOSE_LAB_RUN, restore_test_plan_id=plan_id)
    db_session.commit()
    item = _uplink_item(client, platform_admin_headers, cluster.id, monkeypatch, bridge)
    assert (item["status"], item["code"]) == ("warning", "range_exhausted")

    cluster.lab_appliance_uplink_gateway = "192.168.40.50"  # inside the range -- e.g. an old/imported config
    db_session.commit()
    item = _uplink_item(client, platform_admin_headers, cluster.id, monkeypatch, bridge)
    assert (item["status"], item["code"]) == ("missing", "invalid_settings")


# --- migration ---------------------------------------------------------------------


def test_migration_0055_up_and_down():
    """Runs the real migrations on a scratch database: head -> 0054 -> head."""
    from alembic.config import Config

    from alembic import command
    from tests.conftest import BACKEND_ROOT, _ensure_database_exists

    url = re.sub(r"/[^/?]+$", "/resurgam_migration_0055_test", TEST_DATABASE_URL)
    _ensure_database_exists(url)
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    try:
        columns = {c["name"] for c in inspect(engine).get_columns("pve_clusters")}
        assert {
            "lab_appliance_uplink_vlan_tag",
            "lab_appliance_uplink_ip_mode",
            "lab_appliance_uplink_dns_servers",
        } <= columns
        assert "lab_uplink_ip_allocations" in inspect(engine).get_table_names()

        command.downgrade(cfg, "0054")
        inspector = inspect(engine)
        assert "lab_appliance_uplink_vlan_tag" not in {c["name"] for c in inspector.get_columns("pve_clusters")}
        assert "lab_uplink_ip_allocations" not in inspector.get_table_names()

        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() >= "0055"
    finally:
        engine.dispose()
