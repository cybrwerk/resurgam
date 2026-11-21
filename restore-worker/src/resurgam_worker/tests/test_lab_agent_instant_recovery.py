"""resurgam-lab-agent's Instant Recovery work (ADR 0030): the VM config built
from the backup, the start order (scoped mappings, overlays, create, EFI/TPM
import, start) and its cleanup on failure, the heartbeat / lost-mapping watch,
and finish / discard."""

import os
import subprocess

import pytest

from resurgam_worker.lab_agent import instant_recovery as ir
from resurgam_worker.lab_agent.client import InstantRecoveryClaim, InstantRecoveryJob
from resurgam_worker.lab_agent.config import LabAgentConfig
from resurgam_worker.pbs_client import map as pbs_map
from resurgam_worker.pbs_client.errors import PbsCommandFailedError
from resurgam_worker.pbs_client.models import MappedImage
from resurgam_worker.pve_client import qm
from resurgam_worker.pve_client.errors import PveCommandFailedError, PveOutputParseError

PLAN_ID = "5d0c2b1a-0000-4000-8000-000000000030"
SNAPSHOT = "vm/116/2026-09-14T00:48:57Z"

ORIGINAL = {
    "boot": "order=scsi0;ide2;net0;scsi2",
    "machine": "pc-q35-9.2",
    "bios": "ovmf",
    "ostype": "win11",
    "smbios1": "uuid=0b6f1c2e-1111-4222-8333-444455556666",
    "scsihw": "virtio-scsi-single",
    "scsi0": "local-lvm:vm-116-disk-1,cache=writeback,discard=on,iothread=1,size=64G,ssd=1",
    "scsi1": "local-lvm:vm-116-disk-2,size=200G",
    "scsi2": "local-lvm:vm-116-disk-3,backup=0,size=500G",
    "ide2": "local:iso/virtio-win.iso,media=cdrom,size=700M",
    "efidisk0": "local-lvm:vm-116-disk-0,efitype=4m,pre-enrolled-keys=1,size=4M",
    "tpmstate0": "local-lvm:vm-116-disk-4,size=4M,version=v2.0",
    "net0": "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0,firewall=1,tag=20",
    "net1": "e1000=BC:24:11:AA:BB:DD,bridge=vmbr1",
    "hostpci0": "0000:01:00.0",
    "onboot": "1",
    "protection": "1",
    "tags": "prod;win",
    "unused0": "local-lvm:vm-116-disk-9",
    "meta": "creation-qemu=9.2.0,ctime=1700000000",
}
ARCHIVES = [
    "drive-efidisk0.img.fidx",
    "drive-scsi0.img.fidx",
    "drive-scsi1.img.fidx",
    "drive-tpmstate0-backup.img.fidx",
]


def _config(tmp_path, **overrides) -> LabAgentConfig:
    values = {
        "backend_url": "https://resurgam.example/api/v1",
        "agent_token": "agent-token",
        "pbs_token": "resurgam@pbs!lab:pbs-secret",
        "node_name": "pve1",
        "overlay_dir": str(tmp_path / "overlays"),
        "overlay_storage_id": "resurgam-lab-overlays",
    }
    values.update(overrides)
    return LabAgentConfig(**values)


def _claim(**overrides) -> InstantRecoveryClaim:
    data = {
        "id": PLAN_ID,
        "target_node": "pve1",
        "target_vmid": 200,
        "target_storage": "local-lvm",
        "name": "restore-srv-1a2b3c4d",
        "pbs_host": "pbs.example",
        "pbs_port": 8007,
        "pbs_datastore": "store1",
        "pbs_namespace": "",
        "snapshot_path": SNAPSHOT,
        "network_mode": "isolated",
        "network_bridge": "vmbr9",
        "keep_mac": False,
        "min_free_bytes": 0,
        "encryption_keyfile": None,
    }
    data.update(overrides)
    return InstantRecoveryClaim.model_validate(data)


