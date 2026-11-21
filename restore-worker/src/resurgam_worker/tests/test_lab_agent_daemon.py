import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from resurgam_worker.lab_agent import daemon
from resurgam_worker.lab_agent.client import (
    ApplianceSnippetJob,
    FileIndexJob,
    FileIndexJobClaim,
    LabAgentApiError,
    LabAgentJob,
)
from resurgam_worker.lab_agent.config import LabAgentConfig
from resurgam_worker.pbs_client.errors import PbsCommandFailedError
from resurgam_worker.pbs_client.models import MappedImage
from resurgam_worker.pve_client.errors import PveCommandFailedError


def _fake_read_single_chunk(vmid, path, **kw):
    """Fakes `guest_file_read_all` for `_read_and_submit_chunks`'s
    manifest-then-chunks flow -- one chunk containing one row, matching
    every other pre-chunking test's fixture content exactly.
    """
    if "manifest" in path:
        return "1"
    return "f\t4\t1758700800\t/etc/hostname\n"


JOB = LabAgentJob(
    member_id="11111111-1111-1111-1111-111111111111",
    pbs_host="pbs1.example.internal",
    pbs_port=8007,
    pbs_datastore="backup-store",
    pbs_namespace="tenant-a",
    backup_type="vm",
    backup_id="114",
    snapshot_time="2023-10-20T00:25:29Z",
    target_vmid=114,
    target_node="pve1",
    target_storage="local-lvm",
    vnet_name="labe94e9",
    pool="resurgam-lab",
)

CONFIG = LabAgentConfig(
    backend_url="https://resurgam.example.com/api/v1",
    agent_token="agent-token",
    pbs_token="resurgam@pbs!dev:secretvalue",
    overlay_dir="/tmp/resurgam-lab-agent-test-overlays",
)


def test_pbs_repository_format():
    # The port lives inside the repository string itself
    # ([[user@]server[:port]:]datastore) -- PBS's CLI rejects
    # --repository combined with a separate --port flag, confirmed live
    # (2026-09-21) against a real Recovery Lab run.
    assert (
        daemon._pbs_repository("resurgam@pbs!dev", JOB)
        == "resurgam@pbs!dev@pbs1.example.internal:8007:backup-store"
    )


def test_pbs_snapshot_path_format():
    assert daemon._pbs_snapshot_path(JOB) == "vm/114/2023-10-20T00:25:29Z"


ORIGINAL_CONFIG = {
    "agent": "1",
    "machine": "pc-i440fx-10.0+pve1",
    "memory": "4096",
    "sockets": "2",
    "cores": "2",
    "ostype": "win10",
    "scsihw": "virtio-scsi-pci",
    "name": "Office-PC",
    "net0": "e1000=DA:AE:1F:B7:5F:EC,bridge=vlan005,firewall=1",
    "sata0": "m2-ssd:vm-116-disk-0,size=80G",
    "smbios1": "uuid=b24be887-718b-47c0-b987-26b482b632ac",
    # A real gap found live (2026-09-22): `meta` isn't a valid `qm create`
    # option at all -- `qm create` rejected it outright ("Unknown option:
    # meta") on the first real Windows restore attempt using this
    # config-reconstruction approach.
    "meta": "creation-qemu=6.2.0,ctime=1658997122",
}


def test_process_job_happy_path(monkeypatch, tmp_path):
    config = LabAgentConfig(
        backend_url=CONFIG.backend_url,
        agent_token=CONFIG.agent_token,
        pbs_token=CONFIG.pbs_token,
        overlay_dir=str(tmp_path),
    )
    client = MagicMock()
    calls = {}

    monkeypatch.setattr(daemon, "list_snapshot_archives", lambda *a, **kw: ["drive-sata0.img.fidx"])
    monkeypatch.setattr(daemon, "map_snapshot", lambda *a, **kw: MappedImage(device_path="/dev/loop0"))
    monkeypatch.setattr(daemon, "create_cow_overlay", lambda backing, overlay: calls.update(overlay=overlay))
    monkeypatch.setattr(daemon, "fetch_original_vm_config", lambda *a, **kw: dict(ORIGINAL_CONFIG))
    monkeypatch.setattr(
        daemon,
        "create_vm",
        lambda vmid, cfg, **kw: calls.update(create_vm_config=cfg, create_vm_kwargs=kw, create_vm_id=vmid),
    )
    monkeypatch.setattr(daemon, "start_vm", lambda vmid: calls.update(start_vm_id=vmid))

    daemon.process_job(JOB, config, client)

    client.claim_job.assert_called_once_with(JOB.member_id)
    assert calls["create_vm_id"] == 114
    built = calls["create_vm_config"]
    # The disk-slot key must match the archive's own real slot (sata0,
    # not a hardcoded scsi0) -- a real gap found live (2026-09-22): a
    # synthetic config sent a real Windows restore into repair mode.
    # scsi0 must be a storage-relative reference (confirmed live
    # 2026-09-19: `qm` rejects a bare filesystem path), not the raw
    # overlay path `create_cow_overlay` actually wrote to.
    assert built["sata0"] == f"{config.overlay_storage_id}:114/vm-114-disk-0.qcow2"
    # NIC model/MAC preserved from the original, bridge remapped to the
    # Lab VNet, firewall flag dropped (no rules configured for an ad-hoc
    # VNet -- would otherwise silently drop DHCP/probe traffic).
    assert built["net0"] == "e1000=DA:AE:1F:B7:5F:EC,bridge=labe94e9"
    assert built["machine"] == "pc-i440fx-10.0+pve1"
    assert built["ostype"] == "win10"
    assert built["agent"] == "1"
    assert calls["create_vm_kwargs"]["pool"] == "resurgam-lab"
    assert calls["overlay"].endswith("images/114/vm-114-disk-0.qcow2")
    assert calls["start_vm_id"] == 114
    client.report_status.assert_called_once_with(JOB.member_id, "booted")


