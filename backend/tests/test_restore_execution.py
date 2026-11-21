from datetime import UTC, datetime, timedelta

from resurgam_api.connectivity.models import PveTaskStatus
from resurgam_api.models.restore import RestorePlanExecutionStatus, RestorePlanNetworkMode
from resurgam_api.restore import execution

# --- build_restore_params -------------------------------------------------


def test_build_restore_params_vm_never_includes_net0():
    params = execution.build_restore_params(
        backup_type="vm",
        target_vmid=200,
        volid="pbs-main:backup/vm/116/2026-09-14T00:48:57Z",
        target_storage="local-lvm",
        vmid_intent_is_new=True,
        network_mode=RestorePlanNetworkMode.ISOLATED,
        network_bridge="vmbr1",
        temp_name="restore-116-abc",
    )
    assert "net0" not in params
    assert params["unique"] is True
    assert params["force"] is False
    assert params["name"] == "restore-116-abc"


def test_build_restore_params_existing_vmid_uses_force():
    params = execution.build_restore_params(
        backup_type="vm",
        target_vmid=200,
        volid="v",
        target_storage="s",
        vmid_intent_is_new=False,
        network_mode=RestorePlanNetworkMode.ORIGINAL,
        network_bridge=None,
        temp_name="restore-100-abc",
    )
    assert params["unique"] is False
    assert params["force"] is True


def test_build_restore_params_ct_original_mode_has_no_net0():
    params = execution.build_restore_params(
        backup_type="ct",
        target_vmid=101,
        volid="v",
        target_storage="s",
        vmid_intent_is_new=True,
        network_mode=RestorePlanNetworkMode.ORIGINAL,
        network_bridge=None,
        temp_name="restore-101-abc",
    )
    assert "net0" not in params


# --- needs_network_finalize / needs_start_call -----------------------------


def test_vm_always_needs_finalize_for_non_original_mode():
    assert execution.needs_network_finalize("vm", RestorePlanNetworkMode.NONE) is True
    assert execution.needs_network_finalize("vm", RestorePlanNetworkMode.ISOLATED) is True
    assert execution.needs_network_finalize("vm", RestorePlanNetworkMode.CUSTOM) is True


def test_vm_original_mode_never_needs_finalize():
    assert execution.needs_network_finalize("vm", RestorePlanNetworkMode.ORIGINAL) is False


def test_needs_start_call_matches_flag():
    assert execution.needs_start_call(True) is True
    assert execution.needs_start_call(False) is False


# --- network_updates (every NIC; live finding 2026-11-21) -------------------

TWO_NICS = {
    "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,tag=5",
    "net1": "e1000=AA:BB:CC:DD:EE:00,bridge=vmbr0,firewall=1,tag=35,link_down=0",
    "name": "router",
    "scsi0": "local-lvm:vm-200-disk-0,size=128M",
}
CT_NICS = {"net0": "name=eth0,bridge=vmbr0,hwaddr=AA:BB:CC:DD:EE:FF,ip=dhcp,tag=7", "hostname": "x"}


def test_network_updates_original_changes_nothing():
    assert execution.network_updates(RestorePlanNetworkMode.ORIGINAL, None, TWO_NICS) == {}


def test_network_updates_none_unplugs_every_nic_in_place():
    assert execution.network_updates(RestorePlanNetworkMode.NONE, None, TWO_NICS) == {
        "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,tag=5,link_down=1",
        "net1": "e1000=AA:BB:CC:DD:EE:00,bridge=vmbr0,firewall=1,tag=35,link_down=1",
    }


def test_network_updates_isolated_without_bridge_unplugs_every_nic_in_place():
    assert execution.network_updates(RestorePlanNetworkMode.ISOLATED, None, TWO_NICS) == execution.network_updates(
        RestorePlanNetworkMode.NONE, None, TWO_NICS
    )


def test_network_updates_isolated_with_bridge_moves_every_nic_and_drops_the_vlan():
    assert execution.network_updates(RestorePlanNetworkMode.ISOLATED, "vmbr9", TWO_NICS) == {
        "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr9,link_down=1",
        "net1": "e1000=AA:BB:CC:DD:EE:00,firewall=1,bridge=vmbr9,link_down=1",
    }