def _job(kind, **overrides) -> InstantRecoveryJob:
    data = {
        "id": PLAN_ID,
        "kind": kind,
        "target_node": "pve1",
        "target_vmid": 200,
        "name": "restore-srv-1a2b3c4d",
        "vm_created": True,
        "snapshot_path": SNAPSHOT,
        "mapped_devices": [
            {
                "slot": "scsi0",
                "archive": "drive-scsi0.img.fidx",
                "device": "/dev/loop3",
                "unit": f"resurgam-ir-{PLAN_ID}-scsi0",
            }
        ],
    }
    data.update(overrides)
    return InstantRecoveryJob.model_validate(data)


class FakeClient:
    def __init__(self, claim=None):
        self.claim = claim or _claim()
        self.calls: list[tuple] = []

    def claim_instant_recovery_job(self, plan_id):
        self.calls.append(("claim", plan_id))
        return self.claim

    def report_instant_recovery_progress(self, plan_id, **fields):
        self.calls.append(("progress", fields))

    def report_instant_recovery_status(self, plan_id, status, **kw):
        self.calls.append(("status", status, kw))

    def report_instant_recovery_heartbeat(self, plan_id, **kw):
        self.calls.append(("heartbeat", kw))

    def statuses(self):
        return [c for c in self.calls if c[0] == "status"]


# --- naming and the VM config ---------------------------------------------------


def test_slot_from_archive_knows_the_tpm_archive_name():
    assert ir.slot_from_archive("drive-scsi0.img.fidx") == "scsi0"
    assert ir.slot_from_archive("drive-efidisk0.img.fidx") == "efidisk0"
    assert ir.slot_from_archive("drive-tpmstate0-backup.img.fidx") == "tpmstate0"


def test_vm_config_keeps_the_hardware_and_points_disks_at_the_overlays():
    config, skipped = ir.build_instant_vm_config(
        ORIGINAL,
        vmid=200,
        overlay_storage_id="resurgam-lab-overlays",
        data_slots=["scsi0", "scsi1"],
        name="restore-srv",
        network_mode="original",
        network_bridge=None,
        keep_mac=True,
        plan_id=PLAN_ID,
    )

    assert config["machine"] == "pc-q35-9.2"
    assert config["smbios1"] == ORIGINAL["smbios1"]
    assert config["scsi0"] == (
        "resurgam-lab-overlays:200/vm-200-ir-scsi0.qcow2,cache=writeback,discard=on,iothread=1,ssd=1"
    )
    assert config["scsi1"] == "resurgam-lab-overlays:200/vm-200-ir-scsi1.qcow2"
    assert config["ide2"] == "none,media=cdrom"
    # Not in the backup / host-bound / state of the original: left out.
    assert "scsi2" not in config and "hostpci0" not in config
    assert sorted(skipped) == ["hostpci0", "scsi2"]
    for key in ("efidisk0", "tpmstate0", "onboot", "protection", "unused0", "meta"):
        assert key not in config
    assert config["boot"] == "order=scsi0;ide2;net0"
    assert config["net0"] == "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0,firewall=1,tag=20"
    assert config["tags"] == "prod;win;resurgam-instant-recovery"
    assert config["name"] == "restore-srv"
    assert PLAN_ID in config["description"]


@pytest.mark.parametrize(
    ("mode", "bridge", "keep_mac", "expected"),
    [
        ("isolated", "vmbr9", False, "virtio,firewall=1,bridge=vmbr9,link_down=1"),
        # No bridge chosen: stays on the original bridge, keeps its VLAN, link down.
        ("isolated", None, False, "virtio,bridge=vmbr0,firewall=1,tag=20,link_down=1"),
        ("custom", "vmbr9", True, "virtio=BC:24:11:AA:BB:CC,firewall=1,bridge=vmbr9"),
        ("none", None, False, "virtio,bridge=vmbr0,firewall=1,tag=20,link_down=1"),
        ("original", None, True, "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0,firewall=1,tag=20"),
    ],
)
def test_network_modes_and_mac_handling(mode, bridge, keep_mac, expected):
    config, _ = ir.build_instant_vm_config(
        ORIGINAL,
        vmid=200,
        overlay_storage_id="o",
        data_slots=["scsi0"],
        name="n",
        network_mode=mode,
        network_bridge=bridge,
        keep_mac=keep_mac,
        plan_id=PLAN_ID,
    )
    assert config["net0"] == expected
    assert config["net1"].startswith("e1000" if not keep_mac else "e1000=BC:24:11:AA:BB:DD")


