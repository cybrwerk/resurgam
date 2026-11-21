"""Host capability reporting across the appliance's native/container split.

The appliance runs the API in a container but the privileged workers
(file-restore/file-index, the ones that actually need /dev/kvm for
proxmox-file-restore's QEMU micro-VM, ADR 0011) natively under systemd.
The API process checking /dev/kvm inside its own container answers the
wrong question -- the container never gets the device, so the setup
wizard warned about missing nested virtualization on a host that had it.

So the check runs where the answer matters: `resurgam-host-probe.service`
runs `probe_kvm()` natively, under the exact identity the workers use
(User=resurgam-worker, SupplementaryGroups=kvm), and writes the result to
a state file in a directory Compose bind-mounts read-only into the API
container. The API only reads that file (`read_kvm_status`). Outside the
appliance (local dev, HOST_CAPABILITIES_FILE unset) the API and workers
share one host, so checking its own process is still the right answer.

Since 0.2.2 the probe also reports every file-restore precondition (tools, devices, writable
directories -- `resurgam_worker.capability.checks.run_host_checks`), for the same reason: the
API container has none of the tools and never runs them; the workers on the host do.
"""

import getpass
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

KVM_DEVICE = Path("/dev/kvm")
DEFAULT_STATE_FILE = Path("/run/resurgam/host-capabilities.json")


@dataclass(frozen=True)
class KvmStatus:
    available: bool
    detail: str


def probe_kvm(device: Path = KVM_DEVICE) -> KvmStatus:
    if not device.exists():
        return KvmStatus(
            False,
            f"{device} does not exist. Nested virtualization isn't enabled for this VM -- VM-path file "
            "restores (proxmox-file-restore's internal QEMU micro-VM) will be unavailable. Enable "
            "nested Intel VT-x/AMD-V on the PVE host and set this VM's CPU type to 'host' (manifest §5.2).",
        )
    if not os.access(device, os.R_OK | os.W_OK):
        return KvmStatus(
            False,
            f"{device} exists but isn't accessible to user {getpass.getuser()!r} -- check its device "
            "permissions/group membership (the workers need the 'kvm' group).",
        )
    return KvmStatus(True, f"{device} is present and accessible.")


def write_state(path: Path = DEFAULT_STATE_FILE) -> KvmStatus:
    # Imported here so the KVM-only readers above stay importable without restore-worker.
    from resurgam_api.filerestore.capability import serialize_host_checks

    status = probe_kvm()
    checked_at = datetime.now(UTC).isoformat()
    payload = {
        "kvm": asdict(status),
        # The file-restore tools/devices/directories as the workers see them -- the API reports
        # per backup type from this (resurgam_api.filerestore.capability). Added in 0.2.2; a
        # report without it comes from an older probe.
        "file_restore": {"checks": serialize_host_checks(), "checked_at": checked_at},
        "checked_at": checked_at,
    }
    # Write-then-rename inside the same directory: the API container must
    # never read a half-written file, and the directory itself must never
    # be replaced -- the container's bind mount is pinned to its inode.
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.chmod(0o644)
    tmp.replace(path)
    return status


def read_kvm_status(state_file: Path | None) -> KvmStatus:
    if state_file is None:
        return probe_kvm()
    try:
        data = json.loads(state_file.read_text())
        kvm = data["kvm"]
        return KvmStatus(bool(kvm["available"]), str(kvm["detail"]))
    except FileNotFoundError:
        return KvmStatus(
            False,
            f"The host capability probe hasn't reported yet ({state_file} is missing) -- check "
            "'systemctl status resurgam-host-probe' on the appliance.",
        )
    except (OSError, ValueError, KeyError, TypeError):
        return KvmStatus(False, f"The host capability report at {state_file} is unreadable or malformed.")


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_STATE_FILE
    status = write_state(path)
    print(f"kvm_available={status.available}: {status.detail}")
    for check in json.loads(path.read_text())["file_restore"]["checks"]:
        print(f"{check['name']}: {check['status']} -- {check['message']}")
