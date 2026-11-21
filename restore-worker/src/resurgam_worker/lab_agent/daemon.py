from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from resurgam_worker.lab_agent import lab_cleanup
from resurgam_worker.lab_agent.client import (
    ApplianceSnippetJob,
    FileIndexEntry,
    FileIndexJob,
    LabAgentApiError,
    LabAgentClient,
    LabAgentJob,
)
from resurgam_worker.lab_agent.config import LabAgentConfig, load_config
from resurgam_worker.lab_agent.disk_mount import process_disk_mount_job
from resurgam_worker.lab_agent.file_index import (
    UnsupportedOsTypeError,
    build_cleanup_command,
    build_enumeration_command,
    chunk_path_for_ostype,
    manifest_path_for_ostype,
    parse_chunk_count,
    parse_output_for_ostype,
)
from resurgam_worker.lab_agent.instant_recovery import process_instant_recovery_job
from resurgam_worker.lab_agent.setup_jobs import process_setup_job
from resurgam_worker.pbs_client.errors import PbsClientError
from resurgam_worker.pbs_client.map import fetch_original_vm_config, list_snapshot_archives, map_snapshot, unmap
from resurgam_worker.pve_client.errors import PveClientError, PveCommandFailedError
from resurgam_worker.pve_client.pvesm import resolve_storage_path
from resurgam_worker.pve_client.qm import (
    create_cow_overlay,
    create_vm,
    destroy_vm,
    get_vm_config,
    guest_exec,
    guest_exec_launch,
    guest_file_read_all,
    start_vm,
    stop_vm,
    wait_for_guest_agent,
)

logger = logging.getLogger("resurgam_worker.lab_agent")

# Pure local IPC (a file-drop directory), not a socket/HTTP listener in
# this agent -- matches the router-bootstrap script's own "boring,
# inspectable script" precedent (ADR 0020). Must match the shipped
# hookscript's own $triggers_dir exactly.
_BACKUP_TRIGGERS_DIR = "/var/run/resurgam-lab-agent/backup-triggers"

# How many entries `submit_file_index_entries` sends per POST -- matches
# the backend's own `file_index_entries_insert_batch_size` default so
# neither side is the accidental bottleneck.
_ENTRY_SUBMIT_BATCH_SIZE = 1000

# How long `_wait_for_manifest` waits for the enumeration's completion
# marker to appear before giving up -- generous on purpose, matching
# `guest_exec`'s own old wait budget: a real full-drive enumeration on a
# real production VM has been observed live to take several minutes.
_ENUMERATION_WAIT_TIMEOUT_SECONDS = 1800.0
_ENUMERATION_POLL_INTERVAL_SECONDS = 5.0

# Any of PVE's disk-slot prefixes, matched against the *original* guest's
# config so extra disks (other than the one archive Phase A actually
# mapped) are stripped rather than passed through pointing at production
# storage IDs that don't exist -- or worse, do exist -- on the Lab
# cluster. See `_build_member_vm_config`.
_DISK_SLOT_PATTERN = re.compile(r"^(ide|sata|scsi|virtio|unused)\d+$")
_NET_SLOT_PATTERN = re.compile(r"^net\d+$")

# Not every key a real `qm config`/backup dump carries is a valid `qm
# create` CLI option -- a real gap found live (2026-09-22): `meta`
# (`qm help create` confirms it isn't a top-level create option at all --
# it only exists as a `--cicustom` sub-key of a totally different
# meaning) made every real-config-based create fail outright
# ("Unknown option: meta"). `lock`/`digest`/`parent` are the same class
# of informational/computed-state field (never itself a hardware
# property), stripped defensively even though only `meta` has actually
# been observed live so far.
_NON_CREATE_KEYS = frozenset({"meta", "lock", "digest", "parent"})