def test_imported_volume_keeps_the_original_drive_options():
    value = ir._with_options("local-lvm:vm-200-disk-0,size=4M", ORIGINAL["tpmstate0"])
    assert value == "local-lvm:vm-200-disk-0,version=v2.0"
    value = ir._with_options("local-lvm:vm-200-disk-1", ORIGINAL["efidisk0"])
    assert value == "local-lvm:vm-200-disk-1,efitype=4m,pre-enrolled-keys=1"


# --- start ------------------------------------------------------------------------


@pytest.fixture
def start_env(tmp_path, monkeypatch):
    """Every external call of a start, recorded in `env["calls"]`."""
    env = {"calls": [], "fail_on": None}
    calls = env["calls"]

    def record(name, result=None):
        def fake(*args, **kwargs):
            calls.append((name, args, kwargs))
            if env["fail_on"] == name:
                raise PveCommandFailedError([name], 1, "boom")
            return result

        return fake

    loops = iter(["/dev/loop3", "/dev/loop4"])
    monkeypatch.setattr(ir, "fetch_original_vm_config", record("fetch_config", ORIGINAL))
    monkeypatch.setattr(ir, "list_snapshot_archives", record("list_archives", ARCHIVES))

    def fake_map(repository, snapshot, archive, **kwargs):
        calls.append(("map", (archive,), kwargs))
        return MappedImage(device_path=next(loops))

    monkeypatch.setattr(ir, "map_snapshot", fake_map)

    def fake_overlay(device, path):
        calls.append(("overlay", (device, path), {}))
        open(path, "w").close()

    monkeypatch.setattr(ir, "create_cow_overlay", fake_overlay)

    def fake_restore(repository, snapshot, archive, target, **kwargs):
        calls.append(("restore_image", (archive, target), kwargs))
        open(target, "w").close()

    monkeypatch.setattr(ir, "restore_image", fake_restore)
    monkeypatch.setattr(ir, "create_vm", record("create_vm"))
    monkeypatch.setattr(
        ir,
        "import_disk",
        lambda vmid, src, storage, **kw: (
            calls.append(("import", (src, storage), kw))
            or f"{storage}:vm-200-disk-{os.path.basename(src).split('.')[0]}"
        ),
    )
    monkeypatch.setattr(ir, "set_vm_options", record("set_options"))
    monkeypatch.setattr(ir, "start_vm", record("start_vm"))
    monkeypatch.setattr(ir, "vm_status", record("vm_status", "stopped"))
    monkeypatch.setattr(ir, "stop_vm", record("stop_vm"))
    monkeypatch.setattr(ir, "destroy_vm", record("destroy_vm"))
    monkeypatch.setattr(ir, "unmap", lambda device, **kw: calls.append(("unmap", (device,), kw)))
    return env


