"""Instant Recovery (ADR 0030): plan creation checks, the execute -> agent ->
running -> move/discard lifecycle, the scheduler's move driver and timeouts,
alarms, and the maintenance-mode guard."""

from datetime import UTC, datetime, timedelta

import pytest

from resurgam_api.connectivity.models import GuestListResult, PveTaskStatus, RemoteGuest
from resurgam_api.instant_recovery import lifecycle
from resurgam_api.models.pve import PveCluster
from resurgam_api.models.restore import RestorePlan
from tests.test_restore_plans_api import (
    _create_group,
    _default_plan_body,
    _mock_reachable_pve,
    _setup_vm_snapshot,
)


@pytest.fixture
def original_vm(monkeypatch):
    """What `find_original_vm` sees: set `state["guests"]` / `state["reachable"]`."""
    from resurgam_api.instant_recovery import original_vm as original_vm_module

    state = {"guests": [], "reachable": True}

    def fake_list_guests(*a, **kw):
        if not state["reachable"]:
            return GuestListResult(reachable=False, error="timeout")
        return GuestListResult(reachable=True, guests=state["guests"])

    monkeypatch.setattr(original_vm_module, "list_guests", fake_list_guests)
    return state


def _enable_instant_recovery(client, headers, db_session, cluster_id, **overrides):
    token = client.post(f"/api/v1/pve-clusters/{cluster_id}/lab-agent-token", headers=headers).json()["token"]
    cluster = db_session.get(PveCluster, cluster_id)
    cluster.instant_recovery_enabled = True
    cluster.lab_agent_node = "pve1"
    cluster.lab_agent_last_seen_at = datetime.now(UTC)
    for key, value in overrides.items():
        setattr(cluster, key, value)
    db_session.commit()
    return {"Authorization": f"Bearer {token}"}


def _setup(client, headers, db_session, monkeypatch, **cluster_overrides):
    _customer_id, cluster_id, _group_id, snapshot_id = _setup_vm_snapshot(client, headers, db_session, monkeypatch)
    _mock_reachable_pve(monkeypatch)
    agent = _enable_instant_recovery(client, headers, db_session, cluster_id, **cluster_overrides)
    return cluster_id, snapshot_id, agent


def _create_instant_plan(client, headers, cluster_id, snapshot_id, **overrides):
    body = _default_plan_body(
        backup_snapshot_id=str(snapshot_id), pve_cluster_id=cluster_id, method="instant", **overrides
    )
    return client.post("/api/v1/restore-plans", json=body, headers=headers)


def _running_plan(client, headers, db_session, monkeypatch, original_vm):
    cluster_id, snapshot_id, agent = _setup(client, headers, db_session, monkeypatch)
    plan_id = _create_instant_plan(client, headers, cluster_id, snapshot_id).json()["id"]
    assert (
        client.post(f"/api/v1/restore-plans/{plan_id}/execute", headers=headers).json()["execution_status"] == "queued"
    )
    claim = client.post(f"/api/v1/lab-agent/instant-recovery-jobs/{plan_id}/claim", headers=agent)
    assert claim.status_code == 200
    unit = f"resurgam-ir-{plan_id}-scsi0"
    progress = [
        {"mapped_device": {"slot": "scsi0", "archive": "drive-scsi0.img.fidx", "device": "/dev/loop3", "unit": unit}},
        {"overlay_volume": {"slot": "scsi0", "volume": "resurgam-lab-overlays:200/vm-200-ir-scsi0.qcow2"}},
        {"vm_created": True},
        {"imported_volume": {"slot": "tpmstate0", "volume": "local-lvm:vm-200-disk-0"}},
        {"phase": "starting"},
    ]
    for body in progress:
        response = client.post(f"/api/v1/lab-agent/instant-recovery-jobs/{plan_id}/progress", json=body, headers=agent)
        assert response.status_code == 204, response.text
    response = client.post(
        f"/api/v1/lab-agent/instant-recovery-jobs/{plan_id}/status",
        json={"status": "running", "skipped_disks": ["hostpci0"]},
        headers=agent,
    )
    assert response.status_code == 204, response.text
    return cluster_id, plan_id, agent


# --- plan creation --------------------------------------------------------------


