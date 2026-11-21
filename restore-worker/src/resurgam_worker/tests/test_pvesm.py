"""`pvesm.py` wraps `pvesm path` -- confirmed live (2026-09-22) against a
real PVE node: resolves a snippet-storage volume id to its real local
path even before the file exists (`resurgam-lab-agent` writes it there
itself -- see Recovery Lab Phase B, `lab_agent/daemon.py`).
"""

import subprocess

import pytest

from resurgam_worker.pve_client import pvesm
from resurgam_worker.pve_client.errors import PveBinaryNotFoundError, PveCommandFailedError


def _fake_completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _fake_which(monkeypatch):
    monkeypatch.setattr(pvesm.shutil, "which", lambda name: f"/usr/sbin/{name}")


def test_resolve_storage_path_returns_stripped_stdout(monkeypatch):
    captured = {}

    def fake_run(command):
        captured["command"] = command
        return _fake_completed(0, stdout="/var/lib/vz/snippets/resurgam-lab-abc123.yaml\n")

    monkeypatch.setattr(pvesm, "_run", fake_run)

    result = pvesm.resolve_storage_path("local:snippets/resurgam-lab-abc123.yaml")

    assert result == "/var/lib/vz/snippets/resurgam-lab-abc123.yaml"
    assert captured["command"] == ["/usr/sbin/pvesm", "path", "local:snippets/resurgam-lab-abc123.yaml"]


def test_resolve_storage_path_raises_on_failure(monkeypatch):
    monkeypatch.setattr(pvesm, "_run", lambda command: _fake_completed(1, stderr="unknown storage"))

    with pytest.raises(PveCommandFailedError):
        pvesm.resolve_storage_path("bogus:snippets/x.yaml")


def test_resolve_storage_path_raises_when_binary_missing(monkeypatch):
    monkeypatch.setattr(pvesm.shutil, "which", lambda name: None)

    with pytest.raises(PveBinaryNotFoundError):
        pvesm.resolve_storage_path("local:snippets/x.yaml")
