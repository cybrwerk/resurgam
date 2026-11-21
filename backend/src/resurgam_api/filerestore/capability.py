"""File-restore capability, per backup type and per side (appliance host vs. this process).

On the appliance every PBS CLI call -- CT/host catalog browsing (`proxmox-backup-client`) and VM
disk browsing (`proxmox-file-restore`'s micro-VM) -- runs in the native file-restore/file-index
workers (ADR 0013), never in the API container. So the API must not check its own container: it
reads the report `resurgam-host-probe.service` writes natively, as the workers' identity
(`host_capabilities.write_state`). Outside the appliance (`HOST_CAPABILITIES_FILE` unset, local
development) API and workers share one machine, so a live check of this process is the answer.

The tool set and the "must come from one Proxmox repo release" rule are ADR 0011; how the
appliance gets them (provisioning, update bundles' host-setup step) is ADR 0024.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from resurgam_worker.capability import checks as wchecks
from resurgam_worker.capability.models import CapabilityCheckResult

from resurgam_api.schemas.filerestore import BackupTypeCapabilityOut, CapabilityCheckOut, CapabilityReportOut

APPLIANCE_HOST = "appliance_host"
LOCAL = "local"

CT_HOST_REQUIRES = (wchecks.CHECK_PBS_CLIENT,)
VM_REQUIRES = (
    wchecks.CHECK_FILE_RESTORE,
    wchecks.CHECK_RESTORE_IMAGE,
    wchecks.CHECK_QEMU,
    wchecks.CHECK_KVM,
    wchecks.CHECK_VHOST_VSOCK,
    wchecks.CHECK_RUN_DIR,
    wchecks.CHECK_LOG_DIR,
)
_PACKAGE_CHECKS = {
    wchecks.CHECK_PBS_CLIENT,
    wchecks.CHECK_FILE_RESTORE,
    wchecks.CHECK_RESTORE_IMAGE,
    wchecks.CHECK_QEMU,
    wchecks.CHECK_RUN_DIR,
    wchecks.CHECK_LOG_DIR,
}

HOST_SETUP_COMMAND = "sudo sh /opt/resurgam/current/appliance/release/host-setup.sh --online"
APPLIANCE_PACKAGE_FIX = (
    "Install the Proxmox file-restore tools on the appliance host (they are not part of the API container): "
    "apply the latest RESURGAM update bundle -- its host-setup step installs them offline from the bundle -- or, "
    f"with internet access to download.proxmox.com, run '{HOST_SETUP_COMMAND}' on the appliance console."
)
_LOCAL_PACKAGE_FIX = (
    "Install proxmox-backup-client, proxmox-backup-file-restore, proxmox-backup-restore-image and pve-qemu-kvm on "
    "this machine, all four from the same Proxmox VE repository release (Debian 13: pve-no-subscription) -- see "
    "ADR 0011 -- and make /run/proxmox-backup and /var/log/proxmox-backup/file-restore writable for the worker "
    "(appliance/release/host-setup.sh shows how)."
)
_KVM_FIX = (
    "Enable nested virtualization: nested VT-x/AMD-V on the hypervisor host and CPU type 'host' for this VM "
    "(manifest §5.2); the workers' user also needs the 'kvm' group."
)
_VSOCK_FIX = "Load the vhost_vsock kernel module (modprobe vhost_vsock) and check /dev/vhost-vsock is root:kvm 0660."


def _fix_for(missing: list[str], location: str) -> str:
    fixes = []
    if any(name in _PACKAGE_CHECKS for name in missing):
        fixes.append(APPLIANCE_PACKAGE_FIX if location == APPLIANCE_HOST else _LOCAL_PACKAGE_FIX)
    if wchecks.CHECK_KVM in missing:
        fixes.append(_KVM_FIX)
    if wchecks.CHECK_VHOST_VSOCK in missing:
        fixes.append(_VSOCK_FIX)
    return " ".join(fixes)


def _side(location: str) -> str:
    return (
        "on the appliance host (where the file-restore and file-index workers run)"
        if location == APPLIANCE_HOST
        else "on this machine"
    )


def _backup_type(
    backup_type: str, label: str, required: tuple[str, ...], by_name: dict[str, CapabilityCheckOut], location: str
) -> BackupTypeCapabilityOut:
    missing = [name for name in required if name not in by_name or by_name[name].status != "ok"]
    if not missing:
        message = f"{label} file browsing and download are available {_side(location)}."
    else:
        details = "; ".join(f"{name}: {by_name[name].message}" if name in by_name else name for name in missing)
        message = f"{label} file browsing is unavailable -- missing {_side(location)}: {details}. " + _fix_for(
            missing, location
        )
    return BackupTypeCapabilityOut(
        backup_type=backup_type, available=not missing, location=location, missing=missing, message=message
    )


def build_report(checks: list[CapabilityCheckOut], location: str, checked_at: str | None) -> CapabilityReportOut:
    by_name = {c.name: c for c in checks}
    return CapabilityReportOut(
        checks=checks,
        backup_types=[
            _backup_type("vm", "VM", VM_REQUIRES, by_name, location),
            _backup_type("ct", "Container (CT)", CT_HOST_REQUIRES, by_name, location),
            _backup_type("host", "Host", CT_HOST_REQUIRES, by_name, location),
        ],
        location=location,
        checked_at=checked_at,
    )


def _to_out(result: CapabilityCheckResult, location: str) -> CapabilityCheckOut:
    return CapabilityCheckOut(name=result.name, status=result.status.value, message=result.message, location=location)


def serialize_host_checks() -> list[dict[str, Any]]:
    """What the host probe stores (``host-capabilities.json`` -> ``file_restore.checks``)."""
    return [c.model_dump(mode="json") for c in wchecks.run_host_checks().checks]


def _unreported(location: str, reason: str) -> CapabilityReportOut:
    names = (wchecks.CHECK_PBS_CLIENT, *VM_REQUIRES)
    checks = [CapabilityCheckOut(name=n, status="missing", message=reason, location=location) for n in names]
    types = [
        BackupTypeCapabilityOut(
            backup_type=backup_type,
            available=False,
            location=location,
            missing=list(required),
            message=f"{label} file browsing status is unknown: {reason}",
        )
        for backup_type, label, required in (
            ("vm", "VM", VM_REQUIRES),
            ("ct", "Container (CT)", CT_HOST_REQUIRES),
            ("host", "Host", CT_HOST_REQUIRES),
        )
    ]
    return CapabilityReportOut(checks=checks, backup_types=types, location=location, checked_at=None)


def report_from_state_file(state_file: Path) -> CapabilityReportOut:
    try:
        data = json.loads(state_file.read_text())
    except FileNotFoundError:
        return _unreported(
            APPLIANCE_HOST,
            f"the host capability probe hasn't reported yet ({state_file} is missing); check "
            "'systemctl status resurgam-host-probe' on the appliance.",
        )
    except (OSError, ValueError):
        return _unreported(APPLIANCE_HOST, f"the host capability report at {state_file} is unreadable.")
    section = data.get("file_restore") if isinstance(data, dict) else None
    raw = section.get("checks") if isinstance(section, dict) else None
    if not isinstance(raw, list):
        # A probe from a release before 0.2.2 only reported /dev/kvm.
        return _unreported(
            APPLIANCE_HOST,
            "the host capability probe that last ran predates file-restore tool reporting; run "
            "'sudo systemctl restart resurgam-host-probe' on the appliance (or apply the next update, which "
            "restarts it).",
        )
    checks = []
    for item in raw:
        try:
            result = CapabilityCheckResult.model_validate(item)
        except ValueError:
            continue
        checks.append(_to_out(result, APPLIANCE_HOST))
    checked_at = section.get("checked_at") or data.get("checked_at")
    return build_report(checks, APPLIANCE_HOST, checked_at if isinstance(checked_at, str) else None)


def check_capability(state_file: Path | None) -> CapabilityReportOut:
    """Never raises. ``state_file`` = Settings.host_capabilities_file (None outside the appliance)."""
    if state_file is not None:
        return report_from_state_file(state_file)
    return build_report([_to_out(c, LOCAL) for c in wchecks.run_host_checks().checks], LOCAL, None)
