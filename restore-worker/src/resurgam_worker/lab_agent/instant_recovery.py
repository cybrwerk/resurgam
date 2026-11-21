"""Instant Recovery on the target node (ADR 0030): run a VM straight from its PBS
backup, keep it alive while it runs from the backup, and clean up once its disks
were moved to real storage (or the operator discards it).

Start, per claimed plan:
1. every data disk archive is mapped (`proxmox-backup-client map`) inside its own
   transient systemd scope, so the mapping survives an agent restart;
2. a qcow2 overlay on each mapped device takes the writes;
3. the VM is created from the backup's own `qemu-server.conf`, disks pointing at
   the overlays, network per the plan;
4. `efidisk0` / `tpmstate0` are restored to a file and imported to the target
   storage before the first start (PVE can't move TPM state while the VM runs,
   and `qm disk import` needs a regular file);
5. the VM starts.

Every resource is reported to the backend the moment it exists, so a discard
after any crash knows what to remove. A failing start undoes what it created.

Moving the disks is not done here: the backend calls PVE's `move_disk` API per
disk (a PVE task that survives this process). This module only unmaps and
removes the overlay directory afterwards (`finish`).
"""

from __future__ import annotations

import logging
import os
import random
import re
import shutil
import subprocess
import time
from collections.abc import Callable

from resurgam_worker.lab_agent.client import (
    InstantRecoveryClaim,
    InstantRecoveryJob,
    LabAgentApiError,
    LabAgentClient,
)
from resurgam_worker.lab_agent.config import LabAgentConfig
from resurgam_worker.lab_agent.disk_mount import local_node_name
from resurgam_worker.pbs_client.errors import PbsClientError
from resurgam_worker.pbs_client.map import (
    fetch_original_vm_config,
    list_snapshot_archives,
    map_snapshot,
    restore_image,
    unmap,
)
from resurgam_worker.pve_client.errors import PveClientError
from resurgam_worker.pve_client.qm import (
    create_cow_overlay,
    create_vm,
    delete_vm_options,
    destroy_vm,
    get_vm_config,
    import_disk,
    set_vm_options,
    start_vm,
    stop_vm,
    vm_status,
)

logger = logging.getLogger("resurgam_worker.lab_agent.instant_recovery")

TAG = "resurgam-instant-recovery"
HEARTBEAT_INTERVAL_SECONDS = 60.0
# A mapping is probed with one direct 4 KiB read per heartbeat. Two failed
# probes in a row (>= 1 minute apart) mean it is lost: confirmed live
# (2026-11-20) that `proxmox-backup-client map` does not reconnect after PBS
# was unreachable -- every later read fails, the guest gets I/O errors for
# good, while the map process (and so its scope) keeps running.
PROBE_TIMEOUT_SECONDS = 30
LOST_AFTER_FAILED_PROBES = 2
# Drives restored to the target storage before the start instead of being mapped.
PRE_START_DRIVES = ("efidisk0", "tpmstate0")
_DISK_SLOT_RE = re.compile(r"^(ide|sata|scsi|virtio)\d+$")
_NET_SLOT_RE = re.compile(r"^net\d+$")
_UNUSED_RE = re.compile(r"^unused\d+$")
# Host-bound devices: passthrough and directory shares that may not exist on
# the target node -- dropped and reported, the VM would not start with them.
_HOST_BOUND_RE = re.compile(r"^(hostpci\d+|virtiofs\d+)$")
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
# Keys that are state or bindings of the *original* VM, never hardware:
# `meta`/`lock`/`digest`/`parent` (not `qm create` options, ADR 0019),
# `onboot`/`startup` (a VM whose disks live on a userspace mapping must never
# autostart after a node reboot -- the mapping is gone then), `protection`
# (would block Discard), `template`, `hookscript`/`cicustom` (snippet files of
# the source cluster), saved-state keys.
_DROPPED_KEYS = frozenset(
    {
        "meta",
        "lock",
        "digest",
        "parent",
        "onboot",
        "startup",
        "protection",
        "template",
        "hookscript",
        "cicustom",
        "vmstate",
        "runningmachine",
        "runningcpu",
        *PRE_START_DRIVES,
    }
)


class InstantRecoveryError(Exception):
    pass


# --- naming -------------------------------------------------------------------