def test_process_job_reports_failure_and_unmaps_when_create_vm_fails(monkeypatch, tmp_path):
    config = LabAgentConfig(
        backend_url=CONFIG.backend_url,
        agent_token=CONFIG.agent_token,
        pbs_token=CONFIG.pbs_token,
        overlay_dir=str(tmp_path),
    )
    client = MagicMock()
    unmap_calls = []

    monkeypatch.setattr(daemon, "list_snapshot_archives", lambda *a, **kw: ["drive-sata0.img.fidx"])
    monkeypatch.setattr(daemon, "map_snapshot", lambda *a, **kw: MappedImage(device_path="/dev/loop0"))
    monkeypatch.setattr(daemon, "create_cow_overlay", lambda backing, overlay: None)
    monkeypatch.setattr(daemon, "fetch_original_vm_config", lambda *a, **kw: dict(ORIGINAL_CONFIG))
    monkeypatch.setattr(daemon, "unmap", lambda device, **kw: unmap_calls.append(device))

    def _raise_create_vm(vmid, cfg, **kw):
        raise PveCommandFailedError(["qm", "create"], 1, "vmid already exists")

    monkeypatch.setattr(daemon, "create_vm", _raise_create_vm)

    daemon.process_job(JOB, config, client)

    assert unmap_calls == ["/dev/loop0"]
    client.report_status.assert_called_once()
    args = client.report_status.call_args
    assert args[0][0] == JOB.member_id
    assert args[0][1] == "failed"
    assert "vmid already exists" in args[1]["error"]


def test_process_job_reports_failure_when_no_archives(monkeypatch, tmp_path):
    config = LabAgentConfig(
        backend_url=CONFIG.backend_url,
        agent_token=CONFIG.agent_token,
        pbs_token=CONFIG.pbs_token,
        overlay_dir=str(tmp_path),
    )
    client = MagicMock()
    monkeypatch.setattr(daemon, "list_snapshot_archives", lambda *a, **kw: [])
    map_called = []
    monkeypatch.setattr(daemon, "map_snapshot", lambda *a, **kw: map_called.append(1))

    daemon.process_job(JOB, config, client)

    assert map_called == []
    client.report_status.assert_called_once()
    assert client.report_status.call_args[0][1] == "failed"


def test_process_job_propagates_map_failure_without_unmap(monkeypatch, tmp_path):
    """If mapping itself fails, there's nothing to unmap -- confirms the
    best-effort cleanup only fires once a device was actually mapped.
    """
    config = LabAgentConfig(
        backend_url=CONFIG.backend_url,
        agent_token=CONFIG.agent_token,
        pbs_token=CONFIG.pbs_token,
        overlay_dir=str(tmp_path),
    )
    client = MagicMock()
    unmap_calls = []
    monkeypatch.setattr(daemon, "list_snapshot_archives", lambda *a, **kw: ["drive-scsi0.img.fidx"])

    def _raise_map(*a, **kw):
        raise PbsCommandFailedError(["proxmox-backup-client", "map"], 1, "snapshot not found")

    monkeypatch.setattr(daemon, "map_snapshot", _raise_map)
    monkeypatch.setattr(daemon, "unmap", lambda device, **kw: unmap_calls.append(device))

    daemon.process_job(JOB, config, client)

    assert unmap_calls == []
    assert client.report_status.call_args[0][1] == "failed"


# --- process_appliance_job ---------------------------------------------------

APPLIANCE_JOB = ApplianceSnippetJob(
    plan_id="fc0324af-d75c-4833-a7f1-97bb2594b16f",
    snippet_storage_id="local",
    filename="resurgam-lab-fc0324af.yaml",
    content="#cloud-config\nwrite_files: []\n",
)


def test_process_appliance_job_writes_file_and_confirms(tmp_path, monkeypatch):
    resolved_path = tmp_path / "resurgam-lab-fc0324af.yaml"
    monkeypatch.setattr(daemon, "resolve_storage_path", lambda volume_id: str(resolved_path))
    client = MagicMock()

    daemon.process_appliance_job(APPLIANCE_JOB, client)

    assert resolved_path.read_text() == APPLIANCE_JOB.content
    client.confirm_snippet_written.assert_called_once_with(APPLIANCE_JOB.plan_id)


