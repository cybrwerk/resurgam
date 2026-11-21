from datetime import datetime

from resurgam_api.models.restore import RestorePlanNetworkMode


def pbs_volid(pbs_storage_id: str, backup_type: str, backup_id: str, snapshot_time: datetime) -> str:
    """PVE's own volume-id notation for a PBS-backed backup archive:
    `<storage>:backup/<type>/<id>/<time>`, confirmed against real captured
    snapshot data (docs/milestone-1-pbs-pve-api-assumptions.md).
    """
    timestamp = snapshot_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{pbs_storage_id}:backup/{backup_type}/{backup_id}/{timestamp}"


_LINK_DOWN = "s/,?link_down=[01]//; s/$/,link_down=1/"


def _nic_loop(tool: str, vmid: int, network_mode: RestorePlanNetworkMode, network_bridge: str | None) -> str | None:
    """A shell loop over every NIC (`net0`, `net1`, ...) of the restored
    guest that rewrites each one in place with `sed` -- the command-line
    twin of `restore/execution.py`'s `network_updates` (same rules per
    mode). `None` for `original`."""
    if network_mode == RestorePlanNetworkMode.ORIGINAL:
        return None
    retarget = f"s/,?(bridge|tag)=[^,]*//g; s/,?link_down=[01]//; s/$/,bridge={network_bridge}"
    if network_mode == RestorePlanNetworkMode.ISOLATED and network_bridge:
        first = rest = retarget + ",link_down=1/"
    elif network_mode == RestorePlanNetworkMode.CUSTOM and network_bridge:
        first, rest = retarget + ",link_down=0/", _LINK_DOWN
    else:  # none, or isolated without an isolated bridge: every NIC stays put, unplugged
        first = rest = _LINK_DOWN
    value = f'"$({tool} config {vmid} | sed -n "s/^$n: //p" | sed -E "$e")"'
    return (
        f"e='{first}'; for n in $({tool} config {vmid} | sed -n 's/^\\(net[0-9][0-9]*\\):.*/\\1/p'); do "
        f"{tool} set {vmid} --$n {value}; e='{rest}'; done"
    )


def generate_restore_command(
    *,
    backup_type: str,
    backup_id: str,
    snapshot_time: datetime,
    pbs_storage_id: str,
    target_vmid: int,
    target_storage: str,
    vmid_intent_is_new: bool,
    network_mode: RestorePlanNetworkMode,
    network_bridge: str | None,
    start_after_restore: bool,
    temp_name: str,
) -> str:
    """Manifest §11.1: generate a reviewed, human-runnable `qmrestore`
    (VM) / `pct restore` (CT) command. This is the MVP's entire output —
    nothing here executes anything; every line is an instruction for a
    human operator to run by hand.

    `qmrestore` does not accept an inline network override — unlike `pct
    restore`, which does — so a VM restore that needs anything other than
    the snapshot's own network config gets a follow-up `qm set` line
    (standard, well-documented Proxmox VE CLI behavior; not re-verified
    against this specific node's man pages this session — see ADR 0012).

    `--name`/`--hostname temp_name` applies the plan's already-generated,
    already-conflict-checked name to the restored guest — without it, the
    guest keeps whatever name/hostname was baked into the backup archive,
    a real gap only found once EPIC 9 actually ran a restore (ADR 0014):
    `check_name_conflict` was checking a name that was never applied.

    "none" network mode *disconnects* the snapshot's own real NIC
    (`link_down=1`, read back via `qm config` since PVE has no
    partial-attribute update) rather than deleting it outright — deleting
    removes the device from the guest entirely, which is less reversible
    and can surprise a guest OS that expects the NIC to still be present,
    just unplugged (same reasoning as EPIC 9's live execution path,
    `restore/execution.py`'s `network_updates`). Every NIC (`net0`, `net1`,
    ...) is handled, not just `net0` -- found live 2026-11-21.
    """
    volid = pbs_volid(pbs_storage_id, backup_type, backup_id, snapshot_time)
    unique_flag = "--unique 1" if vmid_intent_is_new else "--force 1"
    lines: list[str] = []

    if backup_type == "vm":
        lines.append(f"qmrestore {volid} {target_vmid} --storage {target_storage} {unique_flag} --name {temp_name}")
        network_line = _nic_loop("qm", target_vmid, network_mode, network_bridge)
        if network_line:
            lines.append(network_line)
        # ORIGINAL: no follow-up line — the snapshot's own network config is kept as-is.
        lines.append(f"# VM {target_vmid} is left powered off.")
        if start_after_restore:
            lines.append(f"# To start it: qm start {target_vmid}")
    else:  # ct / host restored as a container
        lines.append(
            f"pct restore {target_vmid} {volid} --storage {target_storage} {unique_flag} --hostname {temp_name}"
        )
        network_line = _nic_loop("pct", target_vmid, network_mode, network_bridge)
        if network_line:
            lines.append(network_line)
        lines.append(f"# CT {target_vmid} is left stopped.")
        if start_after_restore:
            lines.append(f"# To start it: pct start {target_vmid}")

    return "\n".join(lines)
