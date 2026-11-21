"""`pbs_client._subprocess` -- the shared run/run_json/materialized_keyfile
helpers every PBS client wrapper (`map.py`, `catalog.py`, `file_restore.py`)
uses, so keyfile-materialization and PBS_PASSWORD env handling are each
written and tested once, not duplicated per module.
"""

import os
import stat

import pytest

from resurgam_worker.pbs_client import _subprocess


def test_materialized_keyfile_yields_none_for_none_content():
    with _subprocess.materialized_keyfile(None) as path:
        assert path is None


def test_materialized_keyfile_writes_content_with_restrictive_permissions():
    captured_path = None
    with _subprocess.materialized_keyfile("keyfile-plaintext-content") as path:
        captured_path = path
        assert path is not None
        with open(path) as fh:
            assert fh.read() == "keyfile-plaintext-content"
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == stat.S_IRUSR | stat.S_IWUSR

    assert not os.path.exists(captured_path)


def test_materialized_keyfile_deletes_file_even_on_exception():
    captured_path = None
    with pytest.raises(RuntimeError), _subprocess.materialized_keyfile("some-key") as path:
        captured_path = path
        raise RuntimeError("boom")

    assert not os.path.exists(captured_path)


def test_run_accepts_a_timeout_override(monkeypatch):
    captured = {}

    def fake_run_subprocess(command, **kw):
        captured.update(kw)
        import subprocess as sp

        return sp.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_subprocess, "run_subprocess", fake_run_subprocess)

    _subprocess.run(["proxmox-backup-client", "restore"], timeout=300)

    assert captured["timeout"] == 300


def test_run_defaults_to_module_timeout(monkeypatch):
    captured = {}

    def fake_run_subprocess(command, **kw):
        captured.update(kw)
        import subprocess as sp

        return sp.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_subprocess, "run_subprocess", fake_run_subprocess)

    _subprocess.run(["proxmox-backup-client", "snapshot", "files"])

    assert captured["timeout"] == _subprocess.COMMAND_TIMEOUT_SECONDS


# --- run_guarded: long data-producing calls (folder/multi-selection ZIP downloads) ------------


def test_run_guarded_streams_stdout_to_a_file_and_collects_stderr(tmp_path):
    out = tmp_path / "out.bin"
    result = _subprocess.run_guarded(
        ["sh", "-c", "printf 'payload'; echo 'a warning' >&2"], stdout_path=str(out), guard_interval=0.05
    )
    assert result.returncode == 0
    assert out.read_bytes() == b"payload"
    assert "a warning" in result.stderr
    assert stat.S_IMODE(os.stat(out).st_mode) & 0o007 == 0


def test_run_guarded_passes_token_and_fingerprint_via_env_only(tmp_path):
    out = tmp_path / "env.txt"
    _subprocess.run_guarded(
        ["sh", "-c", 'printf "%s|%s" "$PBS_PASSWORD" "$PBS_FINGERPRINT"'],
        token="s3cret",
        fingerprint="AABB",
        stdout_path=str(out),
        guard_interval=0.05,
    )
    assert out.read_text() == "s3cret|aa:bb"
    assert "PBS_PASSWORD" not in os.environ


def test_run_guarded_guard_exception_kills_the_process_and_propagates():
    class DiskFull(Exception):
        pass

    calls = []

    def guard():
        calls.append(1)
        raise DiskFull

    with pytest.raises(DiskFull):
        _subprocess.run_guarded(["sleep", "30"], guard=guard, guard_interval=0.05)
    assert calls == [1]


def test_run_guarded_timeout_raises_command_failed():
    from resurgam_worker.pbs_client.errors import PbsCommandFailedError

    with pytest.raises(PbsCommandFailedError) as exc_info:
        _subprocess.run_guarded(["sleep", "30"], timeout=0.1, guard_interval=0.05)
    assert exc_info.value.returncode == -1
