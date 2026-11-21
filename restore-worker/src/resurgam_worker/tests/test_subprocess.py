"""`_subprocess.run_subprocess` -- the run-with-timeout-and-wrap primitive
shared by `pbs_client._subprocess.run` and `pve_client.qm._run`, each
supplying its own domain error type and timeout (see both modules'
docstrings for why this was factored out here).
"""

import subprocess

import pytest

from resurgam_worker._subprocess import run_subprocess


class _FakeError(Exception):
    def __init__(self, command, returncode, stderr):
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{command} failed: {stderr}")


def test_run_subprocess_returns_completed_process(monkeypatch):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_subprocess(["echo", "hi"], timeout=5.0, on_timeout=_FakeError)

    assert result.returncode == 0
    assert result.stdout == "ok"


def test_run_subprocess_wraps_timeout_in_the_caller_supplied_error(monkeypatch):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(_FakeError) as exc_info:
        run_subprocess(["sleep", "999"], timeout=5.0, on_timeout=_FakeError)

    assert exc_info.value.command == ["sleep", "999"]
    assert exc_info.value.returncode == -1


def test_run_subprocess_passes_env_through(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    run_subprocess(["echo"], timeout=5.0, on_timeout=_FakeError, env={"FOO": "bar"})

    assert captured["env"] == {"FOO": "bar"}