def test_start_maps_in_scopes_imports_efi_and_tpm_then_starts(tmp_path, start_env):
    config = _config(tmp_path)
    client = FakeClient()

    ir.start_instant_recovery(_job("start"), config, client)

    names = [c[0] for c in start_env["calls"]]
    assert names == [
        "fetch_config",
        "list_archives",
        "map",
        "overlay",
        "map",
        "overlay",
        "create_vm",
        "restore_image",
        "import",
        "set_options",
        "restore_image",
        "import",
        "set_options",
        "start_vm",
    ]
    maps = [c for c in start_env["calls"] if c[0] == "map"]
    assert [m[2]["scope_unit"] for m in maps] == [f"resurgam-ir-{PLAN_ID}-scsi0", f"resurgam-ir-{PLAN_ID}-scsi1"]
    assert maps[0][2]["token"] == "pbs-secret"
    imports = [c for c in start_env["calls"] if c[0] == "import"]
    # Imported as unusedN, attached with the original options in one `qm set`
    # (importing straight into tpmstate0 pins TPM v1.2 -- live finding 2026-11-20).
    assert all("target_disk" not in i[2] for i in imports)
    assert [os.path.basename(i[1][0]) for i in imports] == ["efidisk0.raw", "tpmstate0.raw"]
    assert all(i[1][1] == "local-lvm" and i[2]["disk_format"] == "raw" for i in imports)
    sets = [c[1][1] for c in start_env["calls"] if c[0] == "set_options"]
    assert sets[1] == {"tpmstate0": "local-lvm:vm-200-disk-tpmstate0,version=v2.0"}
    # The restored raw files are removed once imported; only the overlays stay.
    assert sorted(os.listdir(ir.overlay_dir_for(config, 200))) == ["vm-200-ir-scsi0.qcow2", "vm-200-ir-scsi1.qcow2"]

    progress = [c[1] for c in client.calls if c[0] == "progress"]
    assert progress[0]["mapped_device"] == {
        "slot": "scsi0",
        "archive": "drive-scsi0.img.fidx",
        "device": "/dev/loop3",
        "unit": f"resurgam-ir-{PLAN_ID}-scsi0",
    }
    assert {"vm_created": True} in progress
    assert progress[-1] == {"phase": "starting"}
    assert client.statuses() == [("status", "running", {"skipped_disks": ["scsi2", "hostpci0"]})]


def test_a_failed_start_removes_the_vm_and_unmaps(tmp_path, start_env):
    start_env["fail_on"] = "start_vm"
    config = _config(tmp_path)
    client = FakeClient()

    ir.start_instant_recovery(_job("start"), config, client)

    names = [c[0] for c in start_env["calls"]]
    assert names[-4:] == ["vm_status", "destroy_vm", "unmap", "unmap"]
    assert not os.path.exists(ir.overlay_dir_for(config, 200))
    [(_, status, kw)] = client.statuses()
    assert status == "failed" and kw["cleaned_up"] is True


def test_a_failed_unmap_keeps_the_overlays_and_says_so(tmp_path, start_env, monkeypatch):
    start_env["fail_on"] = "create_vm"

    def failing_unmap(device, **kw):
        raise PbsCommandFailedError(["unmap"], 1, "busy")

    monkeypatch.setattr(ir, "unmap", failing_unmap)
    config = _config(tmp_path)
    client = FakeClient()

    ir.start_instant_recovery(_job("start"), config, client)

    [(_, status, kw)] = client.statuses()
    assert status == "failed" and kw["cleaned_up"] is False
    assert os.path.exists(ir.overlay_dir_for(config, 200))


def test_start_refuses_when_the_overlay_storage_is_too_full(tmp_path, start_env):
    client = FakeClient(_claim(min_free_bytes=2**62))

    ir.start_instant_recovery(_job("start"), _config(tmp_path), client)

    assert [c[0] for c in start_env["calls"]] == []
    [(_, status, kw)] = client.statuses()
    assert status == "failed" and "GiB free" in kw["error"]


def test_start_refuses_the_overlay_storage_as_target(tmp_path, start_env):
    client = FakeClient(_claim(target_storage="resurgam-lab-overlays"))

    ir.start_instant_recovery(_job("start"), _config(tmp_path), client)

    [(_, status, kw)] = client.statuses()
    assert status == "failed" and "overlay storage" in kw["error"]