def test_network_updates_custom_connects_the_first_nic_and_unplugs_the_rest():
    # The NIC model and MAC stay (a Windows guest keeps its driver).
    assert execution.network_updates(RestorePlanNetworkMode.CUSTOM, "vmbr2", TWO_NICS) == {
        "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr2,link_down=0",
        "net1": "e1000=AA:BB:CC:DD:EE:00,bridge=vmbr0,firewall=1,tag=35,link_down=1",
    }


def test_network_updates_orders_nics_numerically_and_handles_containers():
    nics = {"net10": "virtio=A,bridge=vmbr0", "net2": "virtio=B,bridge=vmbr0", **CT_NICS}
    updates = execution.network_updates(RestorePlanNetworkMode.CUSTOM, "vmbr2", nics)
    assert list(updates) == ["net0", "net2", "net10"]
    assert updates["net0"] == "name=eth0,hwaddr=AA:BB:CC:DD:EE:FF,ip=dhcp,bridge=vmbr2,link_down=0"
    assert updates["net10"].endswith("link_down=1")


def test_ct_restore_params_never_carry_a_network_override():
    params = execution.build_restore_params(
        backup_type="ct",
        target_vmid=200,
        volid="pbs:backup/ct/1/2026-01-01T00:00:00Z",
        target_storage="local-lvm",
        vmid_intent_is_new=True,
        network_mode=RestorePlanNetworkMode.NONE,
        network_bridge=None,
        temp_name="x",
    )
    assert "net0" not in params


def test_ct_needs_finalize_for_non_original_mode():
    assert execution.needs_network_finalize("ct", RestorePlanNetworkMode.NONE)
    assert not execution.needs_network_finalize("ct", RestorePlanNetworkMode.ORIGINAL)


def test_with_link_down_replaces_existing_value():
    result = execution.with_link_down("virtio=AA:BB,bridge=vmbr0,link_down=0", True)
    assert result == "virtio=AA:BB,bridge=vmbr0,link_down=1"


def test_with_link_down_appends_when_absent():
    result = execution.with_link_down("virtio=AA:BB,bridge=vmbr0", False)
    assert result == "virtio=AA:BB,bridge=vmbr0,link_down=0"


# --- next_execution_status --------------------------------------------------


def test_next_status_stays_running_while_pve_task_running():
    status = execution.next_execution_status(
        RestorePlanExecutionStatus.RESTORE_REQUESTED.value, PveTaskStatus(reachable=True, status="running")
    )
    assert status == RestorePlanExecutionStatus.RESTORE_RUNNING.value


def test_next_status_moves_to_finalizing_on_success():
    status = execution.next_execution_status(
        RestorePlanExecutionStatus.RESTORE_RUNNING.value,
        PveTaskStatus(reachable=True, status="stopped", exitstatus="OK"),
    )
    assert status == RestorePlanExecutionStatus.FINALIZING.value


def test_next_status_moves_to_restore_failed_on_nonzero_exit():
    status = execution.next_execution_status(
        RestorePlanExecutionStatus.RESTORE_RUNNING.value,
        PveTaskStatus(reachable=True, status="stopped", exitstatus="some error"),
    )
    assert status == RestorePlanExecutionStatus.RESTORE_FAILED.value


def test_next_status_is_a_noop_for_terminal_or_unrelated_current_status():
    for current in (
        RestorePlanExecutionStatus.COMPLETED.value,
        RestorePlanExecutionStatus.RESTORE_FAILED.value,
        RestorePlanExecutionStatus.PENDING_APPROVAL.value,
    ):
        status = execution.next_execution_status(
            current, PveTaskStatus(reachable=True, status="stopped", exitstatus="OK")
        )
        assert status == current


# --- is_execution_stale -----------------------------------------------------


def test_is_execution_stale_false_within_max_age():
    executed_at = datetime(2026, 1, 1, tzinfo=UTC)
    now = executed_at + timedelta(seconds=100)
    assert execution.is_execution_stale(executed_at, now, max_age_seconds=200) is False


def test_is_execution_stale_true_past_max_age():
    executed_at = datetime(2026, 1, 1, tzinfo=UTC)
    now = executed_at + timedelta(seconds=300)
    assert execution.is_execution_stale(executed_at, now, max_age_seconds=200) is True
