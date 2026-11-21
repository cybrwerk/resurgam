import os
import shutil
import subprocess
from datetime import UTC, datetime

from resurgam_api.models.restore import RestorePlanNetworkMode
from resurgam_api.restore.command import generate_restore_command


def _snapshot_time():
    return datetime(2026, 9, 14, 0, 48, 57, tzinfo=UTC)


SHELLS = [s for s in ("sh", "bash") if shutil.which(s)]
TWO_NICS = {
    "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,tag=5",
    "net1": "e1000=AA:BB:CC:DD:EE:00,bridge=vmbr0,firewall=1,tag=35,link_down=0",
}

# A fake `qm`/`pct`: `config` prints the config file, `set <id> --netN <v>` replaces that line.
_FAKE_TOOL = """#!/bin/sh
CFG="$(dirname "$0")/config"
case "$1" in
  config) cat "$CFG" ;;
  set) key=${3#--}; grep -v "^$key: " "$CFG" > "$CFG.new"; echo "$key: $4" >> "$CFG.new"; mv "$CFG.new" "$CFG" ;;
esac
"""


def _command(backup_type, mode, bridge):
    return generate_restore_command(
        backup_type=backup_type,
        backup_id="116",
        snapshot_time=_snapshot_time(),
        pbs_storage_id="pbs-main",
        target_vmid=200,
        target_storage="local-lvm",
        vmid_intent_is_new=True,
        network_mode=mode,
        network_bridge=bridge,
        start_after_restore=False,
        temp_name="restore-116-abc123",
    )


def _apply_network_line(tmp_path, tool, command, nets, shell):
    """Runs the command's network line against a fake `qm`/`pct` holding
    `nets` (plus a non-NIC line) and returns the NICs afterwards."""
    fake = tmp_path / tool
    fake.write_text(_FAKE_TOOL)
    fake.chmod(0o755)
    config = tmp_path / "config"
    config.write_text("name: x\n" + "".join(f"{k}: {v}\n" for k, v in nets.items()))
    line = next(x for x in command.splitlines() if "--$n" in x)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"}
    result = subprocess.run([shell, "-c", line], capture_output=True, text=True, env=env, check=False)
    assert result.returncode == 0, result.stderr
    out = {}
    for row in config.read_text().splitlines():
        key, _, value = row.partition(": ")
        if key.startswith("net"):
            out[key] = value
    return dict(sorted(out.items()))


def test_vm_restore_new_vmid_uses_unique_flag_and_detaches_network():
    command = generate_restore_command(
        backup_type="vm",
        backup_id="116",
        snapshot_time=_snapshot_time(),
        pbs_storage_id="pbs-main",
        target_vmid=200,
        target_storage="local-lvm",
        vmid_intent_is_new=True,
        network_mode=RestorePlanNetworkMode.NONE,
        network_bridge=None,
        start_after_restore=False,
        temp_name="restore-116-abc123",
    )

    lines = command.splitlines()
    assert lines[0] == (
        "qmrestore pbs-main:backup/vm/116/2026-09-14T00:48:57Z 200 --storage local-lvm --unique 1 "
        "--name restore-116-abc123"
    )
    # "none" mode disconnects every real NIC (read back via `qm config`)
    # rather than deleting it outright.
    assert "--delete net" not in command
    assert "qm set 200 --$n" in command
    assert "qm config 200" in command
    assert "link_down=1" in command
    assert "powered off" in command
    assert "qm start" not in command


def test_vm_restore_existing_vmid_uses_force_flag():
    command = generate_restore_command(
        backup_type="vm",
        backup_id="100",
        snapshot_time=_snapshot_time(),
        pbs_storage_id="pbs-main",
        target_vmid=100,
        target_storage="local-lvm",
        vmid_intent_is_new=False,
        network_mode=RestorePlanNetworkMode.NONE,
        network_bridge=None,
        start_after_restore=False,
        temp_name="restore-100-abc123",
    )

    assert "--force 1" in command.splitlines()[0]
    assert "--unique" not in command.splitlines()[0]
    assert "--name restore-100-abc123" in command.splitlines()[0]


def test_vm_restore_start_after_restore_adds_instruction_not_execution():
    command = generate_restore_command(
        backup_type="vm",
        backup_id="116",
        snapshot_time=_snapshot_time(),
        pbs_storage_id="pbs-main",
        target_vmid=200,
        target_storage="local-lvm",
        vmid_intent_is_new=True,
        network_mode=RestorePlanNetworkMode.NONE,
        network_bridge=None,
        start_after_restore=True,
        temp_name="restore-116-abc123",
    )

    assert "# To start it: qm start 200" in command
    # The instruction is a comment, never an executable line on its own.
    assert "\nqm start 200" not in command