def test_start_refuses_an_overlay_dir_with_leftovers(tmp_path, start_env):
    config = _config(tmp_path)
    leftover = ir.overlay_dir_for(config, 200)
    os.makedirs(leftover)
    open(os.path.join(leftover, "vm-200-ir-scsi0.qcow2"), "w").close()
    client = FakeClient()

    ir.start_instant_recovery(_job("start"), config, client)

    [(_, status, kw)] = client.statuses()
    assert status == "failed" and "already holds files" in kw["error"]
    assert os.listdir(leftover) == ["vm-200-ir-scsi0.qcow2"]  # never deletes what it didn't create


# --- watch -------------------------------------------------------------------------


def _ok(device):
    return True


def test_watch_sends_a_heartbeat_at_most_once_a_minute(tmp_path):
    config = _config(tmp_path)
    os.makedirs(ir.overlay_dir_for(config, 200))
    client = FakeClient()
    beats: dict[str, float] = {}
    now = [1000.0]

    for t in (1000.0, 1030.0, 1061.0):
        now[0] = t
        ir.watch_instant_recovery(
            _job("watch"), config, client, beats, monotonic=lambda: now[0], is_active=lambda u: True, probe=_ok
        )

    heartbeats = [c for c in client.calls if c[0] == "heartbeat"]
    assert len(heartbeats) == 2
    assert heartbeats[0][1]["storage_total_bytes"] > 0


def test_watch_reports_a_lost_mapping(tmp_path):
    client = FakeClient()

    ir.watch_instant_recovery(_job("watch"), _config(tmp_path), client, {}, is_active=lambda unit: False, probe=_ok)

    [(_, status, kw)] = client.statuses()
    assert status == "lost" and "scsi0" in kw["error"]


def test_watch_does_not_call_a_mapping_lost_when_systemd_cannot_be_asked(tmp_path):
    config = _config(tmp_path)
    os.makedirs(config.overlay_dir)
    client = FakeClient()

    ir.watch_instant_recovery(_job("watch"), config, client, {}, is_active=lambda unit: None, probe=_ok)

    assert client.statuses() == []
    assert [c[0] for c in client.calls] == ["heartbeat"]


def _watch_twice(tmp_path, results, *, on_overlay=True):
    """Two watch passes a minute apart, the probe answering from `results`."""
    config = _config(tmp_path)
    os.makedirs(ir.overlay_dir_for(config, 200))
    client = FakeClient()
    beats: dict[str, float] = {}
    failures: dict[str, int] = {}
    answers = iter(results)
    for t in (1000.0, 1061.0):
        ir.watch_instant_recovery(
            _job("watch"),
            config,
            client,
            beats,
            probe_failures=failures,
            monotonic=lambda t=t: t,
            is_active=lambda u: True,
            probe=lambda device: next(answers),
            still_on_overlay=lambda vmid, slot, storage: on_overlay,
        )
    return client, failures


def test_watch_reports_lost_after_two_failed_reads(tmp_path):
    # Live finding 2026-11-20: after a PBS outage the map process keeps running
    # (scope active) but every read fails for good.
    client, failures = _watch_twice(tmp_path, [False, False])

    [(_, status, kw)] = client.statuses()
    assert status == "lost"
    assert "scsi0" in kw["error"] and "PBS" in kw["error"]
    assert failures == {}
    # The first pass still sent its heartbeat; the second reported instead.
    assert [c[0] for c in client.calls].count("heartbeat") == 1


def test_watch_tolerates_a_single_failed_read(tmp_path):
    client, failures = _watch_twice(tmp_path, [False, True])

    assert client.statuses() == []
    assert failures == {}


def test_watch_ignores_unknown_probe_results(tmp_path):
    client, failures = _watch_twice(tmp_path, [None, None])

    assert client.statuses() == []
    assert failures == {}


def test_watch_ignores_a_dead_mapping_of_a_disk_already_moved(tmp_path):
    client, failures = _watch_twice(tmp_path, [False, False], on_overlay=False)

    assert client.statuses() == []
    assert failures == {}


class _Process:
    def __init__(self, returncode=0, hang=False):
        self.returncode, self.hang = returncode, hang

    def wait(self, timeout=None):
        if self.hang:
            raise subprocess.TimeoutExpired("dd", timeout)
        return self.returncode


