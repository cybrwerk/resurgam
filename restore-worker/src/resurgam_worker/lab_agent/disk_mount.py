"""Disk Mounts on the lab node (ADR 0025): map a VM backup's disks read-only,
attach them to a freshly cloned share helper VM, wait until it shares them, and
tear everything down again.

Every resource is reported to the backend the moment it exists (mapped device,
VMID, snippet), so a teardown after any crash knows what to remove. A failing
provisioning step undoes what this run created before reporting `failed`.

The share password arrives only in the claim response, lives only in this
process's memory and reaches the helper through `qm guest exec --pass-stdin`
-- never a file, a command line or a log line.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import time
from collections.abc import Callable

from resurgam_worker.lab_agent.client import DiskMountClaim, DiskMountJob, LabAgentApiError, LabAgentClient
from resurgam_worker.lab_agent.config import LabAgentConfig
from resurgam_worker.pbs_client.errors import PbsClientError
from resurgam_worker.pbs_client.map import list_mappings, list_snapshot_archives, map_snapshot, unmap
from resurgam_worker.pve_client.errors import PveClientError
from resurgam_worker.pve_client.pvesm import resolve_storage_path
from resurgam_worker.pve_client.qm import (
    clone_vm,
    delete_vm_options,
    destroy_vm,
    get_vm_config,
    guest_exec,
    guest_exec_with_stdin,
    list_cluster_vmids,
    next_free_vmid,
    set_vm_options,
    start_vm,
    stop_vm,
    wait_for_guest_agent,
)

logger = logging.getLogger("resurgam_worker.lab_agent.disk_mount")

HELPER_TAG = "resurgam-disk-mount"
HELPER_SCRIPT = "/usr/local/sbin/resurgam-share-helper"
# Run through the interpreter, the way the helper's own cloud-init `runcmd`
# does. Found live 2026-11-21: the script carried no shebang, so executing it
# directly through the guest agent failed and every SMB mount ended with
# "Setting the SMB password inside the helper failed."
HELPER_COMMAND = ["/usr/bin/python3", HELPER_SCRIPT]
HELPER_RESULT = "/run/resurgam-share-helper/result.json"
_RESULT_POLL_SECONDS = 5.0
# What the backend accepts in the `ready` report (`DiskMountStatusRequest`): a
# longer list would be a 422 and turn a healthy helper into a failed mount.
MAX_REPORTED_ADDRESSES = 16
MAX_REPORTED_VOLUMES = 256
_STORAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-_.]{0,62}")
_SNIPPET_FILENAME_RE = re.compile(r"resurgam-[a-z0-9-]{1,80}\.yaml")
# The only snippet volumes a teardown deletes: RESURGAM's own snippet files.
_SNIPPET_VOLUME_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-_.]{0,62}:snippets/resurgam-[a-z0-9-]{1,80}\.yaml")


class DiskMountError(Exception):
    pass


def local_node_name(config: LabAgentConfig) -> str:
    return config.node_name or socket.gethostname().split(".")[0]


def pbs_repository(pbs_token_id: str, claim: DiskMountClaim) -> str:
    """`user@host:port:datastore` -- the port belongs inside the repository
    (PBS rejects `--repository` together with `--port`, ADR 0019)."""
    return f"{pbs_token_id}@{claim.pbs_host}:{claim.pbs_port}:{claim.pbs_datastore}"


def mapped_drive_value(device: str, serial: str) -> str:
    """The helper's extra drive: the mapped loop device, read-only for QEMU
    (qm.conf `ro`: "Whether the drive is read-only"), identified by `serial`
    inside the guest, and never part of a vzdump backup (`backup=0`). A bare
    `/dev/loopN` path is accepted by `qm` for root (live-proven, ADR 0019)."""
    return f"{device},ro=1,serial={serial},backup=0"


def helper_options(claim: DiskMountClaim, snippet_volume: str, devices: dict[str, str]) -> dict[str, str]:
    """`qm set` options for the clone: network, cloud-init, tags, and one
    read-only drive per mapped disk. The template's boot order (`scsi0`) is left
    alone, so the helper never tries to boot a backup disk."""
    options = {
        "net0": claim.net0,
        "ipconfig0": claim.ipconfig0,
        "cicustom": f"user={snippet_volume}",
        "tags": HELPER_TAG,
        "agent": "enabled=1",
    }
    for disk in claim.disks:
        options[disk.slot] = mapped_drive_value(devices[disk.archive], disk.serial)
    return options


def helper_description(claim: DiskMountClaim) -> str:
    return (
        f"RESURGAM disk mount {claim.id} -- created and removed automatically by resurgam-lab-agent. "
        "Shares a backup's disks read-only; do not edit."
    )


def _read_helper_result(vmid: int) -> dict | None:
    status = guest_exec(vmid, ["cat", HELPER_RESULT], timeout_seconds=30, poll_interval_seconds=1)
    if status.get("exitcode") != 0:
        return None
    try:
        payload = json.loads(status.get("out-data") or "")
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def wait_for_helper_result(
    vmid: int,
    *,
    timeout_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    deadline = monotonic() + timeout_seconds
    while True:
        try:
            result = _read_helper_result(vmid)
        except PveClientError:
            result = None
        if result is not None:
            return result
        if monotonic() >= deadline:
            raise DiskMountError(
                f"The helper VM {vmid} wrote no result within {int(timeout_seconds // 60)} minutes (cloud-init did "
                f"not run the setup, or it hangs). Check its console: qm terminal {vmid}."
            )
        sleep(_RESULT_POLL_SECONDS)


def _destroy_helper(vmid: int, expected_name: str) -> None:
    """Stops and destroys the helper -- only if that VMID still is this mount's
    helper (name check), so a stale VMID never destroys someone else's VM. A VMID
    that is gone or now carries another name (deleted by hand, VMID reused) means
    the helper is already gone."""
    if vmid not in list_cluster_vmids():
        return
    config = get_vm_config(vmid)
    if config.get("name") != expected_name:
        logger.warning(
            "disk-mount: VM %s is named %r, not %r -- treating the helper as already gone (not touching that VM)",
            vmid,
            config.get("name"),
            expected_name,
        )
        return
    try:
        stop_vm(vmid)
    except PveClientError:
        logger.info("disk-mount: stop of VM %s failed (probably not running)", vmid, exc_info=True)
    loop_slots = [key for key, value in config.items() if isinstance(value, str) and value.startswith("/dev/loop")]
    delete_vm_options(vmid, loop_slots)
    destroy_vm(vmid, purge=True)


def _unmap_if_ours(device: str, archive: str, snapshot_path: str, mappings: dict[str, str]) -> None:
    name = mappings.get(device)
    if name is None:
        return  # already gone (unmapped, or the node rebooted)
    expected = f"{snapshot_path}/{archive.removesuffix('.fidx')}"
    if expected not in name:
        # The loop device was reused (node rebooted, mapping gone): ours is gone.
        logger.warning(
            "disk-mount: %s now maps %r, not %r -- treating this mount's mapping as already gone",
            device,
            name,
            expected,
        )
        return
    unmap(device)


def is_own_snippet_volume(snippet_volume: str) -> bool:
    return bool(_SNIPPET_VOLUME_RE.fullmatch(snippet_volume))


def _delete_snippet(snippet_volume: str) -> None:
    """Deletes one of RESURGAM's own snippet files; refuses any other volume (the
    value comes from the backend, which must never be able to make the agent
    delete arbitrary files)."""
    if not is_own_snippet_volume(snippet_volume):
        raise DiskMountError(f"Refused to delete {snippet_volume!r}: not a RESURGAM disk-mount snippet.")
    path = resolve_storage_path(snippet_volume)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def provision(
    job: DiskMountJob,
    config: LabAgentConfig,
    client: LabAgentClient,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    claim = client.claim_disk_mount_job(job.id)
    logger.info("disk-mount: claimed %s (helper %s)", claim.id, claim.helper_name)
    pbs_token_id, _, pbs_secret = config.pbs_token.partition(":")
    repository = pbs_repository(pbs_token_id, claim)
    namespace = claim.pbs_namespace or ""

    devices: dict[str, str] = {}
    vmid: int | None = None
    snippet_volume: str | None = None
    snippet_path: str | None = None
    try:
        node = local_node_name(config)
        if claim.target_node != node:
            raise DiskMountError(
                f"This disk mount targets node {claim.target_node!r}, but resurgam-lab-agent runs on {node!r}; the "
                "backup's disks can only be mapped on the agent's node. Create the mount for that node."
            )
        archives = list_snapshot_archives(
            repository, claim.snapshot_path, namespace=namespace, token=pbs_secret, keyfile=claim.encryption_keyfile
        )
        missing = [d.archive for d in claim.disks if d.archive not in archives]
        if missing:
            raise DiskMountError(f"The snapshot has no disk {', '.join(missing)} (found: {', '.join(archives)}).")

        for disk in claim.disks:
            mapped = map_snapshot(
                repository,
                claim.snapshot_path,
                disk.archive,
                namespace=namespace,
                token=pbs_secret,
                keyfile=claim.encryption_keyfile,
            )
            devices[disk.archive] = mapped.device_path
            client.report_disk_mount_progress(
                claim.id, mapped_device={"archive": disk.archive, "device": mapped.device_path}
            )
            logger.info("disk-mount: %s mapped %s as %s", claim.id, disk.archive, mapped.device_path)

        vmid = next_free_vmid()
        client.report_disk_mount_progress(claim.id, helper_vmid=vmid)
        try:
            clone_vm(claim.helper_template_vmid, vmid, name=claim.helper_name, description=helper_description(claim))
        except PveClientError:
            logger.info("disk-mount: linked clone failed, trying a full clone", exc_info=True)
            if vmid in list_cluster_vmids():
                raise
            clone_vm(
                claim.helper_template_vmid,
                vmid,
                name=claim.helper_name,
                description=helper_description(claim),
                full=True,
                storage=claim.target_storage,
            )

        if not _STORAGE_RE.fullmatch(claim.snippet_storage) or not _SNIPPET_FILENAME_RE.fullmatch(
            claim.snippet_filename
        ):
            raise DiskMountError("The claim's snippet storage or file name is malformed; refusing to write it.")
        snippet_volume = f"{claim.snippet_storage}:snippets/{claim.snippet_filename}"
        snippet_path = resolve_storage_path(snippet_volume)
        client.report_disk_mount_progress(claim.id, snippet_volume=snippet_volume)
        os.makedirs(os.path.dirname(snippet_path), exist_ok=True)
        with open(snippet_path, "w", encoding="utf-8") as fh:
            fh.write(claim.user_data)

        set_vm_options(vmid, helper_options(claim, snippet_volume, devices))
        start_vm(vmid)
        wait_for_guest_agent(vmid, timeout_seconds=config.disk_mount_ready_timeout_seconds)
        result = wait_for_helper_result(
            vmid, timeout_seconds=config.disk_mount_ready_timeout_seconds, sleep=sleep, monotonic=monotonic
        )
        if result.get("status") != "ok":
            raise DiskMountError(f"The helper's setup failed: {result.get('error') or 'no detail'}")

        if claim.smb_username and claim.share_password:
            outcome = guest_exec_with_stdin(
                vmid, [*HELPER_COMMAND, "set-smb-password", claim.smb_username], f"{claim.share_password}\n"
            )
            if outcome.get("exitcode") != 0:
                raise DiskMountError("Setting the SMB password inside the helper failed.")

        # PVE built the cloud-init ISO at start; the snippet is no longer needed.
        _delete_snippet(snippet_volume)
        client.report_disk_mount_status(
            claim.id,
            "ready",
            addresses=[a for a in result.get("addresses") or [] if isinstance(a, str)][:MAX_REPORTED_ADDRESSES],
            volumes=[v for v in result.get("volumes") or [] if isinstance(v, dict)][:MAX_REPORTED_VOLUMES],
        )
        logger.info("disk-mount: %s ready on VM %s", claim.id, vmid)
    except Exception as exc:  # whatever failed, undo what this run created
        logger.warning("disk-mount: provisioning %s failed: %s", claim.id, exc, exc_info=True)
        cleaned = _cleanup_after_failure(claim, vmid, devices, snippet_path)
        try:
            client.report_disk_mount_status(claim.id, "failed", error=str(exc), cleaned_up=cleaned)
        except LabAgentApiError:
            logger.warning("disk-mount: could not report the failure of %s", claim.id, exc_info=True)


def _cleanup_after_failure(
    claim: DiskMountClaim, vmid: int | None, devices: dict[str, str], snippet_path: str | None
) -> bool:
    """Undoes what this provisioning run created; True if everything is gone.
    The mapped devices stay while the helper VM may still have them attached."""
    clean = True
    if vmid is not None:
        try:
            _destroy_helper(vmid, claim.helper_name)
        except (DiskMountError, PveClientError):
            logger.warning("disk-mount: could not remove helper VM %s", vmid, exc_info=True)
            clean = False
    for device in devices.values() if clean else ():
        try:
            unmap(device)
        except PbsClientError:
            logger.warning("disk-mount: could not unmap %s", device, exc_info=True)
            clean = False
    if snippet_path:
        try:
            os.remove(snippet_path)
        except FileNotFoundError:
            pass
        except OSError:
            clean = False
    return clean


def teardown(job: DiskMountJob, client: LabAgentClient) -> None:
    """Removes what the mount recorded: the helper VM (only if it still carries
    this mount's name), each mapped device (only if it still maps this mount's
    snapshot), the snippet. Reports `done` or `failed` (retried by the backend).
    Resources that are already gone count as removed. While the helper VM could
    not be destroyed its disks stay mapped (it may still have them attached)."""
    errors: list[str] = []
    helper_gone = True
    if job.helper_vmid is not None:
        try:
            _destroy_helper(job.helper_vmid, job.helper_name)
        except (DiskMountError, PveClientError) as exc:
            errors.append(f"helper VM {job.helper_vmid}: {exc}")
            helper_gone = False
    if job.mapped_devices and not helper_gone:
        errors.append("disks left mapped until the helper VM is removed")
    elif job.mapped_devices:
        try:
            mappings = list_mappings()
        except PbsClientError as exc:
            errors.append(f"could not list mappings: {exc}")
            mappings = None
        if mappings is not None:
            for entry in job.mapped_devices:
                device = str(entry.get("device") or "")
                if not device:
                    continue
                try:
                    _unmap_if_ours(device, str(entry.get("archive") or ""), job.snapshot_path, mappings)
                except (DiskMountError, PbsClientError) as exc:
                    errors.append(f"{device}: {exc}")
    if job.snippet_volume:
        try:
            _delete_snippet(job.snippet_volume)
        except (DiskMountError, PveClientError, OSError) as exc:
            errors.append(f"snippet {job.snippet_volume}: {exc}")
    if errors:
        logger.warning("disk-mount: teardown of %s incomplete: %s", job.id, errors)
        client.report_disk_mount_teardown(job.id, "failed", error="; ".join(errors))
    else:
        logger.info("disk-mount: %s torn down", job.id)
        client.report_disk_mount_teardown(job.id, "done")


def process_disk_mount_job(job: DiskMountJob, config: LabAgentConfig, client: LabAgentClient) -> None:
    if job.kind == "provision":
        provision(job, config, client)
    else:
        teardown(job, client)
