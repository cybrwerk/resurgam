from __future__ import annotations

import os
from dataclasses import dataclass


class MissingConfigError(Exception):
    def __init__(self, var_name: str) -> None:
        self.var_name = var_name
        super().__init__(f"required environment variable {var_name} is not set")


@dataclass(frozen=True)
class LabAgentConfig:
    """`resurgam-lab-agent`'s own local config (ADR 0019) -- env-var
    driven, matching every other worker entrypoint in this project
    (`backend/src/resurgam_api/worker/*`'s `Settings`-via-`.env`
    convention), read once at startup, not from the RESURGAM backend.

    `pbs_token` is deliberately local-only: the backend's job payload
    (`GET /lab-agent/jobs`) carries PBS *coordinates* (host/port/
    datastore/namespace/snapshot), never credentials -- no secret is
    ever distributed over the agent's own HTTPS backchannel, matching
    `restore-worker`'s existing local-config precedent instead of
    inventing per-job credential passing.
    """

    backend_url: str
    agent_token: str
    # This node's PBS token. Empty after a pairing without one (ADR 0027):
    # the agent then still runs setup jobs (template builds), but no job
    # that maps a backup (lab runs, ephemeral indexing, disk mounts).
    pbs_token: str = ""
    # The appliance's TLS CA (Caddy's internal CA, written by the pairing
    # bootstrap); None = the system trust store.
    ca_file: str | None = None
    # A PVE "dir" storage, already registered on this node
    # (`pvesm add dir <id> --path <overlay_dir> --content images`) --
    # confirmed live (2026-09-19) that `qm`'s `scsi0` rejects a bare
    # filesystem path outright ("unable to associate path ... to any
    # storage"); the overlay file must be referenced as
    # `<overlay_storage_id>:<vmid>/vm-<vmid>-disk-0.qcow2`, which is
    # only valid once PVE knows about the storage. A required, operator-
    # provisioned prerequisite (matching `lab_appliance_template_vmid`'s
    # own operator-setup requirement), not something this agent
    # provisions for itself -- storage management is a broader privilege
    # than this agent otherwise needs.
    overlay_storage_id: str = "resurgam-lab-overlays"
    overlay_dir: str = "/var/lib/resurgam-lab-agent/overlays"
    poll_interval_seconds: float = 5.0
    vm_memory_mb: int = 2048
    vm_cores: int = 2
    # A real gap found live (2026-09-24): `wait_for_guest_agent`'s own
    # 180s default (chosen before any real test) was nowhere near enough
    # for a real Windows guest booting from a zero-copy PBS-mapped
    # device -- confirmed live via `info blockstats` showing continuous,
    # substantial disk I/O (not a hung VM) well past that point. Real
    # boot time depends heavily on how fast blocks stream from PBS, so
    # this stays operator-tunable rather than a bigger hardcoded guess.
    guest_exec_agent_wait_timeout_seconds: float = 600.0
    # Disk Mounts (ADR 0025): this node's PVE name (default: the short host
    # name, which is what PVE uses as the node name) -- a mount job for another
    # node is refused, the mapped disks are local to this one.
    node_name: str | None = None
    # How long a freshly cloned share helper may take to boot, mount the disks
    # and write its result file.
    disk_mount_ready_timeout_seconds: float = 600.0


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise MissingConfigError(name)
    return value


def load_config() -> LabAgentConfig:
    return LabAgentConfig(
        backend_url=_require("RESURGAM_BACKEND_URL").rstrip("/"),
        agent_token=_require("RESURGAM_LAB_AGENT_TOKEN"),
        pbs_token=os.environ.get("RESURGAM_LAB_AGENT_PBS_TOKEN", "").strip(),
        ca_file=os.environ.get("RESURGAM_LAB_AGENT_CA_FILE") or None,
        overlay_storage_id=os.environ.get("RESURGAM_LAB_AGENT_OVERLAY_STORAGE_ID", "resurgam-lab-overlays"),
        overlay_dir=os.environ.get("RESURGAM_LAB_AGENT_OVERLAY_DIR", "/var/lib/resurgam-lab-agent/overlays"),
        poll_interval_seconds=float(os.environ.get("RESURGAM_LAB_AGENT_POLL_INTERVAL_SECONDS", "5")),
        vm_memory_mb=int(os.environ.get("RESURGAM_LAB_AGENT_VM_MEMORY_MB", "2048")),
        vm_cores=int(os.environ.get("RESURGAM_LAB_AGENT_VM_CORES", "2")),
        guest_exec_agent_wait_timeout_seconds=float(
            os.environ.get("RESURGAM_LAB_AGENT_GUEST_EXEC_AGENT_WAIT_TIMEOUT_SECONDS", "600")
        ),
        node_name=os.environ.get("RESURGAM_LAB_AGENT_NODE_NAME") or None,
        disk_mount_ready_timeout_seconds=float(
            os.environ.get("RESURGAM_LAB_AGENT_DISK_MOUNT_READY_TIMEOUT_SECONDS", "600")
        ),
    )