def _build_member_vm_config(
    original_config: dict[str, str],
    *,
    archive_name: str,
    overlay_storage_id: str,
    target_vmid: int,
    overlay_filename: str,
    name: str,
    vnet_name: str,
) -> dict[str, str]:
    """Reconstructs the lab member's `qm create` config from the
    *original* guest's real config (`pbs_client.map.fetch_original_vm_config`)
    -- a real, decisive gap found live (2026-09-22): a synthetic
    from-scratch config (memory/cores/net0/scsi0 only) happened to boot
    a Linux guest but sent a real Windows restore straight into repair
    mode, having silently dropped `machine` type, the guest's *real*
    disk controller (this guest used `sata0`, not the hardcoded
    `scsi0`), NIC model, `ostype`, real socket/core/memory counts, and
    the `smbios1` UUID.

    Only two things are ever remapped: the disk slot the mapped archive
    actually belongs to (archive filenames are `drive-<slot>.img.fidx`,
    e.g. `drive-sata0.img.fidx` -> slot `sata0`) now points at the local
    COW overlay instead of the original's production storage reference,
    and `net0`'s `bridge=` now points at this run's isolated Lab VNet
    (its NIC model/MAC are preserved from the original for fidelity;
    `firewall=1`, if present, is dropped deliberately -- PVE's per-NIC
    firewall has no rules configured for an ad-hoc Lab VNet, so leaving
    it enabled risks silently dropping the DHCP/probe traffic this
    entire feature depends on). Every other original disk slot and NIC
    (`net1`, `net2`, ...) is dropped -- Phase A only ever maps one
    archive (see `process_job`'s own single-disk scoping), and passing
    the rest through would reference production storage IDs the Lab
    cluster doesn't (or, worse, coincidentally does) have.
    """
    slot = archive_name.removeprefix("drive-").removesuffix(".img.fidx")

    config = {
        key: value
        for key, value in original_config.items()
        if key not in _NON_CREATE_KEYS
        and not (_DISK_SLOT_PATTERN.match(key) and key != slot)
        and not (_NET_SLOT_PATTERN.match(key) and key != "net0")
    }

    config[slot] = f"{overlay_storage_id}:{target_vmid}/{overlay_filename}"

    net0_parts = [
        p for p in original_config.get("net0", "").split(",") if p and not p.startswith(("bridge=", "firewall="))
    ]
    if not net0_parts:
        net0_parts = ["model=virtio"]
    net0_parts.append(f"bridge={vnet_name}")
    config["net0"] = ",".join(net0_parts)

    config["name"] = name
    config["agent"] = "1"
    return config


def _build_guest_exec_ephemeral_vm_config(
    original_config: dict[str, str],
    *,
    archive_name: str,
    overlay_storage_id: str,
    target_vmid: int,
    overlay_filename: str,
    name: str,
) -> dict[str, str]:
    """Same disk-remap discipline as `_build_member_vm_config` (Recovery
    Lab), but `net0` is disconnected (`link_down=1`) rather than attached
    to any Lab VNet -- guest-exec-ephemeral needs no network path at all,
    the guest-exec calls it exists to serve happen over PVE's own
    virtio-serial QGA channel, not the network.

    A real bug found live (2026-09-24): the first version of this
    function hardcoded `net0 = "model=virtio,bridge=vmbr0,link_down=1"`,
    discarding the original NIC's real model/MAC entirely -- the exact
    same class of mistake `_build_member_vm_config`'s own docstring
    already documents and fixes for Recovery Lab (a hardcoded synthetic
    value instead of the guest's real hardware sent a real Windows
    restore into repair mode). Live-confirmed here too: a real Windows
    ephemeral clone with a forced `virtio` NIC (replacing the original's
    real `e1000`) never became guest-agent-reachable and showed a
    persistently black display, while disk I/O kept slowly progressing
    (`info blockstats`) -- consistent with Windows stuck doing new-
    hardware detection/verification rather than a normal boot. Fixed by
    preserving the original NIC's `model=`/MAC and every other sub-value
    except `bridge=`/`firewall=`, matching `with_link_down`'s own
    "preserve everything, only flip link state" precedent
    (`backend/restore/execution.py`) far more faithfully than a full
    rebuild.
    """
    slot = archive_name.removeprefix("drive-").removesuffix(".img.fidx")

    config = {
        key: value
        for key, value in original_config.items()
        if key not in _NON_CREATE_KEYS
        and not (_DISK_SLOT_PATTERN.match(key) and key != slot)
        and not (_NET_SLOT_PATTERN.match(key) and key != "net0")
    }
    config[slot] = f"{overlay_storage_id}:{target_vmid}/{overlay_filename}"

    net0_parts = [
        p for p in original_config.get("net0", "").split(",") if p and not p.startswith(("bridge=", "firewall="))
    ]
    if not net0_parts:
        net0_parts = ["model=virtio"]
    net0_parts += ["bridge=vmbr0", "link_down=1"]
    config["net0"] = ",".join(net0_parts)

    config["name"] = name
    config["agent"] = "1"
    return config


def _pbs_repository(pbs_token_id: str, job: LabAgentJob | FileIndexJob) -> str:
    """`proxmox-backup-client`'s own repository syntax is
    `[[username@]server[:port]:]datastore` -- the port belongs *inside*
    this string, not as a separate `--port` flag alongside `--repository`.
    Confirmed live (2026-09-21): passing both raises `Error: --repository
    and --server/--port/--datastore/--auth-id are mutually exclusive` --
    real PBS CLI behavior, not a config issue. Shared by Recovery Lab
    jobs and `guest_exec_ephemeral` file-index jobs (ADR 0021) -- both
    carry the same `pbs_host`/`pbs_port`/`pbs_datastore` fields.
    """
    return f"{pbs_token_id}@{job.pbs_host}:{job.pbs_port}:{job.pbs_datastore}"