def slot_from_archive(archive: str) -> str:
    """`drive-scsi0.img.fidx` -> `scsi0`; vzdump names the TPM archive
    `drive-tpmstate0-backup.img.fidx` (qemu-server `QemuServer.pm`)."""
    slot = archive.removeprefix("drive-").removesuffix(".img.fidx")
    return slot.removesuffix("-backup")


def scope_unit_name(plan_id: str, slot: str) -> str:
    return f"resurgam-ir-{plan_id}-{slot}"


def overlay_filename(vmid: int, slot: str) -> str:
    return f"vm-{vmid}-ir-{slot}.qcow2"


def overlay_dir_for(config: LabAgentConfig, vmid: int) -> str:
    return os.path.join(config.overlay_dir, "images", str(vmid))


# --- the VM config ------------------------------------------------------------


def _without_size(options: list[str]) -> list[str]:
    return [o for o in options if not o.startswith("size=")]


def _nic_value(value: str, *, mode: str, bridge: str | None, keep_mac: bool) -> str:
    parts = [p for p in value.split(",") if p]
    out: list[str] = []
    for i, part in enumerate(parts):
        key, sep, val = part.partition("=")
        if i == 0 and sep and _MAC_RE.match(val) and not keep_mac:
            out.append(key)  # `virtio=AA:..` -> `virtio`: PVE generates a new MAC
            continue
        if key == "macaddr" and not keep_mac:
            continue
        if key == "bridge" and mode in ("isolated", "custom") and bridge:
            continue
        if key == "bridge" and mode == "none" and bridge:
            continue
        if key == "tag" and mode in ("isolated", "custom") and bridge:
            # The original VLAN rarely exists on the chosen bridge. Without a
            # chosen bridge the NIC stays on the original one, and so does its
            # tag: dropping it there would put the NIC on that bridge's untagged
            # network once someone sets the link up (live finding 2026-11-20).
            continue
        if key == "link_down":
            continue
        out.append(part)
    if mode in ("isolated", "custom", "none") and bridge:
        out.append(f"bridge={bridge}")
    if mode in ("none", "isolated"):
        out.append("link_down=1")
    elif mode == "original":
        old = next((p for p in parts if p.startswith("link_down=")), None)
        if old:
            out.append(old)
    return ",".join(out)


def build_instant_vm_config(
    original: dict[str, str],
    *,
    vmid: int,
    overlay_storage_id: str,
    data_slots: list[str],
    name: str,
    network_mode: str,
    network_bridge: str | None,
    keep_mac: bool,
    plan_id: str,
) -> tuple[dict[str, str], list[str]]:
    """The VM's `qm create` options, from the backup's own config (ADR 0019:
    machine type, controllers, NIC model, smbios UUID are kept -- Windows needs
    them). Returns `(config, skipped)`; `skipped` names what was dropped
    because it can't come along (a disk excluded from the backup, a passthrough
    device).

    - Every disk in `data_slots` points at its overlay, keeping its own options
      (cache, discard, ssd, serial, ...), minus `size=`.
    - A CD-ROM keeps its slot but no medium (the ISO lives on the source cluster).
    - `efidisk0` / `tpmstate0` are left out here; they are imported before the
      start (`start_instant_recovery`).
    - Network: see ADR 0030 §4. `keep_mac=False` drops the MAC so PVE assigns
      a new one (the original VM runs, or its state is unknown).
    """
    config: dict[str, str] = {}
    skipped: list[str] = []
    kept_devices: set[str] = set()
    for key, value in original.items():
        if key in _DROPPED_KEYS or _UNUSED_RE.match(key):
            continue
        if _HOST_BOUND_RE.match(key) or (key.startswith("usb") and "host=" in value):
            skipped.append(key)
            continue
        if _DISK_SLOT_RE.match(key):
            options = value.split(",")
            if "media=cdrom" in options:
                config[key] = "none,media=cdrom"
                kept_devices.add(key)
            elif key in data_slots:
                volume = f"{overlay_storage_id}:{vmid}/{overlay_filename(vmid, key)}"
                config[key] = ",".join([volume, *_without_size(options[1:])])
                kept_devices.add(key)
            else:
                skipped.append(key)  # in the VM's config, not in the backup (backup=0)
            continue
        if _NET_SLOT_RE.match(key):
            config[key] = _nic_value(value, mode=network_mode, bridge=network_bridge, keep_mac=keep_mac)
            kept_devices.add(key)
            continue
        config[key] = value

    boot = config.get("boot", "")
    if boot.startswith("order="):
        order = [d for d in boot.removeprefix("order=").split(";") if d in kept_devices]
        if order:
            config["boot"] = "order=" + ";".join(order)
        else:
            config.pop("boot")

    tags = [t for t in re.split(r"[;, ]+", config.get("tags", "")) if t]
    if TAG not in tags:
        tags.append(TAG)
    config["tags"] = ";".join(tags)
    config["name"] = name
    config["description"] = (
        f"RESURGAM Instant Recovery {plan_id}: runs from a PBS backup until its disks are moved to "
        "storage in RESURGAM. Do not remove its disks or migrate it."
    )
    return config, skipped


