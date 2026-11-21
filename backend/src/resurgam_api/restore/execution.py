"""EPIC 9 (manifest §11.2): pure orchestration logic for automated restore
execution -- no PVE I/O here, matching `checks.py`/`command.py`'s style
(fully unit-testable, no mocking of httpx needed). The API layer
(`api/v1/restore_execution.py`) calls the real `connectivity.pve_api`
functions and uses these to decide what to call and when.
"""

import re
from datetime import datetime

from resurgam_api.connectivity.models import PveTaskStatus
from resurgam_api.models.restore import RestorePlanExecutionStatus, RestorePlanNetworkMode

_NET_KEY = re.compile(r"^net(\d+)$")


def with_link_down(net_config_value: str, link_down: bool) -> str:
    """Set (or clear) `link_down=` on an existing PVE NIC config string
    (e.g. `virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0`), preserving every other
    sub-value untouched. PVE's config API has no partial-attribute
    update -- changing one sub-value of `net0` means resending the whole
    string -- so this is how "none" mode *disconnects* the snapshot's
    real, original NIC instead of deleting it outright.
    """
    parts = [p for p in net_config_value.split(",") if not p.startswith("link_down=")]
    parts.append(f"link_down={1 if link_down else 0}")
    return ",".join(parts)


def build_restore_params(
    *,
    backup_type: str,
    target_vmid: int,
    volid: str,
    target_storage: str,
    vmid_intent_is_new: bool,
    network_mode: RestorePlanNetworkMode,
    network_bridge: str | None,
    temp_name: str,
) -> dict:
    """kwargs for `pve_api.start_vm_restore` / `start_ct_restore` (the
    caller picks which function to call based on `backup_type`). For a
    VM, network is never included here -- `qmrestore`'s REST equivalent
    has no inline override, so it's always a `needs_network_finalize`
    follow-up call instead (see below). For a CT, `net0` is included
    inline only when `CT_RESTORE_SUPPORTS_INLINE_NET0` is true.

    `temp_name` is applied as `name` (VM) or `hostname` (CT) -- a real gap
    found live (2026-09-17): without it, the restored guest keeps
    whatever name/hostname was baked into the backup archive, not the
    `temp_name` this plan already generated and checked for conflicts
    against (`restore/checks.py`'s `check_name_conflict`), which is
    otherwise checking a name that's never actually applied.
    """
    params: dict = {
        "vmid": target_vmid,
        "archive": volid,
        "storage": target_storage,
        "unique": vmid_intent_is_new,
        "force": not vmid_intent_is_new,
    }
    if backup_type == "ct":
        params["hostname"] = temp_name
    else:
        params["name"] = temp_name
    return params


def _retarget(net_config_value: str, bridge: str) -> str:
    """Moves a NIC config string onto `bridge`, dropping its VLAN `tag=`
    (the original VLAN rarely exists on the chosen bridge) and keeping the
    model, MAC and every other sub-value."""
    parts = [p for p in net_config_value.split(",") if p and not p.startswith(("bridge=", "tag="))]
    return ",".join([*parts, f"bridge={bridge}"])


def network_updates(
    network_mode: RestorePlanNetworkMode, network_bridge: str | None, current_config: dict
) -> dict[str, str]:
    """The new value of every NIC (`net0`, `net1`, ...) of a just-restored
    guest, from its real restored config -- VM and CT alike, model, MAC and
    name kept:

    - `original`: nothing changes.
    - `none`, or `isolated` without an isolated bridge: every NIC stays where
      it is (bridge, VLAN) with `link_down=1`, so nothing reaches a network
      until someone deliberately sets the link up.
    - `isolated` with a bridge: every NIC moves to that bridge, VLAN tag
      dropped, `link_down=1`.
    - `custom`: the first NIC moves to the chosen bridge (link up), every
      other NIC gets `link_down=1`.
    """
    if network_mode == RestorePlanNetworkMode.ORIGINAL:
        return {}
    nets = sorted(
        (k for k in current_config if _NET_KEY.match(k) and isinstance(current_config[k], str)),
        key=lambda k: int(_NET_KEY.match(k).group(1)),
    )
    updates: dict[str, str] = {}
    for index, key in enumerate(nets):
        value = current_config[key]
        if network_mode == RestorePlanNetworkMode.ISOLATED and network_bridge:
            updates[key] = with_link_down(_retarget(value, network_bridge), True)
        elif network_mode == RestorePlanNetworkMode.CUSTOM and network_bridge and index == 0:
            updates[key] = with_link_down(_retarget(value, network_bridge), False)
        else:
            updates[key] = with_link_down(value, True)
    return updates


def needs_network_finalize(backup_type: str, network_mode: RestorePlanNetworkMode) -> bool:
    """Whether the post-restore network step (`network_updates`) runs: for
    any non-`original` mode, VM and CT alike -- the restore calls take no
    network override that covers every NIC."""
    return network_mode != RestorePlanNetworkMode.ORIGINAL


def needs_start_call(start_after_restore: bool) -> bool:
    """Manifest §11.2 keeps the guest powered off by default -- a start
    call only ever happens when the operator explicitly opted in.
    """
    return start_after_restore


def next_execution_status(current_status: str, task_status: PveTaskStatus) -> str:
    """The poll endpoint's state transition: given the plan's current
    `execution_status` and PVE's live task status, what should the new
    status be? Only transitions out of the "waiting on PVE" states
    (`restore_requested`/`restore_running`) -- any other current status is
    returned unchanged, since polling a plan that isn't mid-restore (e.g.
    already `completed`, or a fresh `pending_approval`) has nothing to do.
    """
    if current_status not in (
        RestorePlanExecutionStatus.RESTORE_REQUESTED.value,
        RestorePlanExecutionStatus.RESTORE_RUNNING.value,
    ):
        return current_status

    if task_status.status == "running":
        return RestorePlanExecutionStatus.RESTORE_RUNNING.value

    if task_status.status == "stopped":
        if task_status.exitstatus == "OK":
            return RestorePlanExecutionStatus.FINALIZING.value
        return RestorePlanExecutionStatus.RESTORE_FAILED.value

    # Unexpected/unknown PVE status shape -- stay put rather than guess.
    return current_status


def is_execution_stale(last_known_good: datetime, now: datetime, max_age_seconds: int) -> bool:
    """Whether it's been longer than `max_age_seconds` since we last had
    *any* successful contact with PVE about this execution
    (`last_known_good` -- `execution_last_confirmed_at` if ever set, else
    `executed_at`). Deliberately **not** used to override a live, reachable
    answer from PVE: a restore PVE is still reporting as genuinely running
    is never force-failed just because time has passed -- only a
    *prolonged inability to reach PVE at all* is treated as stuck. With no
    separate worker/scheduler for this feature (unlike the file-restore
    job queue's TTL sweep, ADR 0013), this is checked inline on each poll,
    not by a background sweep, so a plan nobody ever polls again still
    won't self-heal; an accepted limitation of the pull-only design.
    """
    return (now - last_known_good).total_seconds() > max_age_seconds