@pytest.fixture
def loop_size(monkeypatch):
    """`/sys/class/block/loop3/size` = 8192 sectors (4 MiB = 1024 blocks of 4 KiB)."""
    real_open = open

    def fake_open(path, *args, **kwargs):
        if path == "/sys/class/block/loop3/size":
            import io

            return io.StringIO("8192\n")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)


def test_probe_mapping_reads_one_direct_block_at_a_random_offset(loop_size):
    commands = []

    def popen(command, **kw):
        commands.append(command)
        return _Process(0)

    assert ir.probe_mapping("/dev/loop3", choose=lambda n: n - 1, popen=popen) is True
    assert commands[0][:4] == ["timeout", "-s", "KILL", str(ir.PROBE_TIMEOUT_SECONDS)]
    assert "if=/dev/loop3" in commands[0] and "skip=1023" in commands[0] and "iflag=direct" in commands[0]


@pytest.mark.parametrize(
    ("process", "expected"),
    [
        (_Process(1), False),  # dd: Input/output error
        (_Process(137), False),  # killed by `timeout` after hanging
        (_Process(hang=True), False),  # stuck even past SIGKILL
        (_Process(127), None),  # dd missing
    ],
)
def test_probe_mapping_results(loop_size, process, expected):
    assert ir.probe_mapping("/dev/loop3", popen=lambda command, **kw: process) is expected


def test_probe_mapping_cannot_tell_without_the_device_size():
    assert ir.probe_mapping("/dev/does-not-exist-loop99", popen=lambda *a, **kw: _Process(0)) is None


def test_scope_active_reads_systemctl():
    def run(output):
        return lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, output, "")

    assert ir.scope_active("u", run=run("active\n")) is True
    assert ir.scope_active("u", run=run("inactive\n")) is False
    assert ir.scope_active("u", run=run("")) is None


# --- finish / discard --------------------------------------------------------------------


def test_finish_refuses_while_a_disk_still_uses_an_overlay(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ir, "get_vm_config", lambda vmid: {"scsi0": "local-lvm:vm-200-disk-1", "scsi1": "resurgam-lab-overlays:200/x"}
    )
    unmapped = []
    monkeypatch.setattr(ir, "unmap", lambda device, **kw: unmapped.append(device))
    client = FakeClient()

    ir.finish_instant_recovery(_job("finish"), _config(tmp_path), client)

    assert unmapped == []
    [(_, status, kw)] = client.statuses()
    assert status == "finish_failed" and "scsi1" in kw["error"]


def test_finish_unmaps_removes_the_overlay_dir_and_our_tag(tmp_path, monkeypatch):
    config = _config(tmp_path)
    os.makedirs(ir.overlay_dir_for(config, 200))
    monkeypatch.setattr(
        ir,
        "get_vm_config",
        lambda vmid: {"scsi0": "local-lvm:vm-200-disk-1", "tags": "prod;win;resurgam-instant-recovery"},
    )
    monkeypatch.setattr(ir, "scope_active", lambda unit: True)
    unmapped = []
    monkeypatch.setattr(ir, "unmap", lambda device, **kw: unmapped.append(device))
    options = []
    monkeypatch.setattr(ir, "set_vm_options", lambda vmid, opts: options.append(opts))
    monkeypatch.setattr(ir, "delete_vm_options", lambda vmid, keys: pytest.fail("tags remain, nothing to delete"))
    client = FakeClient()

    ir.finish_instant_recovery(_job("finish"), config, client)

    assert unmapped == ["/dev/loop3"]
    assert not os.path.exists(ir.overlay_dir_for(config, 200))
    assert options[0]["tags"] == "prod;win"
    assert "disks moved to storage" in options[0]["description"]
    assert client.statuses() == [("status", "finished", {})]


OUR_VM = {"name": "restore-srv-1a2b3c4d", "tags": "prod;resurgam-instant-recovery"}