def test_process_appliance_job_uses_snippet_storage_volume_id(monkeypatch, tmp_path):
    captured = {}

    def fake_resolve(volume_id):
        captured["volume_id"] = volume_id
        return str(tmp_path / "out.yaml")

    monkeypatch.setattr(daemon, "resolve_storage_path", fake_resolve)
    daemon.process_appliance_job(APPLIANCE_JOB, MagicMock())

    assert captured["volume_id"] == "local:snippets/resurgam-lab-fc0324af.yaml"


# --- _build_member_vm_config --------------------------------------------------


def test_build_member_vm_config_remaps_the_real_disk_slot():
    """A real, decisive gap found live (2026-09-22): the archive this
    guest actually mapped is `drive-sata0.img.fidx`, but the earlier
    synthetic config hardcoded `scsi0` -- the wrong controller entirely
    for this guest, sending its real Windows restore into repair mode.
    """
    config = daemon._build_member_vm_config(
        ORIGINAL_CONFIG,
        archive_name="drive-sata0.img.fidx",
        overlay_storage_id="resurgam-lab-overlays",
        target_vmid=111,
        overlay_filename="vm-111-disk-0.qcow2",
        name="lab-member-abcd1234",
        vnet_name="labe94e9",
    )
    assert config["sata0"] == "resurgam-lab-overlays:111/vm-111-disk-0.qcow2"
    assert "scsi0" not in config


def test_build_member_vm_config_preserves_nic_model_remaps_bridge_drops_firewall():
    config = daemon._build_member_vm_config(
        ORIGINAL_CONFIG,
        archive_name="drive-sata0.img.fidx",
        overlay_storage_id="resurgam-lab-overlays",
        target_vmid=111,
        overlay_filename="vm-111-disk-0.qcow2",
        name="lab-member-abcd1234",
        vnet_name="labe94e9",
    )
    assert config["net0"] == "e1000=DA:AE:1F:B7:5F:EC,bridge=labe94e9"


def test_build_member_vm_config_preserves_boot_critical_fields():
    config = daemon._build_member_vm_config(
        ORIGINAL_CONFIG,
        archive_name="drive-sata0.img.fidx",
        overlay_storage_id="resurgam-lab-overlays",
        target_vmid=111,
        overlay_filename="vm-111-disk-0.qcow2",
        name="lab-member-abcd1234",
        vnet_name="labe94e9",
    )
    assert config["machine"] == "pc-i440fx-10.0+pve1"
    assert config["ostype"] == "win10"
    assert config["smbios1"] == ORIGINAL_CONFIG["smbios1"]
    assert config["memory"] == "4096"
    assert config["sockets"] == "2"
    assert config["agent"] == "1"
    assert config["name"] == "lab-member-abcd1234"


def test_build_member_vm_config_strips_non_create_keys():
    config = daemon._build_member_vm_config(
        ORIGINAL_CONFIG,
        archive_name="drive-sata0.img.fidx",
        overlay_storage_id="resurgam-lab-overlays",
        target_vmid=111,
        overlay_filename="vm-111-disk-0.qcow2",
        name="lab-member-abcd1234",
        vnet_name="labe94e9",
    )
    assert "meta" not in config
    assert "lock" not in config
    assert "digest" not in config
    assert "parent" not in config


def test_build_member_vm_config_drops_other_disk_and_net_slots():
    original = dict(ORIGINAL_CONFIG, scsi1="m2-ssd:vm-116-disk-1,size=20G", net1="virtio=AA:BB,bridge=vlan006")
    config = daemon._build_member_vm_config(
        original,
        archive_name="drive-sata0.img.fidx",
        overlay_storage_id="resurgam-lab-overlays",
        target_vmid=111,
        overlay_filename="vm-111-disk-0.qcow2",
        name="lab-member-abcd1234",
        vnet_name="labe94e9",
    )
    assert "scsi1" not in config
    assert "net1" not in config


def test_build_member_vm_config_falls_back_when_original_has_no_net0():
    original = {k: v for k, v in ORIGINAL_CONFIG.items() if k != "net0"}
    config = daemon._build_member_vm_config(
        original,
        archive_name="drive-sata0.img.fidx",
        overlay_storage_id="resurgam-lab-overlays",
        target_vmid=111,
        overlay_filename="vm-111-disk-0.qcow2",
        name="lab-member-abcd1234",
        vnet_name="labe94e9",
    )
    assert config["net0"] == "model=virtio,bridge=labe94e9"


# --- _build_guest_exec_ephemeral_vm_config (ADR 0021) -------------------------


