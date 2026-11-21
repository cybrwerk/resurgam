from __future__ import annotations

import os
import shutil
import subprocess

from resurgam_worker.capability.models import (
    CapabilityCheckResult,
    CapabilityReport,
    CapabilityStatus,
)

_VERSION_TIMEOUT_SECONDS = 5


def _check_binary_version(
    binary_name: str,
    *,
    probe_args: tuple[str, ...] = ("version",),
    reports_version: bool = True,
) -> CapabilityCheckResult:
    """Confirmed live (2026-09-14/15) against a real `proxmox-backup-client
    3.4.7`: this tool has no `--version` flag at all — it exits 255 with
    "no such command '--version'". Both PBS client tools instead take
    `version` as a positional subcommand (they share the same Rust CLI
    argument-parsing framework across the Proxmox Backup ecosystem), which
    this originally shipped as an unverified guess (`--version`) and has
    now been corrected. Without this fix, a real, working install was
    reported as `degraded`, not `ok`.

    `proxmox-file-restore` (confirmed live 2026-09-16 against a real
    install, package 4.2.5-1, on a fresh Debian 13 dev VM) goes one step
    further: it has no version-reporting command *at all* — neither
    `--version` nor a `version` subcommand exist ("no such command
    'version'"), unlike `proxmox-backup-client` above. Its capability
    check instead probes `help` (a real, always-present, side-effect-free
    subcommand) purely to confirm the binary runs; `reports_version=False`
    skips trying to surface a version string this tool simply can't
    produce via its own CLI, rather than reporting a real, working
    install as `degraded` for lacking something it was never asked to do.
    """
    binary_path = shutil.which(binary_name)
    if binary_path is None:
        return CapabilityCheckResult(
            name=binary_name,
            status=CapabilityStatus.MISSING,
            message=f"{binary_name} not found on PATH",
        )

    probe_label = " ".join(probe_args)
    try:
        result = subprocess.run(  # noqa: S603 - fixed binary, no user input, no shell
            [binary_path, *probe_args],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CapabilityCheckResult(
            name=binary_name,
            status=CapabilityStatus.DEGRADED,
            message=f"{binary_name} found but '{probe_label}' failed to run",
            detail=str(exc),
        )

    if result.returncode != 0:
        return CapabilityCheckResult(
            name=binary_name,
            status=CapabilityStatus.DEGRADED,
            message=f"{binary_name} {probe_label} exited with code {result.returncode}",
            detail=result.stderr.strip() or None,
        )

    if not reports_version:
        return CapabilityCheckResult(
            name=binary_name,
            status=CapabilityStatus.OK,
            message=f"{binary_name} available",
        )

    version_output = (result.stdout or result.stderr).strip()
    return CapabilityCheckResult(
        name=binary_name,
        status=CapabilityStatus.OK,
        message=version_output or f"{binary_name} available",
    )


def _check_device(path: str, *, required: bool) -> CapabilityCheckResult:
    name = path
    if not os.path.exists(path):
        return CapabilityCheckResult(
            name=name,
            status=CapabilityStatus.MISSING if required else CapabilityStatus.DEGRADED,
            message=f"{path} does not exist",
        )

    if not os.access(path, os.R_OK | os.W_OK):
        return CapabilityCheckResult(
            name=name,
            status=CapabilityStatus.DEGRADED,
            message=f"{path} exists but is not readable/writable by this process",
        )

    return CapabilityCheckResult(
        name=name,
        status=CapabilityStatus.OK,
        message=f"{path} available",
    )


def run_capability_check(
    *,
    pbs_client_binary: str = "proxmox-backup-client",
    file_restore_binary: str = "proxmox-file-restore",
    kvm_device: str = "/dev/kvm",
    fuse_device: str = "/dev/fuse",
) -> CapabilityReport:
    """Report this host's capability to run PBS file-restore operations.

    Never raises for a missing capability (manifest §18.12) — every check
    resolves to a status in the returned report instead.
    """
    checks = [
        _check_binary_version(pbs_client_binary),
        _check_binary_version(file_restore_binary, probe_args=("help",), reports_version=False),
        _check_device(kvm_device, required=True),
        _check_device(fuse_device, required=False),
    ]
    return CapabilityReport(checks=checks)


# --- the full host check set the file-restore/file-index workers depend on --------------------
#
# `run_capability_check()` above is the original four-check set. The appliance's host probe
# (resurgam_api.host_capabilities) and the API's per-backup-type capability report need a few
# more, all live-verified on a Debian 13 appliance VM (2026-10-07) as the exact preconditions for
# `proxmox-file-restore` to work *unprivileged* (as the workers' `resurgam-worker` identity):
#
# - the restore micro-VM's kernel (proxmox-backup-restore-image) and the initramfs the APT hook of
#   proxmox-backup-file-restore builds from it -- without them the tool bails with "package
#   'proxmox-backup-restore-image' is not (correctly) installed";
# - pve-qemu-kvm's qemu-system-x86_64, which boots that micro-VM;
# - /dev/vhost-vsock (the host<->micro-VM channel, root:kvm 0660 on Debian);
# - /run/proxmox-backup and /var/log/proxmox-backup/file-restore writable: the tool creates its
#   per-uid state dir and QMP socket in the former and the micro-VM log in the latter. On a stock
#   install both are root/backup-owned, which is the "EACCES unless root" ADR 0011 found --
#   host-setup.sh's tmpfiles.d entry makes them writable for the workers' group instead.

RESTORE_IMAGE_KERNEL = "/usr/lib/x86_64-linux-gnu/proxmox-backup/file-restore/bzImage"
RESTORE_IMAGE_INITRAMFS = "/var/cache/proxmox-backup/file-restore-initramfs.img"
QEMU_BINARY = "qemu-system-x86_64"
VHOST_VSOCK_DEVICE = "/dev/vhost-vsock"
FILE_RESTORE_RUN_DIR = "/run/proxmox-backup"
FILE_RESTORE_LOG_DIR = "/var/log/proxmox-backup/file-restore"

# Stable check names -- the API maps them to backup types and to operator-facing fixes.
CHECK_PBS_CLIENT = "proxmox-backup-client"
CHECK_FILE_RESTORE = "proxmox-file-restore"
CHECK_RESTORE_IMAGE = "proxmox-backup-restore-image"
CHECK_QEMU = "pve-qemu-kvm"
CHECK_KVM = "/dev/kvm"
CHECK_VHOST_VSOCK = VHOST_VSOCK_DEVICE
CHECK_RUN_DIR = FILE_RESTORE_RUN_DIR
CHECK_LOG_DIR = FILE_RESTORE_LOG_DIR


def _check_files(name: str, paths: tuple[str, ...], missing_hint: str) -> CapabilityCheckResult:
    absent = [p for p in paths if not os.path.isfile(p)]
    if absent:
        return CapabilityCheckResult(
            name=name, status=CapabilityStatus.MISSING, message=f"{', '.join(absent)} missing -- {missing_hint}"
        )
    return CapabilityCheckResult(name=name, status=CapabilityStatus.OK, message=f"{name} installed")


def _check_on_path(name: str, binary: str) -> CapabilityCheckResult:
    path = shutil.which(binary)
    if path is None:
        return CapabilityCheckResult(name=name, status=CapabilityStatus.MISSING, message=f"{binary} not found on PATH")
    return CapabilityCheckResult(name=name, status=CapabilityStatus.OK, message=f"{binary} available at {path}")


def _check_writable_dir(path: str) -> CapabilityCheckResult:
    """``os.access(W_OK)`` also reports a read-only mount (EROFS), so a systemd sandbox that
    forgot ``ReadWritePaths=`` shows up here exactly like a permission problem would."""
    if not os.path.isdir(path):
        return CapabilityCheckResult(name=path, status=CapabilityStatus.MISSING, message=f"{path} does not exist")
    if not os.access(path, os.W_OK | os.X_OK):
        return CapabilityCheckResult(
            name=path, status=CapabilityStatus.MISSING, message=f"{path} exists but is not writable by this process"
        )
    return CapabilityCheckResult(name=path, status=CapabilityStatus.OK, message=f"{path} writable")


def run_host_checks(
    *,
    pbs_client_binary: str = "proxmox-backup-client",
    file_restore_binary: str = "proxmox-file-restore",
    kernel: str = RESTORE_IMAGE_KERNEL,
    initramfs: str = RESTORE_IMAGE_INITRAMFS,
    qemu_binary: str = QEMU_BINARY,
    kvm_device: str = "/dev/kvm",
    vhost_vsock_device: str = VHOST_VSOCK_DEVICE,
    run_dir: str = FILE_RESTORE_RUN_DIR,
    log_dir: str = FILE_RESTORE_LOG_DIR,
) -> CapabilityReport:
    """Every precondition of the CT/host (catalog) and VM (micro-VM) file-restore paths, checked
    as the calling process. Never raises; names are the ``CHECK_*`` constants above."""
    pbs_client = _check_binary_version(pbs_client_binary)
    pbs_client.name = CHECK_PBS_CLIENT
    file_restore = _check_binary_version(file_restore_binary, probe_args=("help",), reports_version=False)
    file_restore.name = CHECK_FILE_RESTORE
    kvm = _check_device(kvm_device, required=True)
    kvm.name = CHECK_KVM
    vsock = _check_device(vhost_vsock_device, required=True)
    vsock.name = CHECK_VHOST_VSOCK
    if vsock.status == CapabilityStatus.DEGRADED:
        vsock.status = CapabilityStatus.MISSING  # unusable, not "partly working"
    if kvm.status == CapabilityStatus.DEGRADED:
        kvm.status = CapabilityStatus.MISSING
    run = _check_writable_dir(run_dir)
    run.name = CHECK_RUN_DIR
    log = _check_writable_dir(log_dir)
    log.name = CHECK_LOG_DIR
    return CapabilityReport(
        checks=[
            pbs_client,
            file_restore,
            _check_files(
                CHECK_RESTORE_IMAGE,
                (kernel, initramfs),
                "install proxmox-backup-restore-image (its APT hook builds the initramfs)",
            ),
            _check_on_path(CHECK_QEMU, qemu_binary),
            kvm,
            vsock,
            run,
            log,
        ]
    )
