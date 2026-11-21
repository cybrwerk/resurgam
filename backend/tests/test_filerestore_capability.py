"""resurgam_api.filerestore.capability -- per backup type, per side, with actionable fixes."""

import json

from resurgam_worker.capability import checks as wchecks

from resurgam_api import host_capabilities
from resurgam_api.filerestore import capability

ALL = (
    wchecks.CHECK_PBS_CLIENT,
    wchecks.CHECK_FILE_RESTORE,
    wchecks.CHECK_RESTORE_IMAGE,
    wchecks.CHECK_QEMU,
    wchecks.CHECK_KVM,
    wchecks.CHECK_VHOST_VSOCK,
    wchecks.CHECK_RUN_DIR,
    wchecks.CHECK_LOG_DIR,
)


def _probe_file(tmp_path, failing=()):
    checks = [
        {"name": n, "status": "missing" if n in failing else "ok", "message": f"{n} {'gone' if n in failing else 'ok'}"}
        for n in ALL
    ]
    path = tmp_path / "host-capabilities.json"
    path.write_text(
        json.dumps(
            {
                "kvm": {"available": True, "detail": "ok"},
                "file_restore": {"checks": checks, "checked_at": "2026-10-07T10:00:00+00:00"},
                "checked_at": "2026-10-07T10:00:00+00:00",
            }
        )
    )
    return path


def _types(report):
    return {t.backup_type: t for t in report.backup_types}


def test_everything_present_on_the_host_makes_all_types_available(tmp_path):
    report = capability.check_capability(_probe_file(tmp_path))
    assert report.location == "appliance_host" and report.checked_at == "2026-10-07T10:00:00+00:00"
    assert all(c.location == "appliance_host" for c in report.checks)
    types = _types(report)
    assert all(t.available and t.missing == [] for t in types.values())
    assert "available on the appliance host" in types["vm"].message


def test_a_host_without_the_proxmox_packages_says_what_is_missing_where_and_how_to_fix_it(tmp_path):
    """the test appliance's state at 0.2.1: no Proxmox tools on the appliance host at all."""
    failing = (
        wchecks.CHECK_PBS_CLIENT,
        wchecks.CHECK_FILE_RESTORE,
        wchecks.CHECK_RESTORE_IMAGE,
        wchecks.CHECK_QEMU,
        wchecks.CHECK_RUN_DIR,
        wchecks.CHECK_LOG_DIR,
    )
    types = _types(capability.check_capability(_probe_file(tmp_path, failing)))
    vm, ct = types["vm"], types["ct"]
    assert not vm.available and not ct.available and not types["host"].available
    assert ct.missing == ["proxmox-backup-client"]
    assert vm.missing == [
        "proxmox-file-restore",
        "proxmox-backup-restore-image",
        "pve-qemu-kvm",
        "/run/proxmox-backup",
        "/var/log/proxmox-backup/file-restore",
    ]
    for entry in (vm, ct):
        assert "on the appliance host (where the file-restore and file-index workers run)" in entry.message
        assert "not part of the API container" in entry.message
        assert "update bundle" in entry.message and capability.HOST_SETUP_COMMAND in entry.message
    assert "proxmox-backup-client: proxmox-backup-client gone" in ct.message


def test_missing_kvm_only_blocks_vm_browsing_and_points_at_nested_virtualization(tmp_path):
    types = _types(capability.check_capability(_probe_file(tmp_path, (wchecks.CHECK_KVM,))))
    assert types["ct"].available and not types["vm"].available
    assert types["vm"].missing == ["/dev/kvm"]
    assert "nested virtualization" in types["vm"].message
    assert "update bundle" not in types["vm"].message


def test_a_probe_report_from_before_0_2_2_asks_for_a_probe_restart(tmp_path):
    path = tmp_path / "host-capabilities.json"
    path.write_text(json.dumps({"kvm": {"available": True, "detail": "ok"}, "checked_at": "x"}))
    report = capability.check_capability(path)
    assert all(not t.available for t in report.backup_types)
    assert all("systemctl restart resurgam-host-probe" in t.message for t in report.backup_types)


def test_a_missing_or_broken_probe_report_never_raises(tmp_path):
    missing = capability.check_capability(tmp_path / "nope.json")
    assert all("hasn't reported yet" in t.message for t in missing.backup_types)
    (tmp_path / "bad.json").write_text("{not json")
    broken = capability.check_capability(tmp_path / "bad.json")
    assert all(not t.available for t in broken.backup_types)


def test_outside_the_appliance_the_api_checks_its_own_process(monkeypatch):
    monkeypatch.setattr(wchecks.shutil, "which", lambda name: None)
    report = capability.check_capability(None)
    assert report.location == "local"
    ct = _types(report)["ct"]
    assert not ct.available and "on this machine" in ct.message and "ADR 0011" in ct.message


def test_the_host_probe_writes_the_file_restore_checks_the_api_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(wchecks.shutil, "which", lambda name: None)
    state = tmp_path / "host-capabilities.json"
    host_capabilities.write_state(state)
    doc = json.loads(state.read_text())
    assert [c["name"] for c in doc["file_restore"]["checks"]] == list(ALL)
    report = capability.check_capability(state)
    assert report.location == "appliance_host"
    assert _types(report)["ct"].missing == ["proxmox-backup-client"]


def test_run_host_checks_reports_writable_directories_and_restore_image(tmp_path):
    kernel, initramfs = tmp_path / "bzImage", tmp_path / "initramfs.img"
    kernel.write_bytes(b"k")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    report = wchecks.run_host_checks(
        kernel=str(kernel),
        initramfs=str(initramfs),
        run_dir=str(run_dir),
        log_dir=str(tmp_path / "no-log-dir"),
        kvm_device=str(tmp_path / "kvm"),
        vhost_vsock_device=str(tmp_path / "vsock"),
    )
    by = {c.name: c for c in report.checks}
    assert by[wchecks.CHECK_RESTORE_IMAGE].status.value == "missing"
    assert "initramfs.img" in by[wchecks.CHECK_RESTORE_IMAGE].message
    assert by[wchecks.CHECK_RUN_DIR].status.value == "ok"
    assert by[wchecks.CHECK_LOG_DIR].status.value == "missing"
    assert by[wchecks.CHECK_VHOST_VSOCK].status.value == "missing"
    run_dir.chmod(0o500)
    try:
        report = wchecks.run_host_checks(run_dir=str(run_dir))
        assert {c.name: c for c in report.checks}[wchecks.CHECK_RUN_DIR].message.endswith(
            "not writable by this process"
        )
    finally:
        run_dir.chmod(0o700)