def test_build_guest_exec_ephemeral_vm_config_disconnects_network_but_preserves_nic_model():
    """A real bug found live (2026-09-24): an earlier version hardcoded
    `net0 = "model=virtio,..."`, discarding the guest's real NIC model
    (here, `e1000`) entirely -- a real Windows ephemeral clone with the
    NIC model changed out from under it never booted successfully
    (guest agent unreachable, persistently black display). The model/MAC
    must be preserved -- only `bridge=`/`firewall=` are replaced/dropped,
    matching Recovery Lab's own `_build_member_vm_config` precedent, not
    a full rebuild.
    """
    config = daemon._build_guest_exec_ephemeral_vm_config(
        ORIGINAL_CONFIG,
        archive_name="drive-sata0.img.fidx",
        overlay_storage_id="resurgam-lab-overlays",
        target_vmid=111,
        overlay_filename="vm-111-disk-0.qcow2",
        name="file-index-abcd1234",
    )
    assert config["net0"] == "e1000=DA:AE:1F:B7:5F:EC,bridge=vmbr0,link_down=1"


def test_build_guest_exec_ephemeral_vm_config_falls_back_when_original_has_no_net0():
    original = {k: v for k, v in ORIGINAL_CONFIG.items() if k != "net0"}
    config = daemon._build_guest_exec_ephemeral_vm_config(
        original,
        archive_name="drive-sata0.img.fidx",
        overlay_storage_id="resurgam-lab-overlays",
        target_vmid=111,
        overlay_filename="vm-111-disk-0.qcow2",
        name="file-index-abcd1234",
    )
    assert config["net0"] == "model=virtio,bridge=vmbr0,link_down=1"


def test_build_guest_exec_ephemeral_vm_config_remaps_disk_and_drops_extras():
    original = dict(ORIGINAL_CONFIG, scsi1="m2-ssd:vm-116-disk-1,size=20G", net1="virtio=AA:BB,bridge=vlan006")
    config = daemon._build_guest_exec_ephemeral_vm_config(
        original,
        archive_name="drive-sata0.img.fidx",
        overlay_storage_id="resurgam-lab-overlays",
        target_vmid=111,
        overlay_filename="vm-111-disk-0.qcow2",
        name="file-index-abcd1234",
    )
    assert config["sata0"] == "resurgam-lab-overlays:111/vm-111-disk-0.qcow2"
    assert "scsi0" not in config
    assert "scsi1" not in config
    assert "net1" not in config
    assert config["name"] == "file-index-abcd1234"
    assert config["agent"] == "1"
    assert config["ostype"] == "win10"


# --- _submit_entries_in_batches ------------------------------------------------


def test_submit_entries_in_batches_splits_across_batch_size(monkeypatch):
    monkeypatch.setattr(daemon, "_ENTRY_SUBMIT_BATCH_SIZE", 2)
    client = MagicMock()
    rows = [{"path": f"/f{i}", "filename": f"f{i}", "type": "file", "size_bytes": 1, "mtime": None} for i in range(5)]

    daemon._submit_entries_in_batches(client, "job-1", rows)

    assert client.submit_file_index_entries.call_count == 3
    sizes = [len(call.args[1]) for call in client.submit_file_index_entries.call_args_list]
    assert sizes == [2, 2, 1]


def test_submit_entries_in_batches_serializes_mtime_to_isoformat():
    client = MagicMock()
    mtime = datetime(2026, 9, 24, 10, 0, 0, tzinfo=UTC)
    rows = [{"path": "/f", "filename": "f", "type": "file", "size_bytes": 1, "mtime": mtime}]

    daemon._submit_entries_in_batches(client, "job-1", rows)

    entries = client.submit_file_index_entries.call_args.args[1]
    assert entries[0].mtime == mtime.isoformat()


# --- process_guest_exec_live_job ------------------------------------------------

LIVE_JOB = FileIndexJob(
    id="job-live-1",
    file_index_method="guest_exec_live",
    target_vmid=114,
    target_node="pve1",
)


def test_process_guest_exec_live_job_happy_path(monkeypatch):
    client = MagicMock()
    client.claim_file_index_job.return_value = FileIndexJobClaim(since=None, is_full_reindex=True)

    monkeypatch.setattr(daemon, "get_vm_config", lambda vmid: {"ostype": "l26"})
    monkeypatch.setattr(daemon, "guest_exec", lambda vmid, args: {"exitcode": 0})
    monkeypatch.setattr(daemon, "guest_exec_launch", lambda vmid, args: 123)
    monkeypatch.setattr(daemon, "guest_file_read_all", _fake_read_single_chunk)

    daemon.process_guest_exec_live_job(LIVE_JOB, client)

    client.claim_file_index_job.assert_called_once_with("job-live-1")
    entries = client.submit_file_index_entries.call_args.args[1]
    assert entries[0].path == "/etc/hostname"
    client.report_file_index_status.assert_called_once_with("job-live-1", "completed")


def test_process_guest_exec_live_job_reports_failure_on_unsupported_ostype(monkeypatch):
    client = MagicMock()
    client.claim_file_index_job.return_value = FileIndexJobClaim(since=None, is_full_reindex=True)
    monkeypatch.setattr(daemon, "get_vm_config", lambda vmid: {"ostype": "unknown-os"})

    daemon.process_guest_exec_live_job(LIVE_JOB, client)

    client.report_file_index_status.assert_called_once()
    assert client.report_file_index_status.call_args.args[1] == "failed"