def test_instant_plan_is_created_with_its_own_checks(
    client, platform_admin_headers, db_session, monkeypatch, original_vm
):
    cluster_id, snapshot_id, _agent = _setup(client, platform_admin_headers, db_session, monkeypatch)

    response = _create_instant_plan(client, platform_admin_headers, cluster_id, snapshot_id)

    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["method"] == "instant"
    checks = {c["check"]: c["status"] for c in plan["checks_result"]}
    assert checks["instant_recovery_enabled"] == "pass"
    assert checks["lab_agent"] == "pass"
    assert checks["agent_node"] == "pass"
    assert checks["original_vm"] == "pass"
    assert "pbs_storage_visibility" not in checks  # the agent reads PBS with its own token
    assert "Instant Recovery" in plan["generated_command"]
    assert plan["instant_recovery"]["original_vm"]["status"] == "absent"
    assert plan["instant_recovery"]["keep_mac"] is True


def test_instant_plan_refuses_a_container(client, platform_admin_headers, db_session, monkeypatch, original_vm):
    from resurgam_api.models.inventory import BackupGroup, BackupSnapshot

    cluster_id, snapshot_id, _agent = _setup(client, platform_admin_headers, db_session, monkeypatch)
    vm_group = db_session.get(BackupGroup, db_session.get(BackupSnapshot, snapshot_id).backup_group_id)
    ct_group_id = _create_group(db_session, vm_group.datastore_id, backup_type="ct", backup_id="300")
    ct_snapshot = BackupSnapshot(backup_group_id=ct_group_id, snapshot_time=datetime(2026, 9, 14, tzinfo=UTC))
    db_session.add(ct_snapshot)
    db_session.commit()

    response = _create_instant_plan(client, platform_admin_headers, cluster_id, ct_snapshot.id)

    assert response.status_code == 400
    assert response.json()["code"] == "instant_recovery_vm_only"


def test_instant_plan_needs_a_free_vmid(client, platform_admin_headers, db_session, monkeypatch, original_vm):
    cluster_id, snapshot_id, _agent = _setup(client, platform_admin_headers, db_session, monkeypatch)

    response = _create_instant_plan(client, platform_admin_headers, cluster_id, snapshot_id, vmid_intent="existing")

    assert response.status_code == 400
    assert response.json()["code"] == "instant_recovery_needs_free_vmid"


def test_instant_plan_fails_when_the_cluster_does_not_allow_it(
    client, platform_admin_headers, db_session, monkeypatch, original_vm
):
    cluster_id, snapshot_id, _agent = _setup(
        client, platform_admin_headers, db_session, monkeypatch, instant_recovery_enabled=False
    )

    response = _create_instant_plan(client, platform_admin_headers, cluster_id, snapshot_id)

    assert response.status_code == 400
    checks = {c["check"]: c["status"] for c in response.json()["details"]["checks"]}
    assert checks["instant_recovery_enabled"] == "fail"


def test_instant_plan_fails_on_another_node_than_the_agents(
    client, platform_admin_headers, db_session, monkeypatch, original_vm
):
    cluster_id, snapshot_id, _agent = _setup(
        client, platform_admin_headers, db_session, monkeypatch, lab_agent_node="pve2"
    )

    response = _create_instant_plan(client, platform_admin_headers, cluster_id, snapshot_id)

    checks = {c["check"]: c["status"] for c in response.json()["details"]["checks"]}
    assert checks["agent_node"] == "fail"


def test_instant_plan_fails_when_the_agent_is_silent(
    client, platform_admin_headers, db_session, monkeypatch, original_vm
):
    cluster_id, snapshot_id, _agent = _setup(
        client,
        platform_admin_headers,
        db_session,
        monkeypatch,
        lab_agent_last_seen_at=datetime.now(UTC) - timedelta(hours=1),
    )

    response = _create_instant_plan(client, platform_admin_headers, cluster_id, snapshot_id)

    checks = {c["check"]: c["status"] for c in response.json()["details"]["checks"]}
    assert checks["lab_agent"] == "fail"