def test_discard_stops_destroys_and_unmaps(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ir, "vm_status", lambda vmid: "running")
    monkeypatch.setattr(ir, "get_vm_config", lambda vmid: OUR_VM)
    monkeypatch.setattr(ir, "stop_vm", lambda vmid: calls.append("stop"))
    monkeypatch.setattr(ir, "destroy_vm", lambda vmid: calls.append("destroy"))
    monkeypatch.setattr(ir, "scope_active", lambda unit: True)
    monkeypatch.setattr(ir, "unmap", lambda device, **kw: calls.append(f"unmap {device}"))
    client = FakeClient()

    ir.discard_instant_recovery(_job("discard"), _config(tmp_path), client)

    assert calls == ["stop", "destroy", "unmap /dev/loop3"]
    assert client.statuses() == [("status", "discarded", {})]


def test_discard_skips_unmapping_a_scope_that_is_already_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(ir, "vm_status", lambda vmid: None)
    monkeypatch.setattr(ir, "scope_active", lambda unit: False)
    monkeypatch.setattr(ir, "unmap", lambda device, **kw: pytest.fail("must not unmap"))
    client = FakeClient()

    ir.discard_instant_recovery(_job("discard"), _config(tmp_path), client)

    assert client.statuses() == [("status", "discarded", {})]


def test_jobs_for_another_node_are_ignored(tmp_path):
    client = FakeClient()
    ir.process_instant_recovery_job(_job("start", target_node="pve2"), _config(tmp_path), client, {})
    assert client.calls == []


# --- PBS / qm helpers ----------------------------------------------------------------------


@pytest.fixture
def binaries(monkeypatch):
    monkeypatch.setattr(qm.shutil, "which", lambda name: f"/usr/bin/{name}")


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_map_in_a_scope_wraps_the_command_in_systemd_run(monkeypatch, binaries):
    commands = []
    monkeypatch.setattr(
        pbs_map,
        "_run",
        lambda command, **kw: commands.append((command, kw)) or _completed(0, "", "Image 'x' mapped on /dev/loop7\n"),
    )

    image = pbs_map.map_snapshot(
        "u@pbs.example:8007:store1",
        SNAPSHOT,
        "drive-scsi0.img.fidx",
        token="t",
        scope_unit=f"resurgam-ir-{PLAN_ID}-scsi0",
    )

    assert image.device_path == "/dev/loop7"
    command, kw = commands[0]
    assert command[:5] == [
        "/usr/bin/systemd-run",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit=resurgam-ir-{PLAN_ID}-scsi0",
    ]
    assert command[5:7] == ["/usr/bin/proxmox-backup-client", "map"]
    assert kw["token"] == "t"


def test_map_rejects_an_odd_scope_name(binaries):
    with pytest.raises(Exception, match="scope_unit"):
        pbs_map.map_snapshot("r", SNAPSHOT, "drive-scsi0.img.fidx", scope_unit="bad name; rm -rf /")


def test_restore_image_writes_the_archive_to_a_file(monkeypatch, binaries):
    commands = []
    monkeypatch.setattr(pbs_map, "_run", lambda command, **kw: commands.append(command) or _completed())

    pbs_map.restore_image("r", SNAPSHOT, "drive-tpmstate0-backup.img.fidx", "/var/lib/x/tpmstate0.raw", namespace="ns")

    assert commands[0] == [
        "/usr/bin/proxmox-backup-client",
        "restore",
        SNAPSHOT,
        "drive-tpmstate0-backup.img.fidx",
        "/var/lib/x/tpmstate0.raw",
        "--repository",
        "r",
        "--ns",
        "ns",
    ]


def test_import_disk_returns_the_new_slot_value(monkeypatch, binaries):
    commands = []
    monkeypatch.setattr(qm, "run_subprocess", lambda command, **kw: commands.append(command) or _completed())
    monkeypatch.setattr(qm, "_run", lambda command: _completed(0, "tpmstate0: local-lvm:vm-200-disk-0,size=4M\n"))

    value = qm.import_disk(200, "/x/tpm.raw", "local-lvm", target_disk="tpmstate0", disk_format="raw")

    assert value == "local-lvm:vm-200-disk-0,size=4M"
    assert commands[0] == [
        "/usr/bin/qm",
        "disk",
        "import",
        "200",
        "/x/tpm.raw",
        "local-lvm",
        "--target-disk",
        "tpmstate0",
        "--format",
        "raw",
    ]