def test_read_and_submit_chunks_reads_each_chunk_in_order(monkeypatch):
    """The direct fix for a real, decisive ceiling found live
    (2026-09-25): PVE's own `agent/file-read` fails deterministically
    well under 44MB, with no lower-level chunked-read primitive to fall
    back to. Confirms every chunk 0..N-1 is read, parsed, and submitted
    in order -- not assembled into one big in-memory listing first.
    """
    reads = []

    def _fake_read(vmid, path, **kw):
        reads.append(path)
        index = path.rsplit("-", 1)[-1]
        return f"f\t4\t1758700800\t/tmp/file-{index}\n"

    client = MagicMock()
    monkeypatch.setattr(daemon, "guest_file_read_all", _fake_read)

    total = daemon._read_and_submit_chunks(client, "job-x", 114, "l26", 3, node="pve1")

    assert total == 3
    assert reads == [
        "/tmp/resurgam-index-chunk-0000",
        "/tmp/resurgam-index-chunk-0001",
        "/tmp/resurgam-index-chunk-0002",
    ]
    submitted_paths = [call.args[1][0].path for call in client.submit_file_index_entries.call_args_list]
    assert submitted_paths == ["/tmp/file-0000", "/tmp/file-0001", "/tmp/file-0002"]


def test_read_and_submit_chunks_zero_chunks_submits_nothing(monkeypatch):
    monkeypatch.setattr(daemon, "guest_file_read_all", lambda vmid, path, **kw: "")
    client = MagicMock()

    total = daemon._read_and_submit_chunks(client, "job-x", 114, "l26", 0, node="pve1")

    assert total == 0
    client.submit_file_index_entries.assert_not_called()


# --- _wait_for_manifest ----------------------------------------------------------
#
# A real, decisive gap found live (2026-09-25): `qm guest exec-status`
# proved unreliable for a genuinely long-running Windows guest command --
# on a real, busy production VM it kept reporting `exited: 0` (or
# erroring) for 20+ minutes *after* the guest-side script had already
# finished and the manifest already existed on disk. This polls for the
# manifest directly instead of trusting QGA's own pid-exit tracking.


def test_wait_for_manifest_returns_as_soon_as_readable(monkeypatch):
    monkeypatch.setattr(daemon, "guest_file_read_all", lambda vmid, path, **kw: "5\n")

    result = daemon._wait_for_manifest(114, "pve1", "/tmp/resurgam-index-manifest.txt")

    assert result == "5\n"


def test_wait_for_manifest_tolerates_not_found_yet_then_succeeds(monkeypatch):
    """A `PveClientError` (e.g. "file not found" because the
    enumeration hasn't written the manifest yet, or a transient QGA
    hiccup) must not be fatal -- both just mean "keep waiting."
    """
    from resurgam_worker.pve_client.errors import PveCommandFailedError as _Err

    attempts = iter(
        [
            _Err(["pvesh"], 1, "file not found"),
            _Err(["pvesh"], 1, "got timeout"),
            "7\n",
        ]
    )

    def _fake_read(vmid, path, **kw):
        item = next(attempts)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(daemon, "guest_file_read_all", _fake_read)
    monkeypatch.setattr(daemon.time, "sleep", lambda s: None)

    result = daemon._wait_for_manifest(114, "pve1", "/tmp/resurgam-index-manifest.txt")

    assert result == "7\n"


def test_wait_for_manifest_raises_last_error_once_deadline_passes(monkeypatch):
    from resurgam_worker.pve_client.errors import PveCommandFailedError as _Err

    def _always_fail(vmid, path, **kw):
        raise _Err(["pvesh"], 1, "file not found")

    monkeypatch.setattr(daemon, "guest_file_read_all", _always_fail)
    monkeypatch.setattr(daemon.time, "sleep", lambda s: None)
    clock = iter([0.0, 0.0, 0.0, 5.0])
    monkeypatch.setattr(daemon.time, "monotonic", lambda: next(clock))

    with pytest.raises(_Err):
        daemon._wait_for_manifest(114, "pve1", "/tmp/resurgam-index-manifest.txt", timeout_seconds=1.0)


# --- process_guest_exec_ephemeral_job -------------------------------------------

EPHEMERAL_JOB = FileIndexJob(
    id="job-eph-1",
    file_index_method="guest_exec_ephemeral",
    target_vmid=999,
    target_node="pve1",
    target_storage="local-lvm",
    pbs_host="pbs.example",
    pbs_port=8007,
    pbs_datastore="store1",
    pbs_namespace="",
    backup_type="vm",
    backup_id="114",
    snapshot_time="2026-09-24T00:00:00Z",
    pool="resurgam-lab",
)


