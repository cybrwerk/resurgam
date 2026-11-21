import subprocess

from resurgam_worker.capability import checks
from resurgam_worker.capability.models import CapabilityStatus


def test_missing_kvm_reports_missing_not_exception(tmp_path):
    report = checks.run_capability_check(
        pbs_client_binary="definitely-not-a-real-binary",
        file_restore_binary="also-not-a-real-binary",
        kvm_device=str(tmp_path / "no-such-kvm"),
        fuse_device=str(tmp_path / "no-such-fuse"),
    )

    kvm_check = report.get(str(tmp_path / "no-such-kvm"))
    assert kvm_check is not None
    assert kvm_check.status == CapabilityStatus.MISSING
    assert report.overall_status == CapabilityStatus.MISSING


def test_missing_optional_fuse_is_degraded_not_missing(tmp_path):
    kvm_path = tmp_path / "kvm"
    kvm_path.write_text("")

    report = checks.run_capability_check(
        pbs_client_binary="definitely-not-a-real-binary",
        file_restore_binary="also-not-a-real-binary",
        kvm_device=str(kvm_path),
        fuse_device=str(tmp_path / "no-such-fuse"),
    )

    fuse_check = report.get(str(tmp_path / "no-such-fuse"))
    assert fuse_check.status == CapabilityStatus.DEGRADED


def test_present_readable_device_is_ok(tmp_path):
    device_path = tmp_path / "kvm"
    device_path.write_text("")

    report = checks.run_capability_check(
        pbs_client_binary="definitely-not-a-real-binary",
        file_restore_binary="also-not-a-real-binary",
        kvm_device=str(device_path),
        fuse_device=str(tmp_path / "no-such-fuse"),
    )

    kvm_check = report.get(str(device_path))
    assert kvm_check.status == CapabilityStatus.OK


def test_binary_version_check_ok(monkeypatch):
    monkeypatch.setattr(checks.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        checks.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=a[0], returncode=0, stdout="proxmox-backup-client 4.2.1\n", stderr=""
        ),
    )

    result = checks._check_binary_version("proxmox-backup-client")

    assert result.status == CapabilityStatus.OK
    assert "4.2.1" in result.message


def test_binary_version_check_uses_positional_version_subcommand(monkeypatch):
    """Confirmed live (2026-09-14/15) against a real `proxmox-backup-client
    3.4.7`: `--version` doesn't exist for this tool at all (exits 255,
    "no such command '--version'") — it must be invoked as `version`
    (positional subcommand). A real install was reported `degraded`
    instead of `ok` before this was caught and fixed.
    """
    monkeypatch.setattr(checks.shutil, "which", lambda name: f"/usr/bin/{name}")
    captured = {}

    def fake_run(command, **kw):
        captured["command"] = command
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout="proxmox-backup-client 3.4.7\n", stderr=""
        )

    monkeypatch.setattr(checks.subprocess, "run", fake_run)
    checks._check_binary_version("proxmox-backup-client")

    assert captured["command"] == ["/usr/bin/proxmox-backup-client", "version"]


def test_file_restore_probe_uses_help_not_version(monkeypatch):
    """Confirmed live (2026-09-16) against a real `proxmox-file-restore
    4.2.5-1` on a fresh Debian 13 install: unlike `proxmox-backup-client`,
    this tool has no version-reporting command at all — neither
    `--version` nor a `version` subcommand exist ("no such command
    'version'", exit 255). A real, working install was reported
    `degraded` before this was caught and fixed; the check now probes
    `help` (a real, side-effect-free subcommand) instead, and doesn't
    try to extract a version string that doesn't exist.
    """
    monkeypatch.setattr(checks.shutil, "which", lambda name: f"/usr/bin/{name}")
    captured = {}

    def fake_run(command, **kw):
        captured["command"] = command
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="Usage:\n\nextract ...\n", stderr="")

    monkeypatch.setattr(checks.subprocess, "run", fake_run)
    result = checks._check_binary_version("proxmox-file-restore", probe_args=("help",), reports_version=False)

    assert captured["command"] == ["/usr/bin/proxmox-file-restore", "help"]
    assert result.status == CapabilityStatus.OK
    assert result.message == "proxmox-file-restore available"


def test_binary_version_check_degraded_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(checks.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        checks.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(args=a[0], returncode=1, stdout="", stderr="boom"),
    )

    result = checks._check_binary_version("proxmox-backup-client")

    assert result.status == CapabilityStatus.DEGRADED
    assert result.detail == "boom"


def test_overall_status_ok_when_all_checks_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(checks.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        checks.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(args=a[0], returncode=0, stdout="1.0.0\n", stderr=""),
    )
    kvm_path = tmp_path / "kvm"
    kvm_path.write_text("")
    fuse_path = tmp_path / "fuse"
    fuse_path.write_text("")

    report = checks.run_capability_check(kvm_device=str(kvm_path), fuse_device=str(fuse_path))

    assert report.overall_status == CapabilityStatus.OK