def test_import_disk_without_target_returns_the_new_unused_volume(monkeypatch, binaries):
    commands = []
    configs = iter(
        [
            "unused0: local-lvm:vm-200-disk-0\n",
            "unused0: local-lvm:vm-200-disk-0\nunused1: local-lvm:vm-200-disk-1\n",
        ]
    )
    monkeypatch.setattr(qm, "run_subprocess", lambda command, **kw: commands.append(command) or _completed())
    monkeypatch.setattr(qm, "_run", lambda command: _completed(0, next(configs)))

    value = qm.import_disk(200, "/x/tpm.raw", "local-lvm", disk_format="raw")

    assert value == "local-lvm:vm-200-disk-1"
    assert commands[0] == ["/usr/bin/qm", "disk", "import", "200", "/x/tpm.raw", "local-lvm", "--format", "raw"]


def test_import_disk_without_target_fails_when_no_new_volume_appears(monkeypatch, binaries):
    monkeypatch.setattr(qm, "run_subprocess", lambda command, **kw: _completed())
    monkeypatch.setattr(qm, "_run", lambda command: _completed(0, "name: x\n"))

    with pytest.raises(PveOutputParseError):
        qm.import_disk(200, "/x/tpm.raw", "local-lvm")


def test_vm_status_parses_qm_status_and_a_missing_vm(monkeypatch, binaries):
    monkeypatch.setattr(qm, "_run", lambda command: _completed(0, "status: running\n"))
    assert qm.vm_status(200) == "running"
    monkeypatch.setattr(qm, "_run", lambda command: _completed(2, "", "Configuration file 'x' does not exist\n"))
    assert qm.vm_status(200) is None


def test_claim_repr_hides_the_key():
    claim = _claim(encryption_keyfile="SECRET-KEY")
    assert "SECRET-KEY" not in repr(claim)


@pytest.mark.parametrize(
    "foreign",
    [
        {"name": "restore-srv-1a2b3c4d", "tags": "prod"},  # tag removed
        {"name": "someone-elses-vm", "tags": "resurgam-instant-recovery"},  # renamed / reused
    ],
)
def test_discard_never_touches_a_vm_that_is_not_ours_any_more(tmp_path, monkeypatch, foreign):
    monkeypatch.setattr(ir, "vm_status", lambda vmid: "running")
    monkeypatch.setattr(ir, "get_vm_config", lambda vmid: foreign)
    monkeypatch.setattr(ir, "stop_vm", lambda vmid: pytest.fail("must not stop"))
    monkeypatch.setattr(ir, "destroy_vm", lambda vmid: pytest.fail("must not destroy"))
    monkeypatch.setattr(ir, "unmap", lambda device, **kw: pytest.fail("must not unmap before the VM is gone"))
    client = FakeClient()

    ir.discard_instant_recovery(_job("discard"), _config(tmp_path), client)

    [(_, status, kw)] = client.statuses()
    assert status == "discard_failed" and "does not touch it" in kw["error"]


def test_discard_after_a_start_that_never_created_the_vm_leaves_the_vmid_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(ir, "vm_status", lambda vmid: pytest.fail("must not even look at the VMID"))
    monkeypatch.setattr(ir, "scope_active", lambda unit: True)
    unmapped = []
    monkeypatch.setattr(ir, "unmap", lambda device, **kw: unmapped.append(device))
    client = FakeClient()

    ir.discard_instant_recovery(_job("discard", vm_created=False), _config(tmp_path), client)

    assert unmapped == ["/dev/loop3"]
    assert client.statuses() == [("status", "discarded", {})]