def test_original_network_is_refused_while_the_original_runs(
    client, platform_admin_headers, db_session, monkeypatch, original_vm
):
    cluster_id, snapshot_id, _agent = _setup(client, platform_admin_headers, db_session, monkeypatch)
    original_vm["guests"] = [RemoteGuest(vmid=116, node="pve1", guest_type="qemu", name="srv", status="running")]

    response = _create_instant_plan(
        client,
        platform_admin_headers,
        cluster_id,
        snapshot_id,
        network_mode="original",
        network_danger_acknowledged=True,
    )

    assert response.status_code == 400
    checks = {c["check"]: c for c in response.json()["details"]["checks"]}
    assert checks["original_vm"]["status"] == "fail"
    assert "running" in checks["original_vm"]["message"]


def test_a_running_original_means_new_mac_addresses(
    client, platform_admin_headers, db_session, monkeypatch, original_vm
):
    cluster_id, snapshot_id, _agent = _setup(client, platform_admin_headers, db_session, monkeypatch)
    original_vm["guests"] = [RemoteGuest(vmid=116, node="pve1", guest_type="qemu", name="srv", status="running")]

    response = _create_instant_plan(
        client,
        platform_admin_headers,
        cluster_id,
        snapshot_id,
        network_mode="isolated",
        network_bridge="vmbr9",
        network_danger_acknowledged=True,
    )

    assert response.status_code == 200, response.text
    assert response.json()["instant_recovery"]["keep_mac"] is False
    assert response.json()["instant_recovery"]["original_vm"]["status"] == "running"


def test_unknown_original_state_with_original_network_needs_acknowledgment(
    client, platform_admin_headers, db_session, monkeypatch, original_vm
):
    cluster_id, snapshot_id, _agent = _setup(client, platform_admin_headers, db_session, monkeypatch)
    original_vm["reachable"] = False

    refused = _create_instant_plan(client, platform_admin_headers, cluster_id, snapshot_id, network_mode="original")
    accepted = _create_instant_plan(
        client,
        platform_admin_headers,
        cluster_id,
        snapshot_id,
        network_mode="original",
        network_danger_acknowledged=True,
    )

    assert refused.status_code == 400
    assert refused.json()["code"] == "network_acknowledgment_required"
    assert accepted.status_code == 200
    assert accepted.json()["instant_recovery"]["keep_mac"] is False


# --- execute / agent -------------------------------------------------------------


def test_execute_queues_for_the_agent_and_rechecks_the_original(
    client, platform_admin_headers, db_session, monkeypatch, original_vm
):
    cluster_id, snapshot_id, agent = _setup(client, platform_admin_headers, db_session, monkeypatch)
    plan_id = _create_instant_plan(
        client,
        platform_admin_headers,
        cluster_id,
        snapshot_id,
        network_mode="original",
        network_danger_acknowledged=True,
    ).json()["id"]
    # The original was started after the plan was created.
    original_vm["guests"] = [RemoteGuest(vmid=116, node="pve1", guest_type="qemu", name="srv", status="running")]

    refused = client.post(f"/api/v1/restore-plans/{plan_id}/execute", headers=platform_admin_headers)

    assert refused.status_code == 409
    assert refused.json()["code"] == "original_vm_running"
    assert client.get("/api/v1/lab-agent/instant-recovery-jobs", headers=agent).json() == []

    original_vm["guests"] = [RemoteGuest(vmid=116, node="pve1", guest_type="qemu", name="srv", status="stopped")]
    queued = client.post(f"/api/v1/restore-plans/{plan_id}/execute", headers=platform_admin_headers)
    assert queued.json()["execution_status"] == "queued"
    jobs = client.get("/api/v1/lab-agent/instant-recovery-jobs", headers=agent).json()
    assert [(j["id"], j["kind"]) for j in jobs] == [(plan_id, "start")]