def _patch_ephemeral_boot(monkeypatch, *, ostype="l26"):
    monkeypatch.setattr(daemon, "list_snapshot_archives", lambda *a, **kw: ["drive-sata0.img.fidx"])
    monkeypatch.setattr(daemon, "map_snapshot", lambda *a, **kw: MappedImage(device_path="/dev/loop0"))
    monkeypatch.setattr(daemon, "create_cow_overlay", lambda *a, **kw: None)
    monkeypatch.setattr(daemon, "fetch_original_vm_config", lambda *a, **kw: dict(ORIGINAL_CONFIG, ostype=ostype))
    monkeypatch.setattr(daemon, "create_vm", lambda *a, **kw: None)
    monkeypatch.setattr(daemon, "start_vm", lambda vmid: None)
    monkeypatch.setattr(daemon, "wait_for_guest_agent", lambda vmid, **kw: None)
    monkeypatch.setattr(daemon, "stop_vm", lambda vmid: None)


def test_process_guest_exec_ephemeral_job_happy_path_tears_down_vm(monkeypatch, tmp_path):
    config = LabAgentConfig(
        backend_url=CONFIG.backend_url,
        agent_token=CONFIG.agent_token,
        pbs_token=CONFIG.pbs_token,
        overlay_dir=str(tmp_path),
    )
    client = MagicMock()
    client.claim_file_index_job.return_value = FileIndexJobClaim(since=None, is_full_reindex=True)
    _patch_ephemeral_boot(monkeypatch)
    monkeypatch.setattr(daemon, "guest_exec", lambda vmid, args: {"exitcode": 0})
    monkeypatch.setattr(daemon, "guest_exec_launch", lambda vmid, args: 123)
    monkeypatch.setattr(daemon, "guest_file_read_all", _fake_read_single_chunk)
    destroy_calls = []
    unmap_calls = []
    monkeypatch.setattr(daemon, "destroy_vm", lambda vmid: destroy_calls.append(vmid))
    monkeypatch.setattr(daemon, "unmap", lambda device, **kw: unmap_calls.append(device))

    daemon.process_guest_exec_ephemeral_job(EPHEMERAL_JOB, config, client)

    client.report_file_index_status.assert_called_once_with("job-eph-1", "completed")
    assert destroy_calls == [999]
    assert unmap_calls == ["/dev/loop0"]


def test_ephemeral_vm_overlay_uses_the_agents_overlay_storage(monkeypatch, tmp_path):
    """Live finding 2026-11-21: the job's target storage (`local-lvm`) was used
    for the overlay volume -- PVE refused `local-lvm:<vmid>/...qcow2`."""
    config = LabAgentConfig(
        backend_url=CONFIG.backend_url,
        agent_token=CONFIG.agent_token,
        pbs_token=CONFIG.pbs_token,
        overlay_dir=str(tmp_path),
    )
    client = MagicMock()
    client.claim_file_index_job.return_value = FileIndexJobClaim(since=None, is_full_reindex=True)
    _patch_ephemeral_boot(monkeypatch)
    created = {}
    monkeypatch.setattr(daemon, "create_vm", lambda vmid, cfg, **kw: created.update(cfg))
    monkeypatch.setattr(daemon, "guest_exec", lambda vmid, args: {"exitcode": 0})
    monkeypatch.setattr(daemon, "guest_exec_launch", lambda vmid, args: 123)
    monkeypatch.setattr(daemon, "guest_file_read_all", _fake_read_single_chunk)
    monkeypatch.setattr(daemon, "destroy_vm", lambda vmid: None)
    monkeypatch.setattr(daemon, "unmap", lambda device, **kw: None)

    daemon.process_guest_exec_ephemeral_job(EPHEMERAL_JOB, config, client)

    disk = next(v for k, v in created.items() if k.startswith(("sata", "scsi", "virtio", "ide")) and "qcow2" in v)
    assert disk.startswith(f"{config.overlay_storage_id}:999/")
    assert EPHEMERAL_JOB.target_storage not in disk


def test_process_guest_exec_ephemeral_job_tears_down_vm_even_on_guest_exec_failure(monkeypatch, tmp_path):
    """The ephemeral VM is never meant to persist -- a failure *after*
    boot must still destroy it, not just a failure before boot.
    """
    config = LabAgentConfig(
        backend_url=CONFIG.backend_url,
        agent_token=CONFIG.agent_token,
        pbs_token=CONFIG.pbs_token,
        overlay_dir=str(tmp_path),
    )
    client = MagicMock()
    client.claim_file_index_job.return_value = FileIndexJobClaim(since=None, is_full_reindex=True)
    _patch_ephemeral_boot(monkeypatch)

    def _raise_guest_exec(vmid, args):
        raise PveCommandFailedError(["qm", "guest", "exec"], 1, "guest agent not responding")

    monkeypatch.setattr(daemon, "guest_exec", lambda vmid, args: {"exitcode": 0})
    monkeypatch.setattr(daemon, "guest_exec_launch", _raise_guest_exec)
    destroy_calls = []
    monkeypatch.setattr(daemon, "destroy_vm", lambda vmid: destroy_calls.append(vmid))
    monkeypatch.setattr(daemon, "unmap", lambda device, **kw: None)

    daemon.process_guest_exec_ephemeral_job(EPHEMERAL_JOB, config, client)

    assert destroy_calls == [999]
    client.report_file_index_status.assert_called_once()
    assert client.report_file_index_status.call_args.args[1] == "failed"


