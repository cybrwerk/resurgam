"""`file_restore.py` wraps `proxmox-file-restore` for VM/qemu snapshots —
confirmed live (2026-09-15) against a real PVE 9.2 node with matched
package versions and a real Windows Server VM backup (see ADR 0011).
`fixtures/list_result.json`/`status_result.json` are trimmed real captures
from that session, not synthetic guesses.
"""

import json
import subprocess
from pathlib import Path

import pytest

from resurgam_worker.pbs_client import _subprocess as pbs_subprocess
from resurgam_worker.pbs_client import file_restore
from resurgam_worker.pbs_client.errors import (
    InvalidPathError,
    PathNotMountableError,
    PbsBinaryNotFoundError,
    PbsCommandFailedError,
    PbsOutputParseError,
)
from resurgam_worker.pbs_client.models import ExtractResult, FileRestoreEntryType

FIXTURES = Path(__file__).parent / "fixtures"
REPO = "user@host:datastore"
SNAPSHOT = "vm/116/2026-09-14T00:48:57Z"


def _fake_completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _fake_which(monkeypatch):
    monkeypatch.setattr(file_restore.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_list_path_parses_real_capture_and_decodes_base64_filepath(monkeypatch):
    raw = (FIXTURES / "list_result.json").read_text()
    monkeypatch.setattr(pbs_subprocess, "run", lambda command, **kw: _fake_completed(0, stdout=raw))

    entries = file_restore.list_path(REPO, SNAPSHOT, "/drive-sata0.img.fidx/part/2")

    by_name = {e.name: e for e in entries}
    assert by_name["hiberfil.sys"].type == FileRestoreEntryType.FILE
    assert by_name["hiberfil.sys"].path == "/drive-sata0.img.fidx/part/2/hiberfil.sys"
    assert by_name["hiberfil.sys"].size == 1717768192
    assert by_name["hiberfil.sys"].mtime == 1758437733
    assert by_name["hiberfil.sys"].leaf is True
    assert by_name["$Recycle.Bin"].type == FileRestoreEntryType.DIRECTORY
    assert by_name["$Recycle.Bin"].leaf is False
    assert by_name["logUploaderSettings.ini"].size == 90


def test_list_path_passes_namespace_and_repository(monkeypatch):
    captured = {}

    def fake_run(command, **kw):
        captured["command"] = command
        return _fake_completed(0, stdout="[]")

    monkeypatch.setattr(pbs_subprocess, "run", fake_run)
    file_restore.list_path(REPO, SNAPSHOT, "/etc", namespace="tenant-a")

    assert captured["command"][:3] == ["/usr/bin/proxmox-file-restore", "list", SNAPSHOT]
    assert "--repository" in captured["command"]
    assert REPO in captured["command"]
    assert "--ns" in captured["command"]
    assert "tenant-a" in captured["command"]


def test_list_path_omits_ns_flag_for_root_namespace(monkeypatch):
    captured = {}

    def fake_run(command, **kw):
        captured["command"] = command
        return _fake_completed(0, stdout="[]")

    monkeypatch.setattr(pbs_subprocess, "run", fake_run)
    file_restore.list_path(REPO, SNAPSHOT, "/etc")

    assert "--ns" not in captured["command"]


def test_list_path_rejects_traversal():
    with pytest.raises(InvalidPathError):
        file_restore.list_path(REPO, SNAPSHOT, "/etc/../../root")


def test_list_path_rejects_relative_path():
    with pytest.raises(InvalidPathError):
        file_restore.list_path(REPO, SNAPSHOT, "etc")


def test_list_path_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(pbs_subprocess, "run", lambda command, **kw: _fake_completed(1, stderr="snapshot not found"))

    with pytest.raises(PbsCommandFailedError):
        file_restore.list_path(REPO, SNAPSHOT, "/etc")


def test_list_path_raises_on_malformed_json(monkeypatch):
    monkeypatch.setattr(pbs_subprocess, "run", lambda command, **kw: _fake_completed(0, stdout="not json"))

    with pytest.raises(PbsOutputParseError):
        file_restore.list_path(REPO, SNAPSHOT, "/etc")


def test_list_path_raises_on_unexpected_shape(monkeypatch):
    monkeypatch.setattr(
        pbs_subprocess, "run", lambda command, **kw: _fake_completed(0, stdout=json.dumps([{"unexpected": True}]))
    )

    with pytest.raises(PbsOutputParseError):
        file_restore.list_path(REPO, SNAPSHOT, "/etc")


_LOCK_EINTR = {
    "code": 500,
    "message": 'Unable to acquire lock "/var/log/proxmox-backup/tasks/.active.lock" - Interrupted system call (os error 4)',
}


def test_list_path_retries_transient_lock_error_then_lists(monkeypatch):
    raw = (FIXTURES / "list_result.json").read_text()
    outputs = [json.dumps(_LOCK_EINTR), json.dumps(_LOCK_EINTR), raw]
    sleeps: list[float] = []
    monkeypatch.setattr(file_restore.time, "sleep", sleeps.append)
    monkeypatch.setattr(pbs_subprocess, "run", lambda command, **kw: _fake_completed(0, stdout=outputs.pop(0)))

    entries = file_restore.list_path(REPO, SNAPSHOT, "/etc")

    assert entries
    assert sleeps == [0.5, 2.0]
    assert outputs == []


def test_list_path_gives_up_on_persistent_transient_error(monkeypatch):
    calls = []
    monkeypatch.setattr(file_restore.time, "sleep", lambda _s: None)

    def fake_run(command, **kw):
        calls.append(command)
        return _fake_completed(0, stdout=json.dumps(_LOCK_EINTR))

    monkeypatch.setattr(pbs_subprocess, "run", fake_run)

    with pytest.raises(PathNotMountableError):
        file_restore.list_path(REPO, SNAPSHOT, "/etc")
    assert len(calls) == 4


def test_list_path_does_not_retry_genuinely_unmountable_partition(monkeypatch):
    calls = []
    monkeypatch.setattr(file_restore.time, "sleep", lambda _s: pytest.fail("must not retry"))

    def fake_run(command, **kw):
        calls.append(command)
        return _fake_completed(
            0, stdout=json.dumps({"code": 400, "message": "mounting 'part/1' failed: all mounts failed"})
        )

    monkeypatch.setattr(pbs_subprocess, "run", fake_run)

    with pytest.raises(PathNotMountableError):
        file_restore.list_path(REPO, SNAPSHOT, "/drive-sata0.img.fidx/part/1")
    assert len(calls) == 1


def test_extract_writes_to_target(monkeypatch):
    captured = {}

    def fake_run(command, **kw):
        captured["command"] = command
        return _fake_completed(0)

    monkeypatch.setattr(file_restore, "_run", fake_run)
    result = file_restore.extract(REPO, SNAPSHOT, "/etc/hosts", "/var/tmp/hosts", namespace="tenant-a")

    assert result == ExtractResult(target="/var/tmp/hosts", success=True)
    assert "--ns" in captured["command"]
    assert "tenant-a" in captured["command"]


def test_extract_rejects_relative_target():
    with pytest.raises(InvalidPathError):
        file_restore.extract(REPO, SNAPSHOT, "/etc/hosts", "relative/path")


def test_extract_rejects_dash_target_directs_to_extract_to_bytes():
    with pytest.raises(InvalidPathError):
        file_restore.extract(REPO, SNAPSHOT, "/etc/hosts", "-")


def test_extract_to_bytes_returns_raw_stdout(monkeypatch):
    payload = b"root:x:0:0:root:/root:/bin/bash\n"
    monkeypatch.setattr(
        file_restore,
        "_run",
        lambda command, **kw: subprocess.CompletedProcess(args=["fake"], returncode=0, stdout=payload, stderr=b""),
    )

    data = file_restore.extract_to_bytes(REPO, SNAPSHOT, "/etc/passwd")

    assert data == payload


def test_status_parses_real_capture(monkeypatch):
    raw = (FIXTURES / "status_result.json").read_text()
    monkeypatch.setattr(pbs_subprocess, "run", lambda command, **kw: _fake_completed(0, stdout=raw))

    sessions = file_restore.status()

    assert len(sessions) == 1
    assert sessions[0].name == (
        "qemu_tenant-a@pbs@pbs1.example.internal:8007:backup-store:tenant-a/vm/116/2026-09-14T00:48:57Z"
    )
    assert sessions[0].cid == 10
    assert sessions[0].pid == 1973407
    assert sessions[0].uptime == 455


def test_status_empty_when_no_sessions_running(monkeypatch):
    monkeypatch.setattr(pbs_subprocess, "run", lambda command, **kw: _fake_completed(0, stdout='{"Qemu":{}}'))

    assert file_restore.status() == []


def test_stop_raises_on_failure(monkeypatch):
    monkeypatch.setattr(file_restore, "_run", lambda command, **kw: _fake_completed(1, stderr="no such session"))

    with pytest.raises(PbsCommandFailedError):
        file_restore.stop("vm-101-restore")


def test_stop_rejects_empty_name():
    with pytest.raises(InvalidPathError):
        file_restore.stop("")


def test_missing_binary_raises(monkeypatch):
    monkeypatch.setattr(file_restore.shutil, "which", lambda name: None)

    with pytest.raises(PbsBinaryNotFoundError):
        file_restore.list_path(REPO, SNAPSHOT, "/etc")


def test_a_bare_error_object_is_reported_with_its_message(monkeypatch):
    """`proxmox-file-restore list` exits 0 with `{"message": ...}` for e.g. EACCES or a TLS failure."""
    monkeypatch.setattr(file_restore.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        file_restore,
        "_run_json",
        lambda command, **kw: {"message": "Permission denied (os error 13)"},
    )
    with pytest.raises(PbsCommandFailedError, match="Permission denied"):
        file_restore.list_path("t@pbs!x@h:8007:ds", "vm/1/2026-01-01T00:00:00Z", "/drive-scsi0.img.fidx/part/1")


# --- extract_to_file: streamed single files and directory ZIPs ---------------------------------


def test_extract_to_file_streams_a_single_file_like_extract_to_bytes(monkeypatch, tmp_path):
    from resurgam_worker.pbs_client._subprocess import GuardedResult

    captured = {}

    def fake(command, **kw):
        captured["command"] = command
        captured["kw"] = kw
        Path(kw["stdout_path"]).write_bytes(b"data")
        return GuardedResult(returncode=0, stderr="")

    monkeypatch.setattr(file_restore, "_run_guarded", fake)
    out = tmp_path / "job.bin"
    file_restore.extract_to_file(
        REPO, SNAPSHOT, "/drive-scsi0.img.fidx/part/2/etc/hosts", str(out), namespace="tenant-a"
    )

    assert captured["command"] == [
        "/usr/bin/proxmox-file-restore",
        "extract",
        SNAPSHOT,
        "/drive-scsi0.img.fidx/part/2/etc/hosts",
        "-",
        "--repository",
        REPO,
        "--ns",
        "tenant-a",
    ]
    assert captured["kw"]["stdout_path"] == str(out)
    assert out.read_bytes() == b"data"


def test_extract_to_file_requests_zip_format_for_directories(monkeypatch, tmp_path):
    from resurgam_worker.pbs_client._subprocess import GuardedResult

    captured = {}

    def fake(command, **kw):
        captured["command"] = command
        return GuardedResult(returncode=0, stderr="")

    monkeypatch.setattr(file_restore, "_run_guarded", fake)
    file_restore.extract_to_file(
        REPO, SNAPSHOT, "/drive-scsi0.img.fidx/part/2/Users", str(tmp_path / "x.zip"), as_zip=True, keyfile="KEY"
    )

    command = captured["command"]
    assert command[:5] == [
        "/usr/bin/proxmox-file-restore",
        "extract",
        SNAPSHOT,
        "/drive-scsi0.img.fidx/part/2/Users",
        "-",
    ]
    assert command[command.index("--format") + 1] == "zip"
    assert "--keyfile" in command


def test_extract_to_file_removes_partial_output_on_failure(monkeypatch, tmp_path):
    from resurgam_worker.pbs_client._subprocess import GuardedResult

    out = tmp_path / "job.zip"

    def fake(command, **kw):
        out.write_bytes(b"partial")
        return GuardedResult(returncode=1, stderr="Error: no such file")

    monkeypatch.setattr(file_restore, "_run_guarded", fake)
    with pytest.raises(PbsCommandFailedError):
        file_restore.extract_to_file(REPO, SNAPSHOT, "/drive-scsi0.img.fidx/part/2/x", str(out))
    assert not out.exists()


def test_extract_to_file_rejects_traversal(tmp_path):
    with pytest.raises(InvalidPathError):
        file_restore.extract_to_file(REPO, SNAPSHOT, "/a/../../etc", str(tmp_path / "x"))