def test_claim_hands_over_the_start_description(client, platform_admin_headers, db_session, monkeypatch, original_vm):
    cluster_id, snapshot_id, agent = _setup(client, platform_admin_headers, db_session, monkeypatch)
    plan_id = _create_instant_plan(client, platform_admin_headers, cluster_id, snapshot_id).json()["id"]
    client.post(f"/api/v1/restore-plans/{plan_id}/execute", headers=platform_admin_headers)

    claim = client.post(f"/api/v1/lab-agent/instant-recovery-jobs/{plan_id}/claim", headers=agent)
    again = client.post(f"/api/v1/lab-agent/instant-recovery-jobs/{plan_id}/claim", headers=agent)

    assert claim.status_code == 200
    body = claim.json()
    assert body["target_vmid"] == 200
    assert body["snapshot_path"] == "vm/116/2026-09-14T00:48:57Z"
    assert body["pbs_datastore"] == "store1"
    assert body["keep_mac"] is True
    assert body["min_free_bytes"] == lifecycle.MIN_FREE_BYTES
    assert again.status_code == 409
    plan = client.get(f"/api/v1/restore-plans/{plan_id}", headers=platform_admin_headers).json()
    assert plan["execution_status"] == "mapping"


def test_another_clusters_agent_gets_404(client, platform_admin_headers, db_session, monkeypatch, original_vm):
    cluster_id, snapshot_id, _agent = _setup(client, platform_admin_headers, db_session, monkeypatch)
    plan_id = _create_instant_plan(client, platform_admin_headers, cluster_id, snapshot_id).json()["id"]
    client.post(f"/api/v1/restore-plans/{plan_id}/execute", headers=platform_admin_headers)
    other_id = client.post(
        "/api/v1/pve-clusters",
        json={
            "display_name": "Other",
            "host": "pve9.internal.example",
            "port": 8006,
            "tls_trust_mode": "ca",
            "api_token": "root@pam!token=secretvalue",
        },
        headers=platform_admin_headers,
    ).json()["id"]
    other_agent = _enable_instant_recovery(client, platform_admin_headers, db_session, other_id)

    assert client.get("/api/v1/lab-agent/instant-recovery-jobs", headers=other_agent).json() == []
    response = client.post(f"/api/v1/lab-agent/instant-recovery-jobs/{plan_id}/claim", headers=other_agent)
    assert response.status_code == 404


def test_the_agent_reports_the_running_vm(client, platform_admin_headers, db_session, monkeypatch, original_vm):
    _cluster_id, plan_id, agent = _running_plan(client, platform_admin_headers, db_session, monkeypatch, original_vm)

    plan = client.get(f"/api/v1/restore-plans/{plan_id}", headers=platform_admin_headers).json()
    assert plan["execution_status"] == "running_from_backup"
    state = plan["instant_recovery"]
    assert state["disks"] == ["scsi0"]
    assert state["imported_volumes"] == ["tpmstate0"]
    assert state["skipped_devices"] == ["hostpci0"]
    assert state["running_since"] is not None

    jobs = client.get("/api/v1/lab-agent/instant-recovery-jobs", headers=agent).json()
    assert jobs[0]["kind"] == "watch"
    assert jobs[0]["mapped_devices"][0]["device"] == "/dev/loop3"
    assert jobs[0]["vm_created"] is True
    assert jobs[0]["name"].startswith("restore-")

    heartbeat = client.post(
        f"/api/v1/lab-agent/instant-recovery-jobs/{plan_id}/heartbeat",
        json={"overlay_bytes": 5 * 2**30, "storage_total_bytes": 100 * 2**30, "storage_free_bytes": 50 * 2**30},
        headers=agent,
    )
    assert heartbeat.status_code == 204
    state = client.get(f"/api/v1/restore-plans/{plan_id}", headers=platform_admin_headers).json()["instant_recovery"]
    assert state["overlay_bytes"] == 5 * 2**30
    assert state["last_heartbeat_at"] is not None