def _with_options(volume_value: str, original_value: str) -> str:
    """The imported volume (`qm config` value, may end in `,size=4M`) with the
    original drive's options (`efitype=4m,pre-enrolled-keys=1`, `version=v2.0`)."""
    volume = volume_value.split(",", 1)[0]
    return ",".join([volume, *_without_size(original_value.split(",")[1:])])


# --- start --------------------------------------------------------------------


def _free_bytes(path: str) -> tuple[int, int]:
    stats = os.statvfs(path)
    return stats.f_blocks * stats.f_frsize, stats.f_bavail * stats.f_frsize


def _repository(pbs_token_id: str, claim: InstantRecoveryClaim) -> str:
    return f"{pbs_token_id}@{claim.pbs_host}:{claim.pbs_port}:{claim.pbs_datastore}"


def start_instant_recovery(job: InstantRecoveryJob, config: LabAgentConfig, client: LabAgentClient) -> None:
    claim = client.claim_instant_recovery_job(job.id)
    logger.info("instant-recovery: claimed plan %s (vmid %s)", claim.id, claim.target_vmid)
    pbs_token_id, _, pbs_secret = config.pbs_token.partition(":")
    repository = _repository(pbs_token_id, claim)
    kw = {"namespace": claim.pbs_namespace, "token": pbs_secret, "keyfile": claim.encryption_keyfile}
    vmid = claim.target_vmid
    local_dir = overlay_dir_for(config, vmid)

    mapped: list[str] = []
    vm_created = False
    created_dir = False
    try:
        if claim.target_storage == config.overlay_storage_id:
            raise InstantRecoveryError(
                f"The target storage {claim.target_storage!r} is this node's overlay storage; pick real storage."
            )
        if os.path.exists(local_dir) and os.listdir(local_dir):
            raise InstantRecoveryError(f"{local_dir} already holds files; VMID {vmid} was used before.")
        os.makedirs(local_dir, exist_ok=True)
        created_dir = True
        _total, free = _free_bytes(local_dir)
        if free < claim.min_free_bytes:
            raise InstantRecoveryError(
                f"Only {free // 2**30} GiB free on the overlay storage ({config.overlay_dir}); "
                f"Instant Recovery needs at least {claim.min_free_bytes // 2**30} GiB for the writes."
            )

        original = fetch_original_vm_config(repository, claim.snapshot_path, **kw)
        archives = list_snapshot_archives(repository, claim.snapshot_path, **kw)
        by_slot = {slot_from_archive(a): a for a in archives}
        data_slots = [s for s in by_slot if s not in PRE_START_DRIVES and _DISK_SLOT_RE.match(s)]
        if not data_slots:
            raise InstantRecoveryError(f"The backup {claim.snapshot_path} has no disk archives to run from.")

        for slot in data_slots:
            unit = scope_unit_name(claim.id, slot)
            device = map_snapshot(repository, claim.snapshot_path, by_slot[slot], scope_unit=unit, **kw).device_path
            mapped.append(device)
            client.report_instant_recovery_progress(
                claim.id, mapped_device={"slot": slot, "archive": by_slot[slot], "device": device, "unit": unit}
            )
            create_cow_overlay(device, os.path.join(local_dir, overlay_filename(vmid, slot)))
            client.report_instant_recovery_progress(
                claim.id,
                overlay_volume={
                    "slot": slot,
                    "volume": f"{config.overlay_storage_id}:{vmid}/{overlay_filename(vmid, slot)}",
                },
            )

        vm_config, skipped = build_instant_vm_config(
            original,
            vmid=vmid,
            overlay_storage_id=config.overlay_storage_id,
            data_slots=data_slots,
            name=claim.name,
            network_mode=claim.network_mode,
            network_bridge=claim.network_bridge,
            keep_mac=claim.keep_mac,
            plan_id=claim.id,
        )
        create_vm(vmid, vm_config)
        vm_created = True
        client.report_instant_recovery_progress(claim.id, vm_created=True)

        for slot in PRE_START_DRIVES:
            if slot not in original:
                continue
            if slot not in by_slot:
                skipped.append(slot)
                continue
            raw = os.path.join(local_dir, f"{slot}.raw")
            try:
                restore_image(repository, claim.snapshot_path, by_slot[slot], raw, **kw)
                value = import_disk(vmid, raw, claim.target_storage, disk_format="raw")
            finally:
                if os.path.exists(raw):
                    os.remove(raw)
            set_vm_options(vmid, {slot: _with_options(value, original[slot])})
            client.report_instant_recovery_progress(
                claim.id, imported_volume={"slot": slot, "volume": value.split(",", 1)[0]}
            )

        client.report_instant_recovery_progress(claim.id, phase="starting")
        start_vm(vmid)
    except (InstantRecoveryError, PbsClientError, PveClientError, OSError) as exc:
        logger.warning("instant-recovery: start of plan %s failed: %s", claim.id, exc, exc_info=True)
        cleaned = _undo_start(vmid, vm_created, mapped, local_dir if created_dir else None, pbs_secret)
        client.report_instant_recovery_status(claim.id, "failed", error=str(exc), cleaned_up=cleaned)
        return

    logger.info("instant-recovery: VM %s runs from backup %s", vmid, claim.snapshot_path)
    client.report_instant_recovery_status(claim.id, "running", skipped_disks=skipped)