def _pbs_snapshot_path(job: LabAgentJob | FileIndexJob) -> str:
    """PVE/PBS's own snapshot-path notation, `<type>/<id>/<time>` --
    matches `backend`'s `restore.command.pbs_volid`'s timestamp format
    exactly (`%Y-%m-%dT%H:%M:%SZ`), since both address the same real PBS
    snapshot-time convention.
    """
    parsed = datetime.fromisoformat(job.snapshot_time)
    return f"{job.backup_type}/{job.backup_id}/{parsed.strftime('%Y-%m-%dT%H:%M:%SZ')}"


def process_job(job: LabAgentJob, config: LabAgentConfig, client: LabAgentClient) -> None:
    """Maps the job's snapshot (zero-copy), overlays a writable COW
    layer on top, builds a VM config around it, and starts the guest --
    ADR 0019's real, live-proven mechanism (`proxmox-backup-client map`
    + direct VM-config attach), not a PVE-API `qmrestore` call.

    Only the first disk archive is used (Phase A scoping, matching the
    backend's own single-disk-VM boundary at plan-creation time --
    multi-disk members are a real follow-up, not silently mishandled).
    """
    client.claim_job(job.member_id)
    logger.info("lab-agent: claimed job for member %s (vmid %s)", job.member_id, job.target_vmid)

    pbs_token_id, _, pbs_secret = config.pbs_token.partition(":")
    repository = _pbs_repository(pbs_token_id, job)
    snapshot = _pbs_snapshot_path(job)

    mapped_device: str | None = None
    try:
        archives = list_snapshot_archives(
            repository, snapshot, namespace=job.pbs_namespace, token=pbs_secret, keyfile=job.encryption_keyfile
        )
        if not archives:
            raise PbsClientError(f"snapshot {snapshot!r} has no disk archives")

        mapped = map_snapshot(
            repository,
            snapshot,
            archives[0],
            namespace=job.pbs_namespace,
            token=pbs_secret,
            keyfile=job.encryption_keyfile,
        )
        mapped_device = mapped.device_path
        logger.info("lab-agent: mapped %s as %s", archives[0], mapped_device)

        # PVE's own "dir" storage image-file convention
        # (<storage-path>/images/<vmid>/vm-<vmid>-disk-0.qcow2) -- `qm`
        # rejects a bare filesystem path for `scsi0` outright, confirmed
        # live (2026-09-19: "unable to associate path ... to any
        # storage"). The overlay must live under `config.overlay_dir`
        # (the real filesystem path `qemu-img` writes to) *and* be
        # referenced via `config.overlay_storage_id` (the PVE storage
        # object pointing at that same path) for `qm` to accept it.
        overlay_filename = f"vm-{job.target_vmid}-disk-0.qcow2"
        overlay_local_dir = os.path.join(config.overlay_dir, "images", str(job.target_vmid))
        os.makedirs(overlay_local_dir, exist_ok=True)
        overlay_path = os.path.join(overlay_local_dir, overlay_filename)
        create_cow_overlay(mapped_device, overlay_path)

        original_config = fetch_original_vm_config(
            repository, snapshot, namespace=job.pbs_namespace, token=pbs_secret, keyfile=job.encryption_keyfile
        )
        vm_config = _build_member_vm_config(
            original_config,
            archive_name=archives[0],
            overlay_storage_id=config.overlay_storage_id,
            target_vmid=job.target_vmid,
            overlay_filename=overlay_filename,
            name=f"lab-member-{job.member_id[:8]}",
            vnet_name=job.vnet_name,
        )
        create_vm(job.target_vmid, vm_config, pool=job.pool)
        start_vm(job.target_vmid)
        logger.info("lab-agent: started VM %s for member %s", job.target_vmid, job.member_id)
        # Unmapped by `lab_cleanup.sweep` once the run's cleanup removed the VM.
        lab_cleanup.record_mapping(mapped_device, job.target_vmid)
    except (PbsClientError, PveClientError, OSError) as exc:
        logger.warning("lab-agent: job for member %s failed: %s", job.member_id, exc, exc_info=True)
        if mapped_device is not None:
            try:
                unmap(mapped_device, token=pbs_secret)
            except PbsClientError:
                logger.warning("lab-agent: best-effort unmap of %s failed", mapped_device, exc_info=True)
        client.report_status(job.member_id, "failed", error=str(exc))
        return

    client.report_status(job.member_id, "booted")