def test_a_failed_start_that_cleaned_up_leaves_nothing_to_discard(
    client, platform_admin_headers, db_session, monkeypatch, original_vm
):
    cluster_id, snapshot_id, agent = _setup(client, platform_admin_headers, db_session, monkeypatch)
    plan_id = _create_instant_plan(client, platform_admin_headers, cluster_id, snapshot_id).json()["id"]
    client.post(f"/api/v1/restore-plans/{plan_id}/execute", headers=platform_admin_headers)
    client.post(f"/api/v1/lab-agent/instant-recovery-jobs/{plan_id}/claim", headers=agent)
    client.post(
        f"/api/v1/lab-agent/instant-recovery-jobs/{plan_id}/progress",
        json={
            "mapped_device": {
                "slot": "scsi0",
                "archive": "drive-scsi0.img.fidx",
                "device": "/dev/loop3",
                "unit": f"resurgam-ir-{plan_id}-scsi0",
            }
        },
        headers=agent,
    )

    client.post(
        f"/api/v1/lab-agent/instant-recovery-jobs/{plan_id}/status",
        json={"status": "failed", "error": "only 3 GiB free", "cleaned_up": True},
        headers=agent,
    )

    plan = client.get(f"/api/v1/restore-plans/{plan_id}", headers=platform_admin_headers).json()
    assert plan["execution_status"] == "failed"
    assert plan["execution_error"] == "only 3 GiB free"
    discard = client.post(
        f"/api/v1/restore-plans/{plan_id}/instant-recovery/discard",
        json={"confirm": True},
        headers=platform_admin_headers,
    )
    assert discard.status_code == 409
    assert discard.json()["code"] == "nothing_to_discard"


def test_a_lost_mapping_fails_the_plan(client, platform_admin_headers, db_session, monkeypatch, original_vm):
    _cluster_id, plan_id, agent = _running_plan(client, platform_admin_headers, db_session, monkeypatch, original_vm)

    client.post(
        f"/api/v1/lab-agent/instant-recovery-jobs/{plan_id}/status",
        json={"status": "lost", "error": "mapping gone"},
        headers=agent,
    )

    plan = client.get(f"/api/v1/restore-plans/{plan_id}", headers=platform_admin_headers).json()
    assert plan["execution_status"] == "failed"
    # Overlays are kept for an expert; the plan stays discardable.
    assert plan["instant_recovery"]["disks"] == ["scsi0"]


# --- move to storage --------------------------------------------------------------