def test_process_guest_exec_ephemeral_job_does_not_destroy_when_never_booted(monkeypatch, tmp_path):
    """If mapping fails before create_vm/start_vm ever ran, there is no
    VM to destroy.
    """
    config = LabAgentConfig(
        backend_url=CONFIG.backend_url,
        agent_token=CONFIG.agent_token,
        pbs_token=CONFIG.pbs_token,
        overlay_dir=str(tmp_path),
    )
    client = MagicMock()
    client.claim_file_index_job.return_value = FileIndexJobClaim(since=None, is_full_reindex=True)
    monkeypatch.setattr(daemon, "list_snapshot_archives", lambda *a, **kw: [])
    destroy_calls = []
    stop_calls = []
    monkeypatch.setattr(daemon, "destroy_vm", lambda vmid: destroy_calls.append(vmid))
    monkeypatch.setattr(daemon, "stop_vm", lambda vmid: stop_calls.append(vmid))

    daemon.process_guest_exec_ephemeral_job(EPHEMERAL_JOB, config, client)

    assert destroy_calls == []
    assert stop_calls == []
    assert client.report_file_index_status.call_args.args[1] == "failed"


def test_process_guest_exec_ephemeral_job_stops_before_destroying(monkeypatch, tmp_path):
    """A real bug found live (2026-09-24): `qm destroy` refuses outright
    on a still-running VM ("VM N is running - destroy failed") -- the
    common case, since most real failures (e.g. a guest-agent timeout)
    leave the VM running; only the readiness *check* failed. `stop_vm`
    must run first, and cleanup must still destroy even if wait_for_guest_agent
    itself is what failed (the VM is definitely still running then).
    """
    config = LabAgentConfig(
        backend_url=CONFIG.backend_url,
        agent_token=CONFIG.agent_token,
        pbs_token=CONFIG.pbs_token,
        overlay_dir=str(tmp_path),
    )
    client = MagicMock()
    client.claim_file_index_job.return_value = FileIndexJobClaim(since=None, is_full_reindex=True)
    _patch_ephemeral_boot(monkeypatch)
    from resurgam_worker.pve_client.errors import GuestAgentTimeoutError

    def _raise_timeout(vmid, **kw):
        raise GuestAgentTimeoutError(vmid, 600.0)

    monkeypatch.setattr(daemon, "wait_for_guest_agent", _raise_timeout)
    calls = []
    monkeypatch.setattr(daemon, "stop_vm", lambda vmid: calls.append(("stop", vmid)))
    monkeypatch.setattr(daemon, "destroy_vm", lambda vmid: calls.append(("destroy", vmid)))
    monkeypatch.setattr(daemon, "unmap", lambda device, **kw: None)

    daemon.process_guest_exec_ephemeral_job(EPHEMERAL_JOB, config, client)

    assert calls == [("stop", 999), ("destroy", 999)]
    assert client.report_file_index_status.call_args.args[1] == "failed"


def test_process_guest_exec_ephemeral_job_destroys_even_when_stop_fails(monkeypatch, tmp_path):
    config = LabAgentConfig(
        backend_url=CONFIG.backend_url,
        agent_token=CONFIG.agent_token,
        pbs_token=CONFIG.pbs_token,
        overlay_dir=str(tmp_path),
    )
    client = MagicMock()
    client.claim_file_index_job.return_value = FileIndexJobClaim(since=None, is_full_reindex=True)
    _patch_ephemeral_boot(monkeypatch)
    monkeypatch.setattr(daemon, "guest_exec", lambda vmid, args: {"exitcode": 0})
    monkeypatch.setattr(daemon, "guest_exec_launch", lambda vmid, args: 123)
    monkeypatch.setattr(daemon, "guest_file_read_all", _fake_read_single_chunk)

    def _raise_stop(vmid):
        raise PveCommandFailedError(["qm", "stop"], 1, "already stopped")

    destroy_calls = []
    monkeypatch.setattr(daemon, "stop_vm", _raise_stop)
    monkeypatch.setattr(daemon, "destroy_vm", lambda vmid: destroy_calls.append(vmid))
    monkeypatch.setattr(daemon, "unmap", lambda device, **kw: None)

    daemon.process_guest_exec_ephemeral_job(EPHEMERAL_JOB, config, client)

    assert destroy_calls == [999]


# --- poll_backup_triggers -------------------------------------------------------


def test_poll_backup_triggers_relays_and_removes_files(tmp_path):
    triggers_dir = tmp_path / "backup-triggers"
    triggers_dir.mkdir()
    (triggers_dir / "114-123.json").write_text(json.dumps({"vmid": 114, "node": "pve1", "at": "2026-09-24T10:00:00Z"}))
    client = MagicMock()

    daemon.poll_backup_triggers(client, triggers_dir=str(triggers_dir))

    client.report_backup_completed.assert_called_once_with(114, "pve1", "2026-09-24T10:00:00Z")
    assert list(triggers_dir.iterdir()) == []