def process_appliance_job(job: ApplianceSnippetJob, client: LabAgentClient) -> None:
    """Writes one Recovery Lab run's probe appliance cloud-init snippet
    to local disk (Phase B: PVE has no REST endpoint to upload a
    snippet file, confirmed live 2026-09-21 -- the upload API's
    `content` param rejects "snippets" outright). `resolve_storage_path`
    (`pvesm path`) turns the backend's storage-relative reference into a
    real filesystem path even though the file doesn't exist yet.
    """
    path = resolve_storage_path(f"{job.snippet_storage_id}:snippets/{job.filename}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(job.content)
    lab_cleanup.record_snippet(path)  # removed by `lab_cleanup.sweep` once no VM uses it
    client.confirm_snippet_written(job.plan_id)
    logger.info("lab-agent: wrote appliance snippet for run %s to %s", job.plan_id, path)


def _submit_entries_in_batches(client: LabAgentClient, job_id: str, rows: list[dict]) -> None:
    """Converts the parser's plain dicts (`file_index.parse_output_for_ostype`)
    into `FileIndexEntry` and POSTs them in `_ENTRY_SUBMIT_BATCH_SIZE`
    chunks -- a real guest-exec listing can be hundreds of thousands of
    rows, far too many for one request.
    """
    entries = [
        FileIndexEntry(
            path=row["path"],
            filename=row["filename"],
            type=row["type"],
            size_bytes=row["size_bytes"],
            mtime=row["mtime"].isoformat() if row["mtime"] is not None else None,
        )
        for row in rows
    ]
    for i in range(0, len(entries), _ENTRY_SUBMIT_BATCH_SIZE):
        client.submit_file_index_entries(job_id, entries[i : i + _ENTRY_SUBMIT_BATCH_SIZE])


def _wait_for_manifest(
    vmid: int,
    node: str,
    manifest_path: str,
    *,
    timeout_seconds: float = _ENUMERATION_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = _ENUMERATION_POLL_INTERVAL_SECONDS,
) -> str:
    """Polls for the enumeration's own completion marker (the manifest,
    written last by the guest-side script, after every chunk file is
    already closed) to become readable, and returns its content once it
    is -- deliberately *not* `guest_exec`'s own `exec-status` polling.

    A real, decisive gap found live (2026-09-25): `qm guest exec-status`
    proved unreliable for a genuinely long-running Windows guest
    command -- on a real, busy production VM, it kept reporting
    `exited: 0` (or erroring) for 20+ minutes *after* the guest-side
    script had already finished and the manifest already existed on
    disk, blocking the whole agent the entire time. QGA's own pid-exit
    tracking simply never caught up, for reasons never fully isolated.
    Rather than keep tuning around an unreliable signal, this waits on
    proof of the *actual thing we care about* -- the output existing --
    which is exactly what the manifest already was designed to mean
    (see `file_index.build_enumeration_command`'s own docstring: "the
    manifest is written last... its mere existence is the completion
    signal"). `guest_file_read_all`'s own retry budget is deliberately
    bypassed here (`retries=1`) -- a single quick attempt per poll,
    since *this* loop is what supplies the patience (up to
    `timeout_seconds`), not the read primitive itself; a "not found yet"
    and a transient QGA hiccup are treated identically (both just mean
    "keep waiting"), which is correct for this specific wait since
    either resolves the exact same way -- try again shortly.
    """
    deadline = time.monotonic() + timeout_seconds
    last_error: PveClientError | None = None
    while True:
        try:
            return guest_file_read_all(vmid, manifest_path, node=node, retries=1, retry_delay_seconds=0)
        except PveClientError as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            assert last_error is not None
            raise last_error
        time.sleep(poll_interval_seconds)


def _read_and_submit_chunks(
    client: LabAgentClient, job_id: str, vmid: int, ostype: str, chunk_count: int, *, node: str
) -> int:
    """Reads the guest-side listing back one small chunk at a time and
    submits each as soon as it's parsed, rather than assembling the
    whole listing in memory first -- the direct fix for a real, decisive
    ceiling found live (2026-09-25): PVE's own `agent/file-read` fails
    deterministically ("got timeout") well under 44MB, with no
    lower-level chunked-read primitive anywhere in PVE's exposed API to
    fall back to (confirmed against `pvesh get
    /nodes/<node>/qemu/<vmid>/agent`'s full command list). `chunk_count`
    comes from the caller having already read the manifest via
    `_wait_for_manifest` -- deliberately not a "probe until
    file-not-found" loop, since that would need to tell a genuine
    failure apart from an expected end-of-chunks error by matching
    `pvesh`'s stderr text, which is locale-dependent (a real surprise
    hit earlier this same effort: a German-locale QGA error message).
    """
    total_entries = 0
    for index in range(chunk_count):
        chunk_path = chunk_path_for_ostype(ostype, index)
        raw = guest_file_read_all(vmid, chunk_path, node=node)
        rows = parse_output_for_ostype(ostype, raw)
        _submit_entries_in_batches(client, job_id, rows)
        total_entries += len(rows)
    return total_entries


def process_guest_exec_live_job(job: FileIndexJob, client: LabAgentClient) -> None:
    """`guest_exec_live` (ADR 0021 Sec.4): the target is already a
    running production VM -- no map/boot/teardown, just a guest-exec
    call against it and the results posted back. Runs whenever the
    hookscript's own backup-end trigger creates a job, i.e. right after
    this VM's own real backup already landed on PBS
    (`poll_backup_triggers` below relays the trigger; the backend does
    the actual job creation).
    """
    claim = client.claim_file_index_job(job.id)
    logger.info(
        "lab-agent: claimed guest-exec-live job %s for VM %s (full=%s)",
        job.id,
        job.target_vmid,
        claim.is_full_reindex,
    )
    try:
        vm_config = get_vm_config(job.target_vmid)
        ostype = vm_config.get("ostype", "")
        command_args = build_enumeration_command(ostype, since=claim.since, exclude_patterns=job.exclude_patterns)

        # Cleanup runs as its own confirmed-complete step (the reliable,
        # polling `guest_exec` -- fast, so its own exec-status polling
        # is trustworthy) *before* the real enumeration is dispatched --
        # a real, decisive race found live (2026-09-25) otherwise: a
        # fire-and-forget-launched enumeration hasn't necessarily
        # reached its own cleanup by the time the first manifest poll
        # runs, so a stale manifest from the previous run could be
        # mistaken for this run's fresh result. See
        # `file_index.build_cleanup_command`'s docstring.
        guest_exec(job.target_vmid, build_cleanup_command(ostype))

        # Fire-and-forget dispatch, not a wait -- `guest_exec`'s own
        # `exec-status` polling proved unreliable for a genuinely
        # long-running command (see `_wait_for_manifest`'s docstring).
        # There's no exitcode to sanity-check here as a result; the
        # manifest/chunks actually existing (read next) is the real
        # correctness check regardless.
        _launch_enumeration(job.target_vmid, command_args)
        manifest_text = _wait_for_manifest(job.target_vmid, job.target_node, manifest_path_for_ostype(ostype))
        chunk_count = parse_chunk_count(manifest_text)

        entry_count = _read_and_submit_chunks(
            client, job.id, job.target_vmid, ostype, chunk_count, node=job.target_node
        )
        client.report_file_index_status(job.id, "completed")
        logger.info("lab-agent: guest-exec-live job %s completed (%d entries)", job.id, entry_count)
    except (PveClientError, LabAgentApiError, UnsupportedOsTypeError) as exc:
        logger.warning("lab-agent: guest-exec-live job %s failed: %s", job.id, exc, exc_info=True)
        try:
            client.report_file_index_status(job.id, "failed", error=str(exc))
        except LabAgentApiError:
            logger.warning("lab-agent: could not report failure for job %s", job.id, exc_info=True)


# A start whose guest-agent call timed out is retried; the scan's own lock
# (`file_index._LINUX_LOCK_PATH` / `_WINDOWS_LOCK_PATH`) makes a repeated start
# exit at once if the first one did run after all.
LAUNCH_ATTEMPTS = 4
LAUNCH_RETRY_SECONDS = 15


def _launch_enumeration(vmid: int, command_args: list[str]) -> None:
    for attempt in range(1, LAUNCH_ATTEMPTS + 1):
        try:
            guest_exec_launch(vmid, command_args)
            return
        except PveCommandFailedError as exc:
            if "got timeout" not in str(exc) or attempt == LAUNCH_ATTEMPTS:
                raise
            logger.info("lab-agent: starting the file scan on VM %s timed out, retrying (%d)", vmid, attempt)
            time.sleep(LAUNCH_RETRY_SECONDS)


def process_guest_exec_ephemeral_job(job: FileIndexJob, config: LabAgentConfig, client: LabAgentClient) -> None:
    """`guest_exec_ephemeral` (ADR 0021 Sec.3): boots a disposable clone
    of the snapshot -- the same zero-copy map + COW overlay + real-config
    reconstruction as Recovery Lab's own `process_job`, but disconnected
    from any network (`_build_guest_exec_ephemeral_vm_config`) and
    **always** torn down at the end, success or failure -- unlike a
    Recovery Lab run, this was never meant to persist for inspection.
    """
    claim = client.claim_file_index_job(job.id)
    logger.info(
        "lab-agent: claimed guest-exec-ephemeral job %s for VM %s (full=%s)",
        job.id,
        job.target_vmid,
        claim.is_full_reindex,
    )

    pbs_token_id, _, pbs_secret = config.pbs_token.partition(":")
    repository = _pbs_repository(pbs_token_id, job)
    snapshot = _pbs_snapshot_path(job)
    namespace = job.pbs_namespace or ""

    mapped_device: str | None = None
    booted = False
    try:
        archives = list_snapshot_archives(
            repository, snapshot, namespace=namespace, token=pbs_secret, keyfile=job.encryption_keyfile
        )
        if not archives:
            raise PbsClientError(f"snapshot {snapshot!r} has no disk archives")

        mapped = map_snapshot(
            repository, snapshot, archives[0], namespace=namespace, token=pbs_secret, keyfile=job.encryption_keyfile
        )
        mapped_device = mapped.device_path
        logger.info("lab-agent: mapped %s as %s for file-index job %s", archives[0], mapped_device, job.id)

        overlay_filename = f"vm-{job.target_vmid}-disk-0.qcow2"
        overlay_local_dir = os.path.join(config.overlay_dir, "images", str(job.target_vmid))
        os.makedirs(overlay_local_dir, exist_ok=True)
        overlay_path = os.path.join(overlay_local_dir, overlay_filename)
        create_cow_overlay(mapped_device, overlay_path)

        original_config = fetch_original_vm_config(
            repository, snapshot, namespace=namespace, token=pbs_secret, keyfile=job.encryption_keyfile
        )
        ostype = original_config.get("ostype", "")
        vm_config = _build_guest_exec_ephemeral_vm_config(
            original_config,
            archive_name=archives[0],
            # The overlay file lives in `config.overlay_dir`, so only the
            # agent's overlay storage can reference it. Found live 2026-11-21:
            # the tenant's target storage (`local-lvm`) was used here, and PVE
            # refused `local-lvm:107/vm-107-disk-0.qcow2` ("illegal characters").
            overlay_storage_id=config.overlay_storage_id,
            target_vmid=job.target_vmid,
            overlay_filename=overlay_filename,
            name=f"file-index-{job.id[:8]}",
        )
        create_vm(job.target_vmid, vm_config, pool=job.pool)
        start_vm(job.target_vmid)
        booted = True
        wait_for_guest_agent(job.target_vmid, timeout_seconds=config.guest_exec_agent_wait_timeout_seconds)
        logger.info("lab-agent: guest-exec-ephemeral job %s VM %s agent ready", job.id, job.target_vmid)

        command_args = build_enumeration_command(ostype, since=claim.since, exclude_patterns=job.exclude_patterns)
        # Cleanup first, as its own confirmed-complete step -- see
        # `process_guest_exec_live_job`'s identical comment. Applies
        # here too even though this VM is a fresh clone every run: the
        # *snapshot* being cloned could itself carry stale
        # resurgam-index-* files baked in from an earlier
        # `guest_exec_live` run against this same original guest (both
        # methods share the same guest-side paths).
        guest_exec(job.target_vmid, build_cleanup_command(ostype))
        # Fire-and-forget dispatch -- see `_wait_for_manifest`'s
        # docstring for why this no longer waits via `guest_exec`'s own
        # `exec-status` polling.
        _launch_enumeration(job.target_vmid, command_args)
        manifest_text = _wait_for_manifest(job.target_vmid, job.target_node, manifest_path_for_ostype(ostype))
        chunk_count = parse_chunk_count(manifest_text)

        entry_count = _read_and_submit_chunks(
            client, job.id, job.target_vmid, ostype, chunk_count, node=job.target_node
        )
        client.report_file_index_status(job.id, "completed")
        logger.info("lab-agent: guest-exec-ephemeral job %s completed (%d entries)", job.id, entry_count)
    except (PbsClientError, PveClientError, LabAgentApiError, UnsupportedOsTypeError, OSError) as exc:
        logger.warning("lab-agent: guest-exec-ephemeral job %s failed: %s", job.id, exc, exc_info=True)
        try:
            client.report_file_index_status(job.id, "failed", error=str(exc))
        except LabAgentApiError:
            logger.warning("lab-agent: could not report failure for job %s", job.id, exc_info=True)
    finally:
        # Always torn down, success or failure -- an ephemeral index VM
        # was never meant to persist for inspection the way a Recovery
        # Lab run is. A real bug found live (2026-09-24): `qm destroy`
        # refuses outright ("VM N is running - destroy failed") on a
        # still-running VM -- the common case here, since most failures
        # (e.g. a guest-agent timeout) leave the VM running, only the
        # *check* failed. `stop_vm` first (a hard power-off, not a
        # graceful shutdown -- appropriate for a disposable clone about
        # to be destroyed anyway, and the only option when the guest
        # agent itself is what's unreachable).
        if booted:
            try:
                stop_vm(job.target_vmid)
            except PveClientError:
                logger.warning("lab-agent: best-effort stop of VM %s failed", job.target_vmid, exc_info=True)
            try:
                destroy_vm(job.target_vmid)
            except PveClientError:
                logger.warning("lab-agent: best-effort destroy of VM %s failed", job.target_vmid, exc_info=True)
        if mapped_device is not None:
            try:
                unmap(mapped_device, token=pbs_secret)
            except PbsClientError:
                logger.warning("lab-agent: best-effort unmap of %s failed", mapped_device, exc_info=True)


def poll_backup_triggers(client: LabAgentClient, *, triggers_dir: str = _BACKUP_TRIGGERS_DIR) -> None:
    """Consumes the hookscript's own local file-drop (ADR 0021 Sec.4) --
    pure local IPC, never a socket/HTTP listener in this agent. Each file
    is relayed to the backend then removed regardless of outcome -- a
    failed relay isn't retried from here (`backup-completed` is a cheap,
    idempotent no-op for anything it can't act on, and the next real
    backup of this VM produces a fresh trigger file anyway).
    """
    if not os.path.isdir(triggers_dir):
        return
    for name in sorted(os.listdir(triggers_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(triggers_dir, name)
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
            client.report_backup_completed(payload["vmid"], payload["node"], payload["at"])
        except (OSError, ValueError, KeyError, LabAgentApiError) as exc:
            logger.warning("lab-agent: could not relay backup trigger %s: %s", path, exc, exc_info=True)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


# No per-VM hookscript reconcile loop here (an earlier version had one).
# A real, decisive finding (2026-09-24, live-verified): PVE's per-VM
# `hookscript` property (`qm set <vmid> --hookscript ...`) only fires on
# VM lifecycle phases (pre-start/post-start/pre-stop/post-stop) -- never
# on a backup, confirmed against PVE's own `guest-example-hookscript.pl`.
# The mechanism that actually fires on backup phases is vzdump's own,
# separate `script:` option (`/etc/vzdump.conf`, confirmed against PVE's
# `vzdump-hook-script.pl`) -- a **node-level** setting, not a per-VM one,
# so there is nothing for this agent to reconcile per VM: the shipped
# script, shipped at /usr/share/resurgam-lab-agent/hooks/
# resurgam-guest-exec-live-hook.sh) is referenced directly from
# /etc/vzdump.conf's own `script:` line as a one-time operator/packaging
# step (see debian/resurgam-lab-agent.default's sibling note), not
# deployed anywhere by this agent at runtime. `poll_backup_triggers`
# (above) is still exactly right -- the script still just drops a JSON
# trigger file; only *how* the script gets invoked in the first place
# was wrong.


def _run_setup_job(job, config: LabAgentConfig, client: LabAgentClient) -> None:
    try:
        process_setup_job(job, config, client)
    except LabAgentApiError as exc:
        logger.info("lab-agent: skipping setup job %s: %s", job.id, exc)
    except Exception:  # a thread's uncaught exception is only printed to stderr -- log it properly
        logger.exception("lab-agent: setup job %s crashed", job.id)


@dataclass
class PollState:
    """What survives between poll passes -- kept outside `poll_once` so a pass
    that fails half-way still remembers a setup thread it already started."""

    setup_thread: threading.Thread | None = None
    # Instant Recovery (ADR 0030): monotonic time of the last heartbeat per plan.
    instant_recovery_heartbeats: dict[str, float] = field(default_factory=dict)
    # Consecutive failed read probes per plan and disk slot ("<plan id>:<slot>").
    instant_recovery_probe_failures: dict[str, int] = field(default_factory=dict)


def poll_once(config: LabAgentConfig, client: LabAgentClient, state: PollState) -> None:
    """One pass over every job queue."""
    has_pbs_token = bool(config.pbs_token)
    poll_backup_triggers(client)

    if has_pbs_token:
        try:
            lab_cleanup.sweep(config.pbs_token.partition(":")[2])
        except OSError:  # a cleanup problem must never stop the job queues
            logger.warning("lab-agent: lab leftover cleanup failed", exc_info=True)

    # Template builds queued from the Lab Compute wizard (ADR 0027): one at a
    # time, in a thread -- a build takes many minutes and must not hold up
    # lab runs or disk mounts.
    if state.setup_thread is None or not state.setup_thread.is_alive():
        try:
            setup_jobs = client.list_setup_jobs()
        except LabAgentApiError as exc:
            logger.warning("lab-agent: could not reach RESURGAM backend (setup jobs): %s", exc)
            setup_jobs = []
        if setup_jobs:
            state.setup_thread = threading.Thread(
                target=_run_setup_job, args=(setup_jobs[0], config, client), name="setup-job", daemon=True
            )
            state.setup_thread.start()

    try:
        appliance_jobs = client.list_appliance_jobs()
    except LabAgentApiError as exc:
        logger.warning("lab-agent: could not reach RESURGAM backend (appliance jobs): %s", exc)
        appliance_jobs = []

    for appliance_job in appliance_jobs:
        try:
            process_appliance_job(appliance_job, client)
        except (PveClientError, OSError, LabAgentApiError) as exc:
            logger.warning(
                "lab-agent: writing appliance snippet for run %s failed: %s",
                appliance_job.plan_id,
                exc,
                exc_info=True,
            )

    try:
        jobs = client.list_jobs() if has_pbs_token else []
    except LabAgentApiError as exc:
        logger.warning("lab-agent: could not reach RESURGAM backend: %s", exc)
        jobs = []

    for job in jobs:
        try:
            process_job(job, config, client)
        except LabAgentApiError as exc:
            # Claiming raced with another agent instance, or the run
            # was cleaned up mid-flight -- log and move on, not a
            # reason to crash the whole loop.
            logger.info("lab-agent: skipping job for member %s: %s", job.member_id, exc)

    try:
        file_index_jobs = client.list_file_index_jobs()
    except LabAgentApiError as exc:
        logger.warning("lab-agent: could not reach RESURGAM backend (file-index jobs): %s", exc)
        file_index_jobs = []

    for file_index_job in file_index_jobs:
        try:
            if file_index_job.file_index_method == "guest_exec_live":
                process_guest_exec_live_job(file_index_job, client)
            elif has_pbs_token:
                process_guest_exec_ephemeral_job(file_index_job, config, client)
        except LabAgentApiError as exc:
            logger.info("lab-agent: skipping file-index job %s: %s", file_index_job.id, exc)

    # Disk Mounts (ADR 0025): provision / tear down share helper VMs.
    try:
        disk_mount_jobs = client.list_disk_mount_jobs() if has_pbs_token else []
    except LabAgentApiError as exc:
        logger.warning("lab-agent: could not reach RESURGAM backend (disk-mount jobs): %s", exc)
        disk_mount_jobs = []

    for disk_mount_job in disk_mount_jobs:
        try:
            process_disk_mount_job(disk_mount_job, config, client)
        except LabAgentApiError as exc:
            logger.info("lab-agent: skipping disk-mount job %s: %s", disk_mount_job.id, exc)

    # Instant Recovery (ADR 0030): start, watch, finish, discard. A start runs
    # inline (a few minutes at most: map + overlays + create + start); the
    # disk moves themselves are PVE tasks driven by the backend.
    try:
        instant_jobs = client.list_instant_recovery_jobs() if has_pbs_token else []
    except LabAgentApiError as exc:
        logger.warning("lab-agent: could not reach RESURGAM backend (instant-recovery jobs): %s", exc)
        instant_jobs = []

    for instant_job in instant_jobs:
        process_instant_recovery_job(
            instant_job,
            config,
            client,
            state.instant_recovery_heartbeats,
            state.instant_recovery_probe_failures,
        )


def run_forever() -> None:
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    client = LabAgentClient(config.backend_url, config.agent_token, ca_file=config.ca_file)
    logger.info("resurgam-lab-agent starting (poll interval %.1fs)", config.poll_interval_seconds)
    if not config.pbs_token:
        logger.warning(
            "lab-agent: RESURGAM_LAB_AGENT_PBS_TOKEN is not set -- running template builds only; lab runs, "
            "ephemeral file indexing and disk mounts wait until it is set in /etc/default/resurgam-lab-agent"
        )

    state = PollState()
    while True:
        # Whatever goes wrong in one pass (a backend outage during an appliance
        # update, an unexpected response), the loop must survive: the process
        # dying would also kill a template build running in `state.setup_thread`.
        try:
            poll_once(config, client, state)
        except Exception:
            logger.exception("lab-agent: poll pass failed; retrying next interval")
        time.sleep(config.poll_interval_seconds)


if __name__ == "__main__":
    run_forever()
