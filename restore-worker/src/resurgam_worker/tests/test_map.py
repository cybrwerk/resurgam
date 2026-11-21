"""`map.py` wraps `proxmox-backup-client map`/`unmap`/`snapshot files` --
confirmed live (2026-09-19) against a real PBS 4.2 instance: a real
130MB VM disk archive mapped as `/dev/loop0` with zero copy (a genuine
readable MBR disk, `fdisk -l` confirmed), and cleanly unmapped afterward
(`losetup -a` confirmed empty). See ADR 0019.
"""

import os
import subprocess

import pytest

from resurgam_worker.pbs_client import map as map_module
from resurgam_worker.pbs_client.errors import (
    InvalidPathError,
    PbsCommandFailedError,
    PbsOutputParseError,
)

REPO = "resurgam@pbs!dev@pbs1.example.internal:backup-store"
SNAPSHOT = "vm/114/2023-10-20T00:25:29Z"


def _fake_completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _fake_which(monkeypatch):
    monkeypatch.setattr(map_module.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_map_snapshot_parses_real_output_shape(monkeypatch):
    # Confirmed live 2026-09-19 -- the exact real stdout line.
    real_output = (
        "Image 'resurgam@pbs!dev@pbs1.example.internal:8007:backup-store:"
        "vm/114/2023-10-20T00:25:29Z/drive-scsi0.img' mapped on /dev/loop0\n"
    )
    captured = {}

    def fake_run(command, **kw):
        captured["command"] = command
        return _fake_completed(0, stdout=real_output)

    monkeypatch.setattr(map_module, "_run", fake_run)

    result = map_module.map_snapshot(REPO, SNAPSHOT, "drive-scsi0.img.fidx", namespace="tenant-a", token="secret")

    assert result.device_path == "/dev/loop0"
    assert captured["command"] == [
        "/usr/bin/proxmox-backup-client",
        "map",
        SNAPSHOT,
        "drive-scsi0.img.fidx",
        "--repository",
        REPO,
        "--ns",
        "tenant-a",
    ]


def test_map_snapshot_parses_output_on_stderr(monkeypatch):
    """Confirmed live (2026-09-21, a real end-to-end Recovery Lab run):
    the "mapped on ..." line lands on stderr for this PBS client build,
    not stdout -- the original code only ever searched stdout, so a
    genuinely successful map was always treated as a parse failure.
    """
    real_output = (
        "Image 'resurgam@pbs!lab-agent@pbs1.example.internal:8007:backup-store:"
        "vm/116/2026-09-21T00:46:50Z/drive-sata0.img' mapped on /dev/loop0\n"
    )

    monkeypatch.setattr(map_module, "_run", lambda *a, **kw: _fake_completed(0, stdout="", stderr=real_output))

    result = map_module.map_snapshot(REPO, SNAPSHOT, "drive-sata0.img.fidx", namespace="tenant-a", token="secret")

    assert result.device_path == "/dev/loop0"


def test_map_snapshot_omits_ns_flag_when_no_namespace(monkeypatch):
    captured = {}

    def fake_run(command, **kw):
        captured["command"] = command
        return _fake_completed(0, stdout="mapped on /dev/loop1")

    monkeypatch.setattr(map_module, "_run", fake_run)
    map_module.map_snapshot(REPO, SNAPSHOT, "drive-scsi0.img.fidx")

    assert "--ns" not in captured["command"]


def test_map_snapshot_raises_on_command_failure(monkeypatch):
    monkeypatch.setattr(map_module, "_run", lambda *a, **kw: _fake_completed(1, stderr="snapshot not found"))

    with pytest.raises(PbsCommandFailedError):
        map_module.map_snapshot(REPO, SNAPSHOT, "drive-scsi0.img.fidx")


def test_map_snapshot_raises_on_unparseable_output(monkeypatch):
    monkeypatch.setattr(map_module, "_run", lambda *a, **kw: _fake_completed(0, stdout="something unexpected"))

    with pytest.raises(PbsOutputParseError):
        map_module.map_snapshot(REPO, SNAPSHOT, "drive-scsi0.img.fidx")


def test_map_snapshot_rejects_empty_snapshot():
    with pytest.raises(InvalidPathError):
        map_module.map_snapshot(REPO, "", "drive-scsi0.img.fidx")


def test_unmap_calls_real_command_shape(monkeypatch):
    captured = {}

    def fake_run(command, **kw):
        captured["command"] = command
        return _fake_completed(0)

    monkeypatch.setattr(map_module, "_run", fake_run)
    map_module.unmap("/dev/loop0", token="secret")

    assert captured["command"] == ["/usr/bin/proxmox-backup-client", "unmap", "/dev/loop0"]


def test_unmap_raises_on_failure(monkeypatch):
    monkeypatch.setattr(map_module, "_run", lambda *a, **kw: _fake_completed(1, stderr="not mapped"))

    with pytest.raises(PbsCommandFailedError):
        map_module.unmap("/dev/loop0")


def test_unmap_rejects_empty_name():
    with pytest.raises(InvalidPathError):
        map_module.unmap("")


def test_list_snapshot_archives_filters_to_disk_archives(monkeypatch):
    payload = (
        '[{"filename": "drive-scsi0.img.fidx", "size": 1024}, '
        '{"filename": "drive-scsi1.img.fidx", "size": 2048}, '
        '{"filename": "index.json.blob", "size": 10}]'
    )
    monkeypatch.setattr(map_module, "_run", lambda *a, **kw: _fake_completed(0, stdout=payload))

    archives = map_module.list_snapshot_archives(REPO, SNAPSHOT)

    assert archives == ["drive-scsi0.img.fidx", "drive-scsi1.img.fidx"]


def test_list_snapshot_archives_raises_on_bad_json(monkeypatch):
    monkeypatch.setattr(map_module, "_run", lambda *a, **kw: _fake_completed(0, stdout="not json"))

    with pytest.raises(PbsOutputParseError):
        map_module.list_snapshot_archives(REPO, SNAPSHOT)


def test_fetch_original_vm_config_parses_real_output_shape(monkeypatch):
    # Confirmed live (2026-09-22) -- the exact real output of
    # `proxmox-backup-client restore <snapshot> qemu-server.conf.blob -`
    # against a real Windows guest's backup.
    real_output = (
        "agent: 1\n"
        "boot: order=sata0;net0;ide2\n"
        "cores: 2\n"
        "machine: pc-i440fx-10.0+pve1\n"
        "memory: 4096\n"
        "name: Office-PC\n"
        "net0: e1000=DA:AE:1F:B7:5F:EC,bridge=vlan005,firewall=1\n"
        "ostype: win10\n"
        "sata0: m2-ssd:vm-116-disk-0,size=80G\n"
        "smbios1: uuid=b24be887-718b-47c0-b987-26b482b632ac\n"
        "#qmdump#map:sata0:drive-sata0:m2-ssd:raw:\n"
    )
    captured = {}

    def fake_run(command, **kw):
        captured["command"] = command
        return _fake_completed(0, stdout=real_output)

    monkeypatch.setattr(map_module, "_run", fake_run)

    config = map_module.fetch_original_vm_config(REPO, SNAPSHOT, namespace="tenant-a", token="secret")

    assert config["machine"] == "pc-i440fx-10.0+pve1"
    assert config["sata0"] == "m2-ssd:vm-116-disk-0,size=80G"
    assert config["ostype"] == "win10"
    # The trailing #qmdump#map comment line must not be parsed as a key.
    assert "#qmdump#map" not in config
    assert captured["command"] == [
        "/usr/bin/proxmox-backup-client",
        "restore",
        SNAPSHOT,
        "qemu-server.conf.blob",
        "-",
        "--repository",
        REPO,
        "--ns",
        "tenant-a",
    ]


def test_fetch_original_vm_config_raises_on_command_failure(monkeypatch):
    monkeypatch.setattr(map_module, "_run", lambda *a, **kw: _fake_completed(1, stderr="snapshot not found"))

    with pytest.raises(PbsCommandFailedError):
        map_module.fetch_original_vm_config(REPO, SNAPSHOT)


def test_fetch_original_vm_config_raises_on_empty_output(monkeypatch):
    monkeypatch.setattr(map_module, "_run", lambda *a, **kw: _fake_completed(0, stdout=""))

    with pytest.raises(PbsOutputParseError):
        map_module.fetch_original_vm_config(REPO, SNAPSHOT)


def test_fetch_original_vm_config_rejects_empty_snapshot():
    with pytest.raises(InvalidPathError):
        map_module.fetch_original_vm_config(REPO, "")


# --- keyfile threading (docs/adr/0022) -------------------------------------------
#
# PBS's own client-side backup encryption means reading an encrypted
# snapshot needs `--keyfile <path>` -- confirmed live (2026-09-25) that
# `map`/`restore` both accept it directly. `keyfile` is passed as raw
# content (matching `token`), materialized to a temp file only for the
# duration of the one call it's needed for.


def test_map_snapshot_appends_keyfile_flag_when_given(monkeypatch):
    real_output = "... mapped on /dev/loop0\n"
    captured = {}

    def fake_run(command, **kw):
        captured["command"] = command
        # Read the keyfile's content *while the `with` block is still
        # open* -- `materialized_keyfile` deletes it in `finally`, right
        # after this call returns, matching real usage (the temp file
        # only ever exists for the duration of the one subprocess call).
        idx = command.index("--keyfile")
        with open(command[idx + 1]) as fh:
            captured["keyfile_content"] = fh.read()
        return _fake_completed(0, stdout=real_output)

    monkeypatch.setattr(map_module, "_run", fake_run)

    map_module.map_snapshot(REPO, SNAPSHOT, "drive-scsi0.img.fidx", keyfile="my-key-content")

    assert "--keyfile" in captured["command"]
    assert captured["keyfile_content"] == "my-key-content"


def test_map_snapshot_omits_keyfile_flag_when_not_given(monkeypatch):
    real_output = "... mapped on /dev/loop0\n"
    captured = {}

    def fake_run(command, **kw):
        captured["command"] = command
        return _fake_completed(0, stdout=real_output)

    monkeypatch.setattr(map_module, "_run", fake_run)

    map_module.map_snapshot(REPO, SNAPSHOT, "drive-scsi0.img.fidx")

    assert "--keyfile" not in captured["command"]


def test_map_snapshot_deletes_keyfile_temp_file_after_call(monkeypatch):
    captured_paths = []

    def fake_run(command, **kw):
        if "--keyfile" in command:
            captured_paths.append(command[command.index("--keyfile") + 1])
        return _fake_completed(0, stdout="... mapped on /dev/loop0\n")

    monkeypatch.setattr(map_module, "_run", fake_run)

    map_module.map_snapshot(REPO, SNAPSHOT, "drive-scsi0.img.fidx", keyfile="my-key-content")

    assert len(captured_paths) == 1
    assert not os.path.exists(captured_paths[0])


def test_fetch_original_vm_config_appends_keyfile_flag_when_given(monkeypatch):
    captured = {}

    def fake_run(command, **kw):
        captured["command"] = command
        return _fake_completed(0, stdout="machine: pc-i440fx-10.0+pve1\n")

    monkeypatch.setattr(map_module, "_run", fake_run)

    map_module.fetch_original_vm_config(REPO, SNAPSHOT, keyfile="my-key-content")

    assert "--keyfile" in captured["command"]
