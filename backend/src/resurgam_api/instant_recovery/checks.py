"""Pre-flight checks of an Instant Recovery plan (ADR 0030 §2/§4), in the same
`{check, status, message}` shape as Guided Restore's (`restore/checks.py`)."""

from __future__ import annotations

from datetime import datetime, timedelta

from resurgam_api.instant_recovery.original_vm import ABSENT, RUNNING, STOPPED, UNKNOWN, OriginalVmState
from resurgam_api.models.pve import PveCluster
from resurgam_api.models.restore import RestorePlanNetworkMode
from resurgam_api.restore.checks import CheckEntry, _entry

# The agent polls every few seconds and the backend stamps `lab_agent_last_seen_at`
# on each call; ten minutes of silence means it is not running.
AGENT_SEEN_WITHIN = timedelta(minutes=10)


def check_cluster_ready(cluster: PveCluster, target_node: str, *, now: datetime) -> list[CheckEntry]:
    entries: list[CheckEntry] = []
    if not cluster.instant_recovery_enabled:
        entries.append(
            _entry(
                "instant_recovery_enabled",
                "fail",
                "Instant Recovery is not allowed on this cluster. A platform admin can switch it on in the cluster's "
                "settings (Allow Instant Recovery).",
            )
        )
    else:
        entries.append(_entry("instant_recovery_enabled", "pass", "Instant Recovery is allowed on this cluster."))

    if not cluster.lab_agent_token_hash:
        entries.append(
            _entry(
                "lab_agent", "fail", "No resurgam-lab-agent is paired with this cluster; pair one on the target node."
            )
        )
    elif cluster.lab_agent_last_seen_at is None or cluster.lab_agent_last_seen_at < now - AGENT_SEEN_WITHIN:
        entries.append(
            _entry(
                "lab_agent",
                "fail",
                "The cluster's resurgam-lab-agent has not reported for over 10 minutes; it must run to start the VM.",
            )
        )
    else:
        entries.append(_entry("lab_agent", "pass", "The cluster's resurgam-lab-agent is reporting."))

    if cluster.lab_agent_node is None:
        entries.append(
            _entry(
                "agent_node",
                "warn",
                f"RESURGAM does not know which node the agent runs on; the VM can only start if it runs on "
                f"{target_node!r}.",
            )
        )
    elif cluster.lab_agent_node != target_node:
        entries.append(
            _entry(
                "agent_node",
                "fail",
                f"The agent runs on node {cluster.lab_agent_node!r}; an instantly recovered VM runs on the agent's node. "
                f"Pick {cluster.lab_agent_node!r} as target node.",
            )
        )
    else:
        entries.append(_entry("agent_node", "pass", f"The agent runs on {target_node!r}."))
    return entries


def check_original_vm(state: OriginalVmState, mode: RestorePlanNetworkMode) -> CheckEntry:
    """Refuses the original network while the original runs; asks for an
    acknowledgment (warn) when its state is unknown."""
    where = f" on {state.cluster_name} / {state.node}" if state.cluster_name else ""
    unreachable = ", ".join(state.unreachable_clusters)
    if mode == RestorePlanNetworkMode.ORIGINAL:
        if state.status == RUNNING:
            return _entry(
                "original_vm",
                "fail",
                f"The original VM is running{where}. Two copies of the same machine on the original network would "
                "clash (same IP, same MAC). Stop the original first, or choose another network mode.",
            )
        if state.status == UNKNOWN:
            return _entry(
                "original_vm",
                "warn",
                f"Could not check whether the original VM is running (unreachable: {unreachable}). Confirm it is "
                "not running before using the original network.",
            )
    if state.status == RUNNING:
        return _entry(
            "original_vm", "pass", f"The original VM is running{where}; the recovered VM gets new MAC addresses."
        )
    if state.status == UNKNOWN:
        return _entry(
            "original_vm",
            "pass",
            f"The original VM's state is unknown (unreachable: {unreachable}); the recovered VM gets new MAC addresses.",
        )
    if state.status == STOPPED:
        return _entry("original_vm", "pass", f"The original VM is stopped{where}; MAC addresses are kept.")
    if state.status == ABSENT:
        return _entry("original_vm", "pass", "The original VM does not exist any more; MAC addresses are kept.")
    return _entry("original_vm", "unknown", f"Unexpected original VM state {state.status!r}.")


def keep_mac(state: OriginalVmState) -> bool:
    return state.status in (STOPPED, ABSENT)


def describe_plan(*, backup_label: str, target_vmid: int, target_node: str, target_storage: str) -> str:
    """What a plan with `method=instant` will do -- stored where a full plan
    stores its generated `qmrestore` command."""
    return "\n".join(
        [
            f"# Instant Recovery of {backup_label} (ADR 0030) -- run by resurgam-lab-agent on {target_node}:",
            "#  1. map every disk of the backup (proxmox-backup-client map) and put a qcow2 overlay on each",
            f"#  2. create VM {target_vmid} from the backup's own config; EFI/TPM state imported to {target_storage}",
            f"#  3. start VM {target_vmid} -- it runs from the backup; writes go to the overlays",
            f"#  4. Move to storage (later, while it runs): PVE move_disk of each disk to {target_storage}",
            "#     or Discard: stop and delete the VM and its overlays. The backup itself is never written to.",
        ]
    )