def test_move_to_storage_moves_disk_by_disk_then_the_agent_finishes(
    client, platform_admin_headers, db_session, monkeypatch, original_vm, test_settings
):
    _cluster_id, plan_id, agent = _running_plan(client, platform_admin_headers, db_session, monkeypatch, original_vm)
    calls = []
    task = {"status": "running", "exitstatus": None}
    monkeypatch.setattr(lifecycle, "move_vm_disk", lambda *a, **kw: calls.append(kw) or f"UPID:pve1:move-{kw['disk']}:")
    monkeypatch.setattr(
        lifecycle,
        "get_task_status",
        lambda *a, **kw: PveTaskStatus(reachable=True, status=task["status"], exitstatus=task["exitstatus"]),
    )

    response = client.post(
        f"/api/v1/restore-plans/{plan_id}/instant-recovery/move",
        json={"bwlimit_kib": 51200},
        headers=platform_admin_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["execution_status"] == "moving_disks"

    lifecycle.advance(db_session, test_settings)
    db_session.commit()
    assert calls == [
        {
            "node": "pve1",
            "vmid": 200,
            "disk": "scsi0",
            "storage": "local-lvm",
            "delete_source": True,
            "bwlimit_kib": 51200,
        }
    ]
    lifecycle.advance(db_session, test_settings)  # still running: nothing changes
    db_session.commit()
    assert len(calls) == 1

    task.update(status="stopped", exitstatus="OK")
    lifecycle.advance(db_session, test_settings)
    db_session.commit()

    plan = client.get(f"/api/v1/restore-plans/{plan_id}", headers=platform_admin_headers).json()
    assert plan["execution_status"] == "finishing"
    assert plan["instant_recovery"]["move_disks"][0]["status"] == "done"
    jobs = client.get("/api/v1/lab-agent/instant-recovery-jobs", headers=agent).json()
    assert jobs[0]["kind"] == "finish"

    client.post(f"/api/v1/lab-agent/instant-recovery-jobs/{plan_id}/status", json={"status": "finished"}, headers=agent)
    plan = client.get(f"/api/v1/restore-plans/{plan_id}", headers=platform_admin_headers).json()
    assert plan["execution_status"] == "completed"
    assert client.get("/api/v1/lab-agent/instant-recovery-jobs", headers=agent).json() == []


def test_a_failed_disk_move_returns_to_running_and_can_be_retried(
    client, platform_admin_headers, db_session, monkeypatch, original_vm, test_settings
):
    _cluster_id, plan_id, _agent = _running_plan(client, platform_admin_headers, db_session, monkeypatch, original_vm)
    monkeypatch.setattr(lifecycle, "move_vm_disk", lambda *a, **kw: "UPID:pve1:x:")
    monkeypatch.setattr(
        lifecycle,
        "get_task_status",
        lambda *a, **kw: PveTaskStatus(reachable=True, status="stopped", exitstatus="storage full"),
    )
    client.post(f"/api/v1/restore-plans/{plan_id}/instant-recovery/move", json={}, headers=platform_admin_headers)

    lifecycle.advance(db_session, test_settings)  # starts the move
    lifecycle.advance(db_session, test_settings)  # sees it fail
    db_session.commit()

    plan = client.get(f"/api/v1/restore-plans/{plan_id}", headers=platform_admin_headers).json()
    assert plan["execution_status"] == "running_from_backup"
    assert "storage full" in plan["instant_recovery"]["last_error"]
    retry = client.post(
        f"/api/v1/restore-plans/{plan_id}/instant-recovery/move", json={}, headers=platform_admin_headers
    )
    assert retry.json()["instant_recovery"]["move_disks"][0]["status"] == "pending"


def test_move_needs_a_vm_running_from_the_backup(client, platform_admin_headers, db_session, monkeypatch, original_vm):
    cluster_id, snapshot_id, _agent = _setup(client, platform_admin_headers, db_session, monkeypatch)
    plan_id = _create_instant_plan(client, platform_admin_headers, cluster_id, snapshot_id).json()["id"]

    response = client.post(
        f"/api/v1/restore-plans/{plan_id}/instant-recovery/move", json={}, headers=platform_admin_headers
    )

    assert response.status_code == 409


# --- discard ---------------------------------------------------------------------------


def test_discard_needs_confirmation_then_the_agent_removes_everything(
    client, platform_admin_headers, db_session, monkeypatch, original_vm
):
    _cluster_id, plan_id, agent = _running_plan(client, platform_admin_headers, db_session, monkeypatch, original_vm)

    unconfirmed = client.post(
        f"/api/v1/restore-plans/{plan_id}/instant-recovery/discard",
        json={"confirm": False},
        headers=platform_admin_headers,
    )
    confirmed = client.post(
        f"/api/v1/restore-plans/{plan_id}/instant-recovery/discard",
        json={"confirm": True},
        headers=platform_admin_headers,
    )

    assert unconfirmed.status_code == 400
    assert confirmed.json()["execution_status"] == "discarding"
    jobs = client.get("/api/v1/lab-agent/instant-recovery-jobs", headers=agent).json()
    assert jobs[0]["kind"] == "discard"

    client.post(
        f"/api/v1/lab-agent/instant-recovery-jobs/{plan_id}/status",
        json={"status": "discard_failed", "error": "qm destroy timed out"},
        headers=agent,
    )
    # Retried after the backoff, not on the very next poll.
    assert client.get("/api/v1/lab-agent/instant-recovery-jobs", headers=agent).json() == []
    plan = db_session.get(RestorePlan, plan_id)
    db_session.refresh(plan)
    plan.ir_next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    assert client.get("/api/v1/lab-agent/instant-recovery-jobs", headers=agent).json()[0]["kind"] == "discard"

    client.post(
        f"/api/v1/lab-agent/instant-recovery-jobs/{plan_id}/status", json={"status": "discarded"}, headers=agent
    )
    plan = client.get(f"/api/v1/restore-plans/{plan_id}", headers=platform_admin_headers).json()
    assert plan["execution_status"] == "discarded"
    assert plan["instant_recovery"]["disks"] == []


def test_restore_operator_of_another_customer_cannot_discard(
    client, platform_admin_headers, db_session, monkeypatch, original_vm, test_settings
):
    from tests.test_restore_plans_api import _create_customer, _create_customer_scoped_user

    _cluster_id, plan_id, _agent = _running_plan(client, platform_admin_headers, db_session, monkeypatch, original_vm)
    other = _create_customer(db_session, name="Other", slug="other")
    other_operator = _create_customer_scoped_user(db_session, test_settings, other, "restore_operator")

    response = client.post(
        f"/api/v1/restore-plans/{plan_id}/instant-recovery/discard", json={"confirm": True}, headers=other_operator
    )

    assert response.status_code == 404


# --- scheduler timeouts --------------------------------------------------------------------


def test_a_plan_nobody_claims_fails_after_15_minutes(
    client, platform_admin_headers, db_session, monkeypatch, original_vm, test_settings
):
    cluster_id, snapshot_id, _agent = _setup(client, platform_admin_headers, db_session, monkeypatch)
    plan_id = _create_instant_plan(client, platform_admin_headers, cluster_id, snapshot_id).json()["id"]
    client.post(f"/api/v1/restore-plans/{plan_id}/execute", headers=platform_admin_headers)

    changes = lifecycle.advance(db_session, test_settings, now=datetime.now(UTC) + timedelta(minutes=16))
    db_session.commit()

    assert [action for _id, action in changes] == ["instant_recovery_failed"]
    plan = client.get(f"/api/v1/restore-plans/{plan_id}", headers=platform_admin_headers).json()
    assert plan["execution_status"] == "failed"
    assert "did not pick up" in plan["execution_error"]


# --- alarms and warnings ----------------------------------------------------------------------


def test_alarms_for_a_long_run_a_full_overlay_storage_and_leftovers(
    client, platform_admin_headers, db_session, monkeypatch, original_vm
):
    _cluster_id, plan_id, agent = _running_plan(client, platform_admin_headers, db_session, monkeypatch, original_vm)
    client.post(
        f"/api/v1/lab-agent/instant-recovery-jobs/{plan_id}/heartbeat",
        json={"overlay_bytes": 1, "storage_total_bytes": 100, "storage_free_bytes": 15},
        headers=agent,
    )
    plan = db_session.get(RestorePlan, plan_id)
    db_session.refresh(plan)
    plan.ir_running_since = datetime.now(UTC) - timedelta(hours=25)
    db_session.commit()

    alarms = {a["type"]: a for a in client.get("/api/v1/alarms", headers=platform_admin_headers).json()}

    assert alarms["instant_recovery_running_long"]["entity_id"] == plan_id
    assert "25 h" in alarms["instant_recovery_running_long"]["message"]
    assert alarms["instant_recovery_overlay_storage"]["severity"] == "critical"

    client.post(f"/api/v1/lab-agent/instant-recovery-jobs/{plan_id}/status", json={"status": "lost"}, headers=agent)
    alarms = {a["type"] for a in client.get("/api/v1/alarms", headers=platform_admin_headers).json()}
    assert alarms == {"instant_recovery_failed"}


def test_maintenance_mode_asks_for_confirmation_while_a_vm_reads_from_the_datastore(
    client, platform_admin_headers, db_session, monkeypatch, original_vm
):
    from resurgam_api.api.v1 import pbs_instances as pbs_instances_module
    from resurgam_api.models.datastore import Datastore

    _running_plan(client, platform_admin_headers, db_session, monkeypatch, original_vm)
    datastore = db_session.query(Datastore).filter_by(name="store1").one()
    calls = []
    monkeypatch.setattr(pbs_instances_module, "set_datastore_maintenance_mode", lambda *a, **kw: calls.append(kw))
    url = f"/api/v1/pbs-instances/{datastore.pbs_instance_id}/datastores/store1/maintenance-mode"

    refused = client.put(url, json={"mode": "offline"}, headers=platform_admin_headers)
    confirmed = client.put(
        url, json={"mode": "offline", "confirm_instant_recovery": True}, headers=platform_admin_headers
    )
    cleared = client.put(url, json={"mode": None}, headers=platform_admin_headers)

    assert refused.status_code == 409
    assert refused.json()["code"] == "instant_recovery_active"
    assert refused.json()["details"]["restore_plans"][0]["vmid"] == 200
    assert confirmed.status_code == 200
    assert cleared.status_code == 200  # ending maintenance never needs a confirmation
    assert len(calls) == 2
    instance = client.get(f"/api/v1/pbs-instances/{datastore.pbs_instance_id}", headers=platform_admin_headers).json()
    assert instance["instant_recoveries_reading"] == ["store1"]