def test_vm_restore_isolated_network_moves_every_nic_to_the_bridge(tmp_path):
    command = _command("vm", RestorePlanNetworkMode.ISOLATED, "vmbr99")
    for shell in SHELLS:
        assert _apply_network_line(tmp_path, "qm", command, TWO_NICS, shell) == {
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr99,link_down=1",
            "net1": "e1000=AA:BB:CC:DD:EE:00,firewall=1,bridge=vmbr99,link_down=1",
        }


def test_vm_restore_none_and_isolated_without_bridge_unplug_every_nic_in_place(tmp_path):
    # Live finding 2026-11-21: a restored two-NIC router VM in "isolated"
    # mode (no isolated bridge configured) kept both NICs on their VLANs.
    for mode in (RestorePlanNetworkMode.NONE, RestorePlanNetworkMode.ISOLATED):
        command = _command("vm", mode, None)
        for shell in SHELLS:
            assert _apply_network_line(tmp_path, "qm", command, TWO_NICS, shell) == {
                "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,tag=5,link_down=1",
                "net1": "e1000=AA:BB:CC:DD:EE:00,bridge=vmbr0,firewall=1,tag=35,link_down=1",
            }


def test_vm_restore_none_mode_strips_preexisting_link_down_before_appending(tmp_path):
    """A NIC already unplugged in the backup must not end up with two
    `link_down=` keys -- runs the generated sed pipeline for real."""
    command = _command("vm", RestorePlanNetworkMode.NONE, None)
    nets = _apply_network_line(
        tmp_path, "qm", command, {"net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,link_down=1"}, SHELLS[0]
    )
    assert nets == {"net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,link_down=1"}


def test_vm_restore_custom_connects_first_nic_and_unplugs_the_rest(tmp_path):
    command = _command("vm", RestorePlanNetworkMode.CUSTOM, "vmbr2")
    assert _apply_network_line(tmp_path, "qm", command, TWO_NICS, SHELLS[0]) == {
        "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr2,link_down=0",
        "net1": "e1000=AA:BB:CC:DD:EE:00,bridge=vmbr0,firewall=1,tag=35,link_down=1",
    }


def test_vm_restore_original_network_mode_has_no_follow_up_qm_set():
    command = generate_restore_command(
        backup_type="vm",
        backup_id="116",
        snapshot_time=_snapshot_time(),
        pbs_storage_id="pbs-main",
        target_vmid=200,
        target_storage="local-lvm",
        vmid_intent_is_new=True,
        network_mode=RestorePlanNetworkMode.ORIGINAL,
        network_bridge=None,
        start_after_restore=False,
        temp_name="restore-116-abc123",
    )

    assert "qm set" not in command


def test_ct_restore_uses_pct_restore_then_unplugs_every_nic(tmp_path):
    command = generate_restore_command(
        backup_type="ct",
        backup_id="1121",
        snapshot_time=_snapshot_time(),
        pbs_storage_id="pbs-main",
        target_vmid=300,
        target_storage="local-lvm",
        vmid_intent_is_new=True,
        network_mode=RestorePlanNetworkMode.NONE,
        network_bridge=None,
        start_after_restore=False,
        temp_name="restore-1121-abc123",
    )

    lines = command.splitlines()
    assert lines[0] == (
        "pct restore 300 pbs-main:backup/ct/1121/2026-09-14T00:48:57Z --storage local-lvm --unique 1"
        " --hostname restore-1121-abc123"
    )
    assert _apply_network_line(
        tmp_path, "pct", command, {"net0": "name=eth0,bridge=vmbr0,hwaddr=AA:BB:CC:DD:EE:FF,ip=dhcp"}, SHELLS[0]
    ) == {"net0": "name=eth0,bridge=vmbr0,hwaddr=AA:BB:CC:DD:EE:FF,ip=dhcp,link_down=1"}
    assert "stopped" in command


def test_ct_restore_existing_vmid_uses_force_flag():
    command = generate_restore_command(
        backup_type="ct",
        backup_id="1121",
        snapshot_time=_snapshot_time(),
        pbs_storage_id="pbs-main",
        target_vmid=1121,
        target_storage="local-lvm",
        vmid_intent_is_new=False,
        network_mode=RestorePlanNetworkMode.CUSTOM,
        network_bridge="vmbr5",
        start_after_restore=True,
        temp_name="restore-1121-abc123",
    )

    lines = command.splitlines()
    assert "--force 1" in lines[0]
    assert "bridge=vmbr5" in lines[1]
    assert "--hostname restore-1121-abc123" in lines[0]
    assert "# To start it: pct start 1121" in command
