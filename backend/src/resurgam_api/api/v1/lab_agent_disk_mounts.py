"""`resurgam-lab-agent`'s Disk Mounts backchannel (ADR 0025 §4).

Same auth as every `/lab-agent/*` endpoint: the cluster-scoped agent token
(`lab_agent._require_lab_agent`); a mount on another cluster is a 404. The
agent lists jobs, claims a provision job (the only response that carries the
share password and the PBS encryption key -- the password ciphertext is
deleted in the same transaction), reports every resource as it creates it,
and reports the outcome of provisioning and of teardown.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header
from sqlalchemy import or_
from sqlalchemy.orm import Session

from resurgam_api.api.v1.lab_agent import _require_lab_agent, _resolve_encryption_keyfile
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_db
from resurgam_api.disk_mounts import lifecycle
from resurgam_api.disk_mounts.helper_config import (
    disk_serial,
    helper_ipconfig,
    helper_nic,
    render_helper_config,
    render_user_data,
)
from resurgam_api.disk_mounts.snapshot_disks import snapshot_path
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.datastore import Datastore
from resurgam_api.models.disk_mount import DiskMount, DiskMountStatus
from resurgam_api.models.inventory import BackupGroup, BackupSnapshot
from resurgam_api.models.pbs import PbsInstance
from resurgam_api.models.pve import PveCluster
from resurgam_api.schemas.disk_mount import (
    DiskMountAgentJobOut,
    DiskMountClaimOut,
    DiskMountDiskOut,
    DiskMountProgressRequest,
    DiskMountStatusRequest,
    DiskMountTeardownStatusRequest,
)

router = APIRouter(prefix="/lab-agent", tags=["disk-mounts"])

MAX_ERROR_LENGTH = 4000
_LOOP_DEVICE_RE = re.compile(r"/dev/loop[0-9]{1,5}")


def _snapshot_path_for(db: Session, mount: DiskMount) -> str:
    group = db.get(BackupGroup, mount.backup_group_id)
    snapshot = db.get(BackupSnapshot, mount.backup_snapshot_id)
    return snapshot_path(group.backup_type, group.backup_id, snapshot.snapshot_time)


def _mount_for_agent(db: Session, mount_id: uuid.UUID, cluster: PveCluster) -> DiskMount:
    mount = db.query(DiskMount).filter_by(id=mount_id).with_for_update().one_or_none()
    if mount is None or mount.pve_cluster_id != cluster.id:
        raise ResurgamApiError(code="not_found", message="Job not found.", status_code=404)
    return mount


@router.get("/disk-mount-jobs", response_model=list[DiskMountAgentJobOut])
def list_disk_mount_jobs(
    db: Session = Depends(get_db), authorization: str | None = Header(default=None)
) -> list[DiskMountAgentJobOut]:
    """Provision jobs (`queued` mounts) and due teardown jobs (`stopping` mounts
    whose retry backoff has elapsed) on the authenticated agent's cluster. A
    provision job must be claimed before acting on it. No secrets here."""
    cluster = _require_lab_agent(db, authorization)
    now = datetime.now(UTC)
    mounts = (
        db.query(DiskMount)
        .filter(DiskMount.pve_cluster_id == cluster.id)
        .filter(
            or_(
                DiskMount.status == DiskMountStatus.QUEUED.value,
                (DiskMount.status == DiskMountStatus.STOPPING.value)
                & (or_(DiskMount.next_teardown_attempt_at.is_(None), DiskMount.next_teardown_attempt_at <= now)),
            )
        )
        .order_by(DiskMount.created_at)
        .all()
    )
    return [
        DiskMountAgentJobOut(
            id=m.id,
            kind="provision" if m.status == DiskMountStatus.QUEUED.value else "teardown",
            target_node=m.target_node,
            helper_name=lifecycle.helper_name(m.id),
            helper_vmid=m.helper_vmid,
            mapped_devices=list(m.mapped_devices or []),
            snippet_volume=m.snippet_volume,
            snapshot_path=_snapshot_path_for(db, m),
        )
        for m in mounts
    ]


@router.post("/disk-mount-jobs/{mount_id}/claim", response_model=DiskMountClaimOut)
def claim_disk_mount_job(
    mount_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
) -> DiskMountClaimOut:
    """`queued` -> `provisioning` (row-locked, so two agents can never both build
    it). Returns the helper's full build description incl. the share password,
    which is deleted from the database in the same transaction."""
    cluster = _require_lab_agent(db, authorization)
    mount = _mount_for_agent(db, mount_id, cluster)
    if mount.status != DiskMountStatus.QUEUED.value:
        raise ResurgamApiError(code="not_queued", message="This disk mount is not queued.", status_code=409)
    if cluster.disk_mount_helper_template_vmid is None or not cluster.lab_snippet_storage_id:
        lifecycle.finish(
            db,
            mount,
            DiskMountStatus.FAILED.value,
            error="The cluster's share helper template or snippet storage is no longer configured.",
        )
        lifecycle.record_audit(db, mount, "disk_mount_failed", actor_user_id=None, detail={"reason": "not_configured"})
        db.commit()
        raise ResurgamApiError(code="not_configured", message="Disk mounts are not configured.", status_code=409)

    group = db.get(BackupGroup, mount.backup_group_id)
    datastore = db.get(Datastore, group.datastore_id)
    pbs = db.get(PbsInstance, datastore.pbs_instance_id)
    password = lifecycle.decrypt_password(settings.secrets_local_encryption_key, mount.share_password_ciphertext)
    if "smb" in mount.protocols and password is None:
        lifecycle.finish(db, mount, DiskMountStatus.FAILED.value, error="The share password is no longer available.")
        lifecycle.record_audit(
            db, mount, "disk_mount_failed", actor_user_id=None, detail={"reason": "password_unavailable"}
        )
        db.commit()
        raise ResurgamApiError(code="password_unavailable", message="Share password unavailable.", status_code=409)

    config = render_helper_config(
        mount_id=str(mount.id),
        smb_user=mount.smb_username,
        protocols=list(mount.protocols),
        allowed_clients=list(mount.allowed_clients or []),
        archives=list(mount.disk_archives),
    )
    claim = DiskMountClaimOut(
        id=mount.id,
        target_node=mount.target_node,
        target_storage=mount.target_storage,
        helper_name=lifecycle.helper_name(mount.id),
        helper_template_vmid=cluster.disk_mount_helper_template_vmid,
        pbs_host=pbs.host,
        pbs_port=pbs.port,
        pbs_datastore=datastore.name,
        pbs_namespace=group.namespace or "",
        snapshot_path=_snapshot_path_for(db, mount),
        disks=[
            DiskMountDiskOut(archive=archive, serial=disk_serial(i), slot=f"scsi{i}")
            for i, archive in enumerate(mount.disk_archives, start=1)
        ],
        net0=helper_nic(mount.bridge, mount.vlan_tag),
        ipconfig0=helper_ipconfig(
            "dhcp" if mount.ip_mode == "dhcp" else "static", mount.ip_address_cidr, mount.gateway
        ),
        snippet_storage=cluster.lab_snippet_storage_id,
        snippet_filename=lifecycle.snippet_filename(mount.id),
        user_data=render_user_data(config, hostname=lifecycle.helper_name(mount.id)),
        smb_username=mount.smb_username,
        share_password=password,
        encryption_keyfile=_resolve_encryption_keyfile(db, settings, db.get(BackupSnapshot, mount.backup_snapshot_id)),
    )
    now = datetime.now(UTC)
    mount.status = DiskMountStatus.PROVISIONING.value
    mount.claimed_at = now
    mount.last_progress_at = now
    mount.share_password_ciphertext = None
    lifecycle.record_audit(db, mount, "disk_mount_provisioning_started", actor_user_id=None)
    db.commit()
    return claim


@router.post("/disk-mount-jobs/{mount_id}/progress", status_code=204)
def report_disk_mount_progress(
    mount_id: uuid.UUID,
    body: DiskMountProgressRequest,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> None:
    """Records a resource the agent just created (VMID before the clone, each
    mapped device, the snippet), so a teardown always knows what exists."""
    cluster = _require_lab_agent(db, authorization)
    mount = _mount_for_agent(db, mount_id, cluster)
    if mount.status not in (DiskMountStatus.PROVISIONING.value, DiskMountStatus.STOPPING.value):
        raise ResurgamApiError(code="not_provisioning", message="This disk mount is not provisioning.", status_code=409)
    if body.helper_vmid is not None:
        mount.helper_vmid = body.helper_vmid
    if body.mapped_device is not None:
        archive = str(body.mapped_device.get("archive") or "")
        device = str(body.mapped_device.get("device") or "")
        if not _LOOP_DEVICE_RE.fullmatch(device):
            raise ResurgamApiError(
                code="invalid_device", message="mapped_device.device must be /dev/loopN.", status_code=422
            )
        mount.mapped_devices = [*(mount.mapped_devices or []), {"archive": archive, "device": device}]
    if body.snippet_volume is not None:
        # Only this mount's own snippet file: a teardown deletes this path on the
        # node, so an arbitrary volume here would be an arbitrary-file delete.
        _, _, path = body.snippet_volume.partition(":")
        if path != f"snippets/{lifecycle.snippet_filename(mount.id)}":
            raise ResurgamApiError(
                code="invalid_snippet_volume",
                message="snippet_volume must be <storage>:snippets/" + lifecycle.snippet_filename(mount.id) + ".",
                status_code=422,
            )
        mount.snippet_volume = body.snippet_volume
    mount.last_progress_at = datetime.now(UTC)
    db.commit()


def _clean_volumes(volumes: list[dict]) -> list[dict]:
    keys = ("disk_index", "archive", "partition", "fstype", "size_bytes", "name", "mounted", "reason", "detail")
    return [{k: v.get(k) for k in keys} for v in volumes if isinstance(v, dict)]


@router.post("/disk-mount-jobs/{mount_id}/status", status_code=204)
def report_disk_mount_status(
    mount_id: uuid.UUID,
    body: DiskMountStatusRequest,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> None:
    """Outcome of provisioning. `ready` starts the lifetime clock. `failed` with
    `cleaned_up` ends the mount; without it the mount goes to `stopping` so the
    leftovers are torn down. A mount stopped meanwhile stays `stopping`."""
    cluster = _require_lab_agent(db, authorization)
    mount = _mount_for_agent(db, mount_id, cluster)
    now = datetime.now(UTC)
    if mount.status not in (DiskMountStatus.PROVISIONING.value, DiskMountStatus.STOPPING.value):
        raise ResurgamApiError(code="not_provisioning", message="This disk mount is not provisioning.", status_code=409)
    stopped_meanwhile = mount.status == DiskMountStatus.STOPPING.value
    mount.last_progress_at = now
    if body.status == "ready":
        mount.helper_addresses = [a for a in body.addresses if isinstance(a, str)][:16]
        mount.volumes = _clean_volumes(body.volumes)
        if not stopped_meanwhile:
            mount.status = DiskMountStatus.READY.value
            mount.ready_at = now
            mount.expires_at = now + timedelta(hours=mount.lifetime_hours)
            lifecycle.record_audit(
                db,
                mount,
                "disk_mount_ready",
                actor_user_id=None,
                detail={
                    "helper_vmid": mount.helper_vmid,
                    "addresses": mount.helper_addresses,
                    "mounted_volumes": [v["name"] for v in mount.volumes if v.get("mounted")],
                    "expires_at": mount.expires_at.isoformat(),
                },
            )
        db.commit()
        return

    error = (body.error or "Provisioning failed.")[:MAX_ERROR_LENGTH]
    if body.cleaned_up:
        mount.helper_vmid = None
        mount.mapped_devices = []
        mount.snippet_volume = None
        final = mount.teardown_reason if stopped_meanwhile and mount.teardown_reason else DiskMountStatus.FAILED.value
        lifecycle.finish(db, mount, final, now=now, error=error)
    else:
        lifecycle.request_teardown(db, mount, DiskMountStatus.FAILED.value, now=now, error=error)
    lifecycle.record_audit(
        db, mount, "disk_mount_failed", actor_user_id=None, detail={"error": error, "cleaned_up": body.cleaned_up}
    )
    db.commit()


@router.post("/disk-mount-jobs/{mount_id}/teardown-status", status_code=204)
def report_disk_mount_teardown_status(
    mount_id: uuid.UUID,
    body: DiskMountTeardownStatusRequest,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> None:
    """Outcome of a teardown. `done`: helper destroyed, disks unmapped, snippet
    deleted -- the mount ends (stopped / expired / failed) and a range address is
    freed. `failed`: stays `stopping`, retried with backoff (1, 2, 4 ... 15
    minutes); after `TEARDOWN_MAX_ATTEMPTS` (10) failures the mount ends `failed`
    with the leftovers named in its error, and its address is freed."""
    cluster = _require_lab_agent(db, authorization)
    mount = _mount_for_agent(db, mount_id, cluster)
    if mount.status != DiskMountStatus.STOPPING.value:
        raise ResurgamApiError(code="not_stopping", message="This disk mount is not being torn down.", status_code=409)
    now = datetime.now(UTC)
    if body.status == "done":
        reason = mount.teardown_reason or DiskMountStatus.STOPPED.value
        mount.helper_vmid = None
        mount.mapped_devices = []
        mount.snippet_volume = None
        lifecycle.finish(db, mount, reason, now=now)
        lifecycle.record_audit(db, mount, "disk_mount_cleaned_up", actor_user_id=None, detail={"final_status": reason})
    else:
        error = (body.error or "Teardown failed.")[:MAX_ERROR_LENGTH]
        gave_up = lifecycle.teardown_failed(db, mount, error, now=now)
        lifecycle.record_audit(
            db,
            mount,
            "disk_mount_cleanup_gave_up" if gave_up else "disk_mount_cleanup_failed",
            actor_user_id=None,
            detail={"error": error, "attempt": mount.teardown_attempts},
        )
    db.commit()