def test_poll_backup_triggers_removes_malformed_file_without_relaying(tmp_path):
    triggers_dir = tmp_path / "backup-triggers"
    triggers_dir.mkdir()
    (triggers_dir / "bad.json").write_text("not json")
    client = MagicMock()

    daemon.poll_backup_triggers(client, triggers_dir=str(triggers_dir))

    client.report_backup_completed.assert_not_called()
    assert list(triggers_dir.iterdir()) == []


def test_poll_backup_triggers_no_ops_when_dir_missing(tmp_path):
    client = MagicMock()
    daemon.poll_backup_triggers(client, triggers_dir=str(tmp_path / "does-not-exist"))
    client.report_backup_completed.assert_not_called()


def test_poll_backup_triggers_removes_file_even_when_relay_fails(tmp_path):
    triggers_dir = tmp_path / "backup-triggers"
    triggers_dir.mkdir()
    (triggers_dir / "114-1.json").write_text(json.dumps({"vmid": 114, "node": "pve1", "at": "2026-09-24T10:00:00Z"}))
    client = MagicMock()
    client.report_backup_completed.side_effect = LabAgentApiError(500, "backend error")

    daemon.poll_backup_triggers(client, triggers_dir=str(triggers_dir))

    assert list(triggers_dir.iterdir()) == []


# --- the main loop never dies ----------------------------------------------------------


class _StopLoop(BaseException):
    pass


def test_run_forever_survives_any_error_in_a_poll_pass(monkeypatch):
    passes = []

    def failing_poll(config, client, state):
        passes.append(1)
        raise RuntimeError("unexpected response shape")

    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            raise _StopLoop

    monkeypatch.setattr(daemon, "load_config", lambda: CONFIG)
    monkeypatch.setattr(daemon, "LabAgentClient", lambda *a, **kw: MagicMock())
    monkeypatch.setattr(daemon, "poll_once", failing_poll)
    monkeypatch.setattr(daemon.time, "sleep", fake_sleep)
    with pytest.raises(_StopLoop):
        daemon.run_forever()
    assert len(passes) == 3


def test_poll_once_tolerates_an_unreachable_backend(monkeypatch, tmp_path):
    client = MagicMock()
    for name in (
        "list_setup_jobs",
        "list_appliance_jobs",
        "list_jobs",
        "list_file_index_jobs",
        "list_disk_mount_jobs",
    ):
        getattr(client, name).side_effect = LabAgentApiError(0, "ConnectError: connection refused")
    monkeypatch.setattr(daemon, "poll_backup_triggers", lambda c: None)
    state = daemon.PollState()
    daemon.poll_once(CONFIG, client, state)
    assert state.setup_thread is None
    client.list_disk_mount_jobs.assert_called_once()


def test_setup_thread_is_remembered_even_when_the_rest_of_the_pass_fails(monkeypatch):
    client = MagicMock()
    client.list_setup_jobs.return_value = [MagicMock(id="j1")]
    client.list_appliance_jobs.side_effect = RuntimeError("boom")
    started = []
    monkeypatch.setattr(daemon, "poll_backup_triggers", lambda c: None)
    monkeypatch.setattr(daemon, "_run_setup_job", lambda job, config, c: started.append(job.id))
    state = daemon.PollState()
    with pytest.raises(RuntimeError):
        daemon.poll_once(CONFIG, client, state)
    assert state.setup_thread is not None
    state.setup_thread.join(1)
    assert started == ["j1"]


def test_run_setup_job_logs_instead_of_crashing_the_thread(monkeypatch, caplog):
    def boom(job, config, client):
        raise ValueError("bad")

    monkeypatch.setattr(daemon, "process_setup_job", boom)
    daemon._run_setup_job(MagicMock(id="j1"), CONFIG, MagicMock())
    assert "crashed" in caplog.text


# --- retried scan start (live finding 2026-11-21) ------------------------------


def test_scan_start_is_retried_after_a_guest_agent_timeout(monkeypatch):
    calls = []

    def launch(vmid, args):
        calls.append(vmid)
        if len(calls) < 3:
            raise PveCommandFailedError(["qm"], 255, "VM 107 qga command 'guest-exec' failed - got timeout")
        return 42

    monkeypatch.setattr(daemon, "guest_exec_launch", launch)
    monkeypatch.setattr(daemon.time, "sleep", lambda s: None)

    daemon._launch_enumeration(107, ["/bin/sh", "-c", "x"])

    assert calls == [107, 107, 107]


def test_scan_start_other_errors_are_not_retried(monkeypatch):
    calls = []

    def launch(vmid, args):
        calls.append(vmid)
        raise PveCommandFailedError(["qm"], 255, "QEMU guest agent is not running")

    monkeypatch.setattr(daemon, "guest_exec_launch", launch)
    monkeypatch.setattr(daemon.time, "sleep", lambda s: None)

    with pytest.raises(PveCommandFailedError):
        daemon._launch_enumeration(107, ["/bin/sh", "-c", "x"])
    assert calls == [107]
