"""The flags RESURGAM passes to the PBS CLI tools for folder/multi-selection ZIP downloads exist in
the real tools -- checked against `help` output captured from proxmox-backup-client /
proxmox-file-restore 4.2.8 (pve-no-subscription, Debian 13, 2026-10-08, the appliance's
host-packages set), stored verbatim in fixtures/."""

from pathlib import Path

import pytest

from resurgam_worker.pbs_client import catalog, file_restore

FIXTURES = Path(__file__).parent / "fixtures"


def _options(command: list[str]) -> set[str]:
    return {arg for arg in command if arg.startswith("--")}


@pytest.fixture(autouse=True)
def _fake_which(monkeypatch):
    monkeypatch.setattr(catalog.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(file_restore.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_restore_selection_flags_exist_in_proxmox_backup_client_restore_help(monkeypatch, tmp_path):
    from resurgam_worker.pbs_client._subprocess import GuardedResult

    captured = {}

    def fake(command, **kw):
        captured["command"] = command
        return GuardedResult(0, "")

    monkeypatch.setattr(catalog, "_run_guarded", fake)
    catalog.restore_selection("r", "ct/1/t", "root.pxar", str(tmp_path), ["/a", "/b"], namespace="ns", keyfile="K")
    help_text = (FIXTURES / "backup_client_restore_help.txt").read_text()
    assert "Usage: restore <snapshot> <archive-name> <target>" in help_text
    for option in _options(captured["command"]):
        assert f" {option} " in help_text or f" {option}\n" in help_text, option


def test_extract_to_file_flags_exist_in_proxmox_file_restore_extract_help(monkeypatch, tmp_path):
    from resurgam_worker.pbs_client._subprocess import GuardedResult

    captured = {}

    def fake(command, **kw):
        captured["command"] = command
        return GuardedResult(0, "")

    monkeypatch.setattr(file_restore, "_run_guarded", fake)
    file_restore.extract_to_file("r", "vm/1/t", "/d.img.fidx/part/1/x", str(tmp_path / "o"), as_zip=True, namespace="n")
    help_text = (FIXTURES / "file_restore_extract_help.txt").read_text()
    assert "Use '-' to write to standard output." in help_text
    assert "--format   plain|pxar|zip|tar" in help_text
    for option in _options(captured["command"]):
        assert f" {option} " in help_text, option