def _undo_start(vmid: int, vm_created: bool, devices: list[str], local_dir: str | None, token: str) -> bool:
    cleaned = True
    if vm_created:
        try:
            if vm_status(vmid) == "running":
                stop_vm(vmid)
            destroy_vm(vmid)
        except PveClientError:
            logger.warning("instant-recovery: could not remove VM %s", vmid, exc_info=True)
            cleaned = False
    for device in devices:
        try:
            unmap(device, token=token)
        except PbsClientError:
            logger.warning("instant-recovery: could not unmap %s", device, exc_info=True)
            cleaned = False
    if local_dir is not None and cleaned:
        shutil.rmtree(local_dir, ignore_errors=True)
    return cleaned


# --- while it runs ------------------------------------------------------------


def scope_active(unit: str, *, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> bool | None:
    """Whether the mapping's scope still has its `map` process (True), is gone
    (False), or systemd could not be asked (None)."""
    try:
        result = run(
            ["systemctl", "is-active", f"{unit}.scope"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    state = (result.stdout or "").strip()
    if state in ("active", "activating", "deactivating"):
        return True
    if state in ("inactive", "failed"):
        return False
    return None


def probe_mapping(
    device: str,
    *,
    timeout: int = PROBE_TIMEOUT_SECONDS,
    choose: Callable[[int], int] = random.randrange,
    popen: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> bool | None:
    """One direct 4 KiB read at a random offset of a mapped backup disk: True
    when it worked, False when it failed or hung, None when this can't be
    checked here (device size unknown, `dd`/`timeout` missing).

    The offset is random so the client's chunk cache can't answer for a dead
    connection. `timeout -s KILL` ends a read that hangs (network gone without
    a reset); if even that does not return in time (process stuck in the
    kernel), the probe gives up without waiting for it."""
    name = os.path.basename(device)
    try:
        with open(f"/sys/class/block/{name}/size") as handle:
            sectors = int(handle.read().strip())
    except (OSError, ValueError):
        return None
    blocks = sectors * 512 // 4096
    if blocks <= 0:
        return None
    command = [
        "timeout",
        "-s",
        "KILL",
        str(timeout),
        "dd",
        f"if={device}",
        "of=/dev/null",
        "bs=4096",
        "count=1",
        f"skip={choose(blocks)}",
        "iflag=direct",
        "status=none",
    ]
    try:
        process = popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    try:
        returncode = process.wait(timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        return False
    if returncode in (125, 126, 127):  # `timeout` itself failed, or dd is missing / not executable
        return None
    return returncode == 0


def _still_on_overlay(vmid: int, slot: str, overlay_storage_id: str) -> bool:
    """Whether the VM's disk in `slot` still reads through the overlay (and so
    from the backup). A disk already moved to storage no longer needs PBS."""
    try:
        value = get_vm_config(vmid).get(slot, "")
    except PveClientError:
        return True  # can't tell -- assume it still depends on the mapping
    return value.split(":", 1)[0] == overlay_storage_id


def overlay_bytes(local_dir: str) -> int:
    """Space the overlays really occupy (allocated blocks, not virtual size)."""
    total = 0
    try:
        names = os.listdir(local_dir)
    except FileNotFoundError:
        return 0
    for name in names:
        try:
            total += os.stat(os.path.join(local_dir, name)).st_blocks * 512
        except FileNotFoundError:
            continue
    return total


def watch_instant_recovery(
    job: InstantRecoveryJob,
    config: LabAgentConfig,
    client: LabAgentClient,
    last_heartbeat: dict[str, float],
    *,
    probe_failures: dict[str, int] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    is_active: Callable[[str], bool | None] = scope_active,
    probe: Callable[[str], bool | None] = probe_mapping,
    still_on_overlay: Callable[[int, str, str], bool] = _still_on_overlay,
) -> None:
    """A heartbeat per running plan (overlay size, free space), at most once a
    minute. A mapping is reported as lost -- the guest gets I/O errors from then
    on -- when its scope is gone (node reboot, killed process) or when reading
    from it failed twice in a row (PBS or the network to it was gone; the
    mapping does not recover from that)."""
    now = monotonic()
    if now - last_heartbeat.get(job.id, float("-inf")) < HEARTBEAT_INTERVAL_SECONDS:
        return
    last_heartbeat[job.id] = now
    failures = probe_failures if probe_failures is not None else {}

    lost = [d.get("slot") for d in job.mapped_devices if d.get("unit") and is_active(str(d["unit"])) is False]
    if lost:
        client.report_instant_recovery_status(
            job.id,
            "lost",
            error=(
                f"The backup mapping of {', '.join(map(str, lost))} is gone (node restarted or the map process "
                "ended). The VM can't read from the backup any more. Its writes are kept in "
                f"{overlay_dir_for(config, job.target_vmid)}."
            ),
        )
        return

    unreadable: list[str] = []
    for entry in job.mapped_devices:
        device = str(entry.get("device") or "")
        slot = str(entry.get("slot") or "")
        if not device or not slot:
            continue
        key = f"{job.id}:{slot}"
        result = probe(device)
        if result is True:
            failures.pop(key, None)
        elif result is False:
            if not still_on_overlay(job.target_vmid, slot, config.overlay_storage_id):
                failures.pop(key, None)  # already moved to storage: the VM no longer reads from the backup
                continue
            failures[key] = failures.get(key, 0) + 1
            logger.warning("instant-recovery: reading the backup mapping of %s (%s) failed", slot, device)
            if failures[key] >= LOST_AFTER_FAILED_PROBES:
                unreadable.append(slot)
    if unreadable:
        for slot in unreadable:
            failures.pop(f"{job.id}:{slot}", None)
        client.report_instant_recovery_status(
            job.id,
            "lost",
            error=(
                f"Reading the backup of {', '.join(unreadable)} fails (PBS or the network to it was unreachable; "
                "the mapping does not recover from that). The guest gets I/O errors from now on. Discard it and "
                "start a new Instant Recovery, or restore it in full. Its writes are kept in "
                f"{overlay_dir_for(config, job.target_vmid)} until you discard it."
            ),
        )
        return

    local_dir = overlay_dir_for(config, job.target_vmid)
    probe = local_dir if os.path.isdir(local_dir) else config.overlay_dir
    total, free = _free_bytes(probe)
    client.report_instant_recovery_heartbeat(
        job.id, overlay_bytes=overlay_bytes(local_dir), storage_total_bytes=total, storage_free_bytes=free
    )


# --- finish / discard ---------------------------------------------------------


def _unmap_all(job: InstantRecoveryJob, token: str) -> list[str]:
    errors: list[str] = []
    for entry in job.mapped_devices:
        device = str(entry.get("device") or "")
        unit = str(entry.get("unit") or "")
        if unit and scope_active(unit) is False:
            continue  # already gone
        try:
            unmap(device, token=token)
        except PbsClientError as exc:
            errors.append(f"{device}: {exc}")
    return errors


def finish_instant_recovery(job: InstantRecoveryJob, config: LabAgentConfig, client: LabAgentClient) -> None:
    """After the last disk was moved: refuse while any disk still uses an
    overlay, then unmap and remove the overlay directory."""
    _, _, pbs_secret = config.pbs_token.partition(":")
    try:
        current = get_vm_config(job.target_vmid)
        still = [
            k for k, v in current.items() if _DISK_SLOT_RE.match(k) and v.startswith(f"{config.overlay_storage_id}:")
        ]
        if still:
            client.report_instant_recovery_status(
                job.id, "finish_failed", error=f"Still running from the backup: {', '.join(still)}."
            )
            return
    except PveClientError as exc:
        client.report_instant_recovery_status(job.id, "finish_failed", error=str(exc))
        return
    errors = _unmap_all(job, pbs_secret)
    if errors:
        client.report_instant_recovery_status(job.id, "finish_failed", error="; ".join(errors))
        return
    shutil.rmtree(overlay_dir_for(config, job.target_vmid), ignore_errors=True)
    # An ordinary VM now: drop the "runs from a backup" tag and description.
    try:
        tags = [t for t in _tags(current) if t != TAG]
        options = {"description": f"Recovered by RESURGAM Instant Recovery {job.id}; disks moved to storage."}
        if tags:
            options["tags"] = ";".join(tags)
        set_vm_options(job.target_vmid, options)
        if not tags:
            delete_vm_options(job.target_vmid, ["tags"])
    except PveClientError:
        logger.warning("instant-recovery: could not update tags of VM %s", job.target_vmid, exc_info=True)
    client.report_instant_recovery_status(job.id, "finished")


def _tags(config: dict[str, str]) -> list[str]:
    return [t for t in re.split(r"[;, ]+", config.get("tags", "")) if t]


def is_our_vm(config: dict[str, str], job: InstantRecoveryJob) -> bool:
    """The VM at `target_vmid` is the one this plan created: still tagged by
    RESURGAM and still carrying the plan's name. Anything else -- the VMID was
    reused after a failed start, or someone rebuilt it -- is never touched."""
    return TAG in _tags(config) and config.get("name") == job.name


def discard_instant_recovery(job: InstantRecoveryJob, config: LabAgentConfig, client: LabAgentClient) -> None:
    """Stop and destroy the VM (its overlays and imported EFI/TPM volumes go
    with it), unmap, remove the overlay directory. Only a VM this plan created
    and that is still recognisably ours is destroyed."""
    _, _, pbs_secret = config.pbs_token.partition(":")
    try:
        status = vm_status(job.target_vmid) if job.vm_created else None
        if status is not None:
            if not is_our_vm(get_vm_config(job.target_vmid), job):
                client.report_instant_recovery_status(
                    job.id,
                    "discard_failed",
                    error=(
                        f"VM {job.target_vmid} is no longer the recovered VM (tag {TAG!r} or name {job.name!r} "
                        "missing); RESURGAM does not touch it. Remove it by hand if it is yours, then discard again."
                    ),
                )
                return
            if status != "stopped":
                stop_vm(job.target_vmid)
            destroy_vm(job.target_vmid)
    except PveClientError as exc:
        client.report_instant_recovery_status(job.id, "discard_failed", error=str(exc))
        return
    errors = _unmap_all(job, pbs_secret)
    if errors:
        client.report_instant_recovery_status(job.id, "discard_failed", error="; ".join(errors))
        return
    shutil.rmtree(overlay_dir_for(config, job.target_vmid), ignore_errors=True)
    client.report_instant_recovery_status(job.id, "discarded")


def process_instant_recovery_job(
    job: InstantRecoveryJob,
    config: LabAgentConfig,
    client: LabAgentClient,
    last_heartbeat: dict[str, float],
    probe_failures: dict[str, int] | None = None,
) -> None:
    if job.target_node != local_node_name(config):
        return
    try:
        if job.kind == "start":
            start_instant_recovery(job, config, client)
        elif job.kind == "watch":
            watch_instant_recovery(job, config, client, last_heartbeat, probe_failures=probe_failures)
        elif job.kind == "finish":
            finish_instant_recovery(job, config, client)
        elif job.kind == "discard":
            discard_instant_recovery(job, config, client)
    except LabAgentApiError as exc:
        logger.info("instant-recovery: skipping plan %s (%s): %s", job.id, job.kind, exc)
    except OSError as exc:
        logger.warning("instant-recovery: plan %s (%s) failed locally: %s", job.id, job.kind, exc, exc_info=True)
