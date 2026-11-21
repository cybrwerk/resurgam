"""Disk Mounts (ADR 0025): a backup's disks, mapped read-only on a lab node,
shared by a disposable helper VM over SMB and/or NFS for a limited time.

Who: `restore_operator` for the backup's tenant, or `platform_admin` -- the
same bar as file restore and restore plans (`auth/scope.py`). When the lab
cluster has `require_restore_approval`, a second operator (never the
requester) approves first, exactly like restore plans. The share password is
returned once by `POST /disk-mounts` and never again.
"""

import ipaddress
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from resurgam_api.api.v1._connection_helpers import upstream_error
from resurgam_api.auth.dependency import get_current_user, require_role
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.scope import (
    effective_group_customer_id,
    require_restore_access,
    require_restore_access_for_customer,
    restore_visible_customer_ids,
)
from resurgam_api.config import Settings, get_settings
from resurgam_api.connectivity.models import TrustConfig
from resurgam_api.connectivity.pve_api import list_nodes
from resurgam_api.connectivity.pve_lab_api import PveApiError, list_node_bridges
from resurgam_api.db import get_db
from resurgam_api.disk_mounts import lifecycle
from resurgam_api.disk_mounts.snapshot_disks import SnapshotFilesError, list_snapshot_disks
from resurgam_api.errors import ResurgamApiError
from resurgam_api.inventory.workload_label import workload_label
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.datastore import Datastore, DatastoreCustomerAssignment
from resurgam_api.models.disk_mount import ACTIVE_STATUSES, DiskMount, DiskMountStatus
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.inventory import BackupGroup, BackupSnapshot
from resurgam_api.models.pbs import PbsInstance, TlsTrustMode
from resurgam_api.models.pve import PveCluster
from resurgam_api.models.secrets import SecretReference
from resurgam_api.models.tenancy import Customer
from resurgam_api.restore_test.lab_readiness import LAB_AGENT_RECENT_WINDOW
from resurgam_api.schemas.disk_mount import (
    DiskMountConnectionOut,
    DiskMountCreatedOut,
    DiskMountCreateRequest,
    DiskMountExtendRequest,
    DiskMountOut,
    DiskMountPlacementOut,
    DiskMountRejectRequest,
    DiskMountVolumeOut,
    HelperTemplateUpdateRequest,
    SnapshotDiskOut,
)
from resurgam_api.schemas.lab_setup import PveBridgeOut
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

router = APIRouter(prefix="/disk-mounts", tags=["disk-mounts"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)
SMB_SHARE_NAME = "restore"
NFS_EXPORT_ROOT = "/srv/resurgam"


# --- helpers -------------------------------------------------------------------------------


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


def _pbs_trust_and_token(db: Session, settings: Settings, instance: PbsInstance) -> tuple[TrustConfig, str]:
    secret_ref = db.get(SecretReference, instance.api_token_secret_ref_id)
    token = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key).retrieve(secret_ref.reference_key)
    trust = TrustConfig(
        trust_mode=TlsTrustMode(instance.tls_trust_mode), fingerprint_sha256=instance.tls_fingerprint_sha256
    )
    return trust, token


def _pve_trust_and_token(db: Session, settings: Settings, cluster: PveCluster) -> tuple[TrustConfig, str]:
    secret_ref = db.get(SecretReference, cluster.api_token_secret_ref_id)
    token = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key).retrieve(secret_ref.reference_key)
    trust = TrustConfig(
        trust_mode=TlsTrustMode(cluster.tls_trust_mode), fingerprint_sha256=cluster.tls_fingerprint_sha256
    )
    return trust, token


def _snapshot_context(db: Session, snapshot_id: uuid.UUID) -> tuple[BackupSnapshot, BackupGroup, Datastore]:
    snapshot = db.get(BackupSnapshot, snapshot_id)
    if snapshot is None:
        raise ResurgamApiError(code="not_found", message="Snapshot not found.", status_code=404)
    group = db.get(BackupGroup, snapshot.backup_group_id)
    datastore = db.get(Datastore, group.datastore_id)
    return snapshot, group, datastore


def _require_vm(group: BackupGroup) -> None:
    if group.backup_type != "vm":
        raise ResurgamApiError(
            code="disk_mount_unsupported_backup_type",
            message="Disk mounts need a VM backup (disk images). Container and host backups are file archives "
            "(pxar): open them in Recovery -> Files to browse and download files or folders as ZIP.",
            status_code=400,
        )


def _snapshot_disks(db: Session, settings: Settings, snapshot, group, datastore):
    instance = db.get(PbsInstance, datastore.pbs_instance_id)
    trust, token = _pbs_trust_and_token(db, settings, instance)
    try:
        return list_snapshot_disks(
            instance.host,
            instance.port,
            trust,
            token,
            settings.connectivity_timeout_seconds,
            datastore=datastore.name,
            namespace=group.namespace or "",
            backup_type=group.backup_type,
            backup_id=group.backup_id,
            snapshot_time=snapshot.snapshot_time,
        )
    except SnapshotFilesError as exc:
        raise upstream_error("pbs", str(exc)) from exc


def placement_problems(cluster: PveCluster, *, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(UTC)
    problems = []
    if cluster.disabled:
        problems.append("cluster_disabled")
    if cluster.lab_agent_token_hash is None:
        problems.append("agent_token_missing")
    elif cluster.lab_agent_last_seen_at is None or now - cluster.lab_agent_last_seen_at > LAB_AGENT_RECENT_WINDOW:
        problems.append("agent_not_seen")
    if not cluster.lab_snippet_storage_id:
        problems.append("snippet_storage_missing")
    if cluster.disk_mount_helper_template_vmid is None:
        problems.append("helper_template_missing")
    return problems


def _lab_cluster_or_404(db: Session, cluster_id: uuid.UUID) -> PveCluster:
    cluster = db.get(PveCluster, cluster_id)
    if cluster is None or not cluster.is_lab_compute:
        raise ResurgamApiError(code="not_found", message="Lab cluster not found.", status_code=404)
    return cluster


def _require_any_restore_access(user: AuthenticatedUser) -> None:
    if not user.has_role(RoleKey.PLATFORM_ADMIN) and not restore_visible_customer_ids(user):
        raise ResurgamApiError(code="not_found", message="Not found.", status_code=404)


def _mount_or_404(db: Session, mount_id: uuid.UUID, user: AuthenticatedUser, *, for_update: bool = False) -> DiskMount:
    query = db.query(DiskMount).filter_by(id=mount_id)
    if for_update:
        query = query.with_for_update()
    mount = query.one_or_none()
    if mount is None:
        raise ResurgamApiError(code="not_found", message="Disk mount not found.", status_code=404)
    try:
        require_restore_access_for_customer(user, mount.customer_id)
    except ResurgamApiError:
        raise ResurgamApiError(code="not_found", message="Disk mount not found.", status_code=404) from None
    return mount


def connection_info(mount: DiskMount) -> DiskMountConnectionOut:
    host = None
    if mount.status == DiskMountStatus.READY.value:
        if mount.ip_address_cidr:
            host = str(ipaddress.IPv4Interface(mount.ip_address_cidr).ip)
        elif mount.helper_addresses:
            host = mount.helper_addresses[0]
    smb = host is not None and "smb" in mount.protocols
    nfs = host is not None and "nfs" in mount.protocols
    return DiskMountConnectionOut(
        host=host,
        smb_unc=f"\\\\{host}\\{SMB_SHARE_NAME}" if smb else None,
        smb_url=f"smb://{host}/{SMB_SHARE_NAME}" if smb else None,
        smb_username=mount.smb_username if "smb" in mount.protocols else None,
        nfs_exports=[
            f"{host}:{NFS_EXPORT_ROOT}/{v['name']}" for v in mount.volumes if v.get("mounted") and v.get("name")
        ]
        if nfs
        else [],
    )


def mount_out(db: Session, mount: DiskMount) -> DiskMountOut:
    group = db.get(BackupGroup, mount.backup_group_id)
    snapshot = db.get(BackupSnapshot, mount.backup_snapshot_id)
    cluster = db.get(PveCluster, mount.pve_cluster_id)
    customer = db.get(Customer, mount.customer_id) if mount.customer_id else None
    volumes = []
    for v in mount.volumes or []:
        try:
            volumes.append(DiskMountVolumeOut.model_validate(v))
        except ValueError:
            continue
    return DiskMountOut(
        id=mount.id,
        status=mount.status,
        teardown_reason=mount.teardown_reason,
        customer_id=mount.customer_id,
        customer_name=customer.name if customer else None,
        backup_group_id=mount.backup_group_id,
        backup_snapshot_id=mount.backup_snapshot_id,
        snapshot_time=snapshot.snapshot_time if snapshot else None,
        workload_label=f"{group.backup_type}/{workload_label(group)}" if group else None,
        disk_archives=list(mount.disk_archives),
        pve_cluster_id=mount.pve_cluster_id,
        pve_cluster_name=cluster.display_name if cluster else None,
        target_node=mount.target_node,
        bridge=mount.bridge,
        vlan_tag=mount.vlan_tag,
        ip_mode=mount.ip_mode,
        ip_address_cidr=mount.ip_address_cidr,
        gateway=mount.gateway,
        protocols=list(mount.protocols),
        allowed_clients=list(mount.allowed_clients or []),
        smb_username=mount.smb_username,
        lifetime_hours=mount.lifetime_hours,
        created_at=mount.created_at,
        created_by_user_id=mount.created_by_user_id,
        approval_required=mount.approval_required,
        approved_by_user_id=mount.approved_by_user_id,
        approved_at=mount.approved_at,
        approval_notes=mount.approval_notes,
        ready_at=mount.ready_at,
        expires_at=mount.expires_at,
        ended_at=mount.ended_at,
        error=mount.error,
        helper_vmid=mount.helper_vmid,
        volumes=volumes,
        connection=connection_info(mount),
    )


def _audit_settings(mount: DiskMount) -> dict:
    """What a mount was created with -- for the audit trail. No secrets."""
    return {
        "backup_snapshot_id": str(mount.backup_snapshot_id),
        "backup_group_id": str(mount.backup_group_id),
        "disk_archives": list(mount.disk_archives),
        "pve_cluster_id": str(mount.pve_cluster_id),
        "target_node": mount.target_node,
        "bridge": mount.bridge,
        "vlan_tag": mount.vlan_tag,
        "ip_mode": mount.ip_mode,
        "ip_address_cidr": mount.ip_address_cidr,
        "protocols": list(mount.protocols),
        "allowed_clients": list(mount.allowed_clients or []),
        "smb_username": mount.smb_username,
        "lifetime_hours": mount.lifetime_hours,
    }


# --- lookups for the "new disk mount" flow ---------------------------------------------------


@router.get("/placement", response_model=list[DiskMountPlacementOut])
def list_disk_mount_placement(
    db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> list[DiskMountPlacementOut]:
    """Lab clusters (`is_lab_compute`) a disk mount can run on, each with what is
    still missing (`problems`, empty = `ready`). Needs restore access for at least
    one tenant (or platform admin)."""
    _require_any_restore_access(user)
    clusters = db.query(PveCluster).filter_by(is_lab_compute=True).order_by(PveCluster.display_name).all()
    out = []
    for cluster in clusters:
        problems = placement_problems(cluster)
        out.append(
            DiskMountPlacementOut(
                pve_cluster_id=cluster.id,
                display_name=cluster.display_name,
                ready=not problems,
                problems=problems,
                require_approval=cluster.require_restore_approval,
                helper_template_vmid=cluster.disk_mount_helper_template_vmid,
                lab_agent_last_seen_at=cluster.lab_agent_last_seen_at,
                lab_agent_node=cluster.lab_agent_node,
                uplink_bridge=cluster.lab_appliance_uplink_bridge,
                uplink_vlan_tag=cluster.lab_appliance_uplink_vlan_tag,
                uplink_static_range=cluster.lab_appliance_uplink_ip_mode == "static_range",
            )
        )
    return out


@router.get("/placement/{cluster_id}/nodes", response_model=list[str])
def list_disk_mount_nodes(
    cluster_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[str]:
    """Node names of a lab cluster (live, `GET /nodes`). The helper must run on the
    node `resurgam-lab-agent` is installed on."""
    _require_any_restore_access(user)
    cluster = _lab_cluster_or_404(db, cluster_id)
    trust, token = _pve_trust_and_token(db, settings, cluster)
    result = list_nodes(cluster.host, cluster.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    if not result.reachable:
        raise upstream_error("pve", result.error)
    return sorted(n.node for n in result.nodes)


@router.get("/placement/{cluster_id}/nodes/{node}/bridges", response_model=list[PveBridgeOut])
def list_disk_mount_bridges(
    cluster_id: uuid.UUID,
    node: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[PveBridgeOut]:
    """Bridges of one node (live, `GET /nodes/{node}/network?type=any_local_bridge`)
    for the helper's network card."""
    _require_any_restore_access(user)
    cluster = _lab_cluster_or_404(db, cluster_id)
    trust, token = _pve_trust_and_token(db, settings, cluster)
    try:
        bridges = list_node_bridges(
            cluster.host, cluster.port, trust, token, settings.connectivity_timeout_seconds, node=node
        )
    except PveApiError as exc:
        raise upstream_error("pve", str(exc)) from exc
    return [PveBridgeOut(**vars(b)) for b in bridges]


@router.put("/placement/{cluster_id}/helper-template", response_model=DiskMountPlacementOut)
def set_disk_mount_helper_template(
    cluster_id: uuid.UUID,
    body: HelperTemplateUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(_require_platform_admin),
) -> DiskMountPlacementOut:
    """Selects (or clears) the share helper template of a lab cluster -- normally
    set by the guided build (`POST /pve-clusters/{id}/lab-setup/share-helper-template-build-jobs`).
    Platform admin only. Audited."""
    cluster = _lab_cluster_or_404(db, cluster_id)
    previous = cluster.disk_mount_helper_template_vmid
    cluster.disk_mount_helper_template_vmid = body.helper_template_vmid
    db.add(
        AuditEvent(
            actor_user_id=user.user_id,
            action="pve_cluster_disk_mount_helper_template_set",
            target_type="pve_cluster",
            target_id=str(cluster.id),
            correlation_id=_correlation_id(request),
            detail={"vmid": body.helper_template_vmid, "previous_vmid": previous},
        )
    )
    db.commit()
    return next(p for p in list_disk_mount_placement(db, user) if p.pve_cluster_id == cluster.id)


@router.get("/snapshot-disks", response_model=list[SnapshotDiskOut])
def list_disk_mount_snapshot_disks(
    backup_snapshot_id: uuid.UUID = Query(description="The VM backup snapshot to list disks of."),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[SnapshotDiskOut]:
    """The disk archives (`drive-*.img.fidx`) of a VM backup snapshot, live from PBS
    (`GET /admin/datastore/{store}/files`), to pick which disks to mount. 400 for a
    container/host backup."""
    snapshot, group, datastore = _snapshot_context(db, backup_snapshot_id)
    require_restore_access(db, user, group)
    _require_vm(group)
    return [
        SnapshotDiskOut(archive=d.archive, slot=d.slot, size_bytes=d.size_bytes, crypt_mode=d.crypt_mode)
        for d in _snapshot_disks(db, settings, snapshot, group, datastore)
    ]


# --- the mounts ----------------------------------------------------------------------------


@router.get("", response_model=list[DiskMountOut])
def list_disk_mounts(
    active_only: bool = Query(default=False, description="Only mounts that are not finished yet."),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[DiskMountOut]:
    """Disk mounts of every tenant the caller holds restore access for (all for
    platform admins), newest first. Never contains share passwords."""
    query = db.query(DiskMount)
    if not user.has_role(RoleKey.PLATFORM_ADMIN):
        query = query.filter(DiskMount.customer_id.in_(restore_visible_customer_ids(user)))
    if active_only:
        query = query.filter(DiskMount.status.in_(ACTIVE_STATUSES))
    return [mount_out(db, m) for m in query.order_by(DiskMount.created_at.desc()).limit(500).all()]


@router.get("/{mount_id}", response_model=DiskMountOut)
def get_disk_mount(
    mount_id: uuid.UUID, db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> DiskMountOut:
    """One disk mount, incl. connection info once ready. Never the password."""
    return mount_out(db, _mount_or_404(db, mount_id, user))


@router.post("", response_model=DiskMountCreatedOut, status_code=201)
def create_disk_mount(
    body: DiskMountCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> DiskMountCreatedOut:
    """Requests a disk mount: the selected disks of a VM backup, mapped read-only
    on the lab node by `resurgam-lab-agent` and shared by a helper VM over SMB
    and/or NFS. Queued for the agent at once, or `pending_approval` when the
    cluster requires four-eyes approval. The response carries the SMB password
    -- the only time it is ever shown. Audited (without the password)."""
    snapshot, group, datastore = _snapshot_context(db, body.backup_snapshot_id)
    require_restore_access(db, user, group)
    _require_vm(group)

    cluster = db.get(PveCluster, body.pve_cluster_id)
    if cluster is None or not cluster.is_lab_compute:
        raise ResurgamApiError(code="not_found", message="Lab cluster not found.", status_code=404)
    problems = placement_problems(cluster)
    if problems:
        raise ResurgamApiError(
            code="disk_mount_cluster_not_ready",
            message="Disk mounts are not set up on this cluster yet ("
            + ", ".join(problems)
            + "). A platform admin completes the Lab Compute setup and builds the share helper template "
            "(Recovery -> Disk Mounts -> Setup).",
            status_code=400,
            details={"problems": problems},
        )
    if cluster.lab_agent_node and body.target_node != cluster.lab_agent_node:
        raise ResurgamApiError(
            code="disk_mount_wrong_node",
            message=f"resurgam-lab-agent runs on node {cluster.lab_agent_node!r}; the backup's disks can only be "
            f"mapped there. Choose node {cluster.lab_agent_node!r} instead of {body.target_node!r}.",
            status_code=400,
            details={"lab_agent_node": cluster.lab_agent_node},
        )
    if body.ip_mode == "cluster_range" and cluster.lab_appliance_uplink_ip_mode != "static_range":
        raise ResurgamApiError(
            code="disk_mount_cluster_range_unavailable",
            message="This cluster's lab uplink has no static range (it uses DHCP). Choose ip_mode dhcp or static.",
            status_code=400,
        )

    available = {d.archive for d in _snapshot_disks(db, settings, snapshot, group, datastore)}
    unknown = [a for a in body.disk_archives if a not in available]
    if unknown:
        raise ResurgamApiError(
            code="disk_mount_unknown_disk",
            message=f"The snapshot has no disk {', '.join(unknown)}. Available: {', '.join(sorted(available)) or 'none'}.",
            status_code=400,
            details={"unknown": unknown, "available": sorted(available)},
        )

    if body.ip_mode == "static":
        # Serialise static-address checks per cluster (the same row lock
        # `allocate_uplink_ip` takes), so two concurrent requests cannot both pass.
        db.query(PveCluster).filter_by(id=cluster.id).with_for_update().one()
        address = str(ipaddress.IPv4Interface(body.ip_address_cidr).ip)
        _reject_address_in_uplink_range(cluster, address)
        for other in (
            db.query(DiskMount)
            .filter(DiskMount.pve_cluster_id == cluster.id, DiskMount.status.in_(ACTIVE_STATUSES))
            .filter(DiskMount.ip_address_cidr.isnot(None))
            .all()
        ):
            if str(ipaddress.IPv4Interface(other.ip_address_cidr).ip) == address:
                raise ResurgamApiError(
                    code="disk_mount_address_in_use",
                    message=f"{address} is already used by another active disk mount on this cluster. Pick another "
                    "address or stop that mount first.",
                    status_code=409,
                    details={"disk_mount_id": str(other.id)},
                )

    assignment = db.query(DatastoreCustomerAssignment).filter_by(datastore_id=datastore.id).one_or_none()
    mount = DiskMount(
        id=uuid.uuid4(),
        backup_snapshot_id=snapshot.id,
        backup_group_id=group.id,
        customer_id=effective_group_customer_id(group, assignment),
        disk_archives=list(body.disk_archives),
        pve_cluster_id=cluster.id,
        target_node=body.target_node,
        target_storage=body.target_storage,
        bridge=cluster.lab_appliance_uplink_bridge if body.ip_mode == "cluster_range" else body.bridge,
        vlan_tag=cluster.lab_appliance_uplink_vlan_tag if body.ip_mode == "cluster_range" else body.vlan_tag,
        ip_mode=body.ip_mode,
        ip_address_cidr=body.ip_address_cidr if body.ip_mode == "static" else None,
        gateway=body.gateway if body.ip_mode == "static" else None,
        protocols=list(body.protocols),
        allowed_clients=list(body.allowed_clients),
        lifetime_hours=body.lifetime_hours,
        created_by_user_id=user.user_id,
        approval_required=cluster.require_restore_approval,
        mapped_devices=[],
        helper_addresses=[],
        volumes=[],
        teardown_attempts=0,
        status=DiskMountStatus.PENDING_APPROVAL.value,
    )
    if mount.bridge is None:
        raise ResurgamApiError(
            code="disk_mount_cluster_range_unavailable",
            message="This cluster has no lab uplink bridge configured; choose a bridge.",
            status_code=400,
        )
    password = None
    if "smb" in body.protocols:
        mount.smb_username, password = lifecycle.generate_credentials()
        mount.share_password_ciphertext = lifecycle.encrypt_password(settings.secrets_local_encryption_key, password)
    db.add(mount)
    db.flush()
    if not cluster.require_restore_approval:
        lifecycle.queue(db, mount, cluster)

    lifecycle.record_audit(
        db,
        mount,
        "disk_mount_created",
        actor_user_id=user.user_id,
        correlation_id=_correlation_id(request),
        detail={**_audit_settings(mount), "status": mount.status},
    )
    db.commit()
    db.refresh(mount)
    return DiskMountCreatedOut(mount=mount_out(db, mount), share_password=password)


@router.post("/{mount_id}/approve", response_model=DiskMountOut)
def approve_disk_mount(
    mount_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> DiskMountOut:
    """Four-eyes approval (cluster `require_restore_approval`): a second operator
    with restore access for the tenant -- never the requester -- releases the mount
    to the agent. Audited."""
    mount = _mount_or_404(db, mount_id, user, for_update=True)
    _require_not_requester(mount, user)
    if mount.status != DiskMountStatus.PENDING_APPROVAL.value:
        raise ResurgamApiError(
            code="approval_not_pending", message="This disk mount is not awaiting approval.", status_code=409
        )
    cluster = db.get(PveCluster, mount.pve_cluster_id)
    mount.approved_by_user_id = user.user_id
    mount.approved_at = datetime.now(UTC)
    lifecycle.queue(db, mount, cluster)
    lifecycle.record_audit(
        db, mount, "disk_mount_approved", actor_user_id=user.user_id, correlation_id=_correlation_id(request)
    )
    db.commit()
    db.refresh(mount)
    return mount_out(db, mount)


@router.post("/{mount_id}/reject", response_model=DiskMountOut)
def reject_disk_mount(
    mount_id: uuid.UUID,
    body: DiskMountRejectRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> DiskMountOut:
    """Rejects a pending disk mount (second operator, never the requester). Nothing
    was created on the node yet. Audited with the reason."""
    mount = _mount_or_404(db, mount_id, user, for_update=True)
    _require_not_requester(mount, user)
    if mount.status != DiskMountStatus.PENDING_APPROVAL.value:
        raise ResurgamApiError(
            code="approval_not_pending", message="This disk mount is not awaiting approval.", status_code=409
        )
    mount.approved_by_user_id = user.user_id
    mount.approved_at = datetime.now(UTC)
    mount.approval_notes = body.reason
    lifecycle.finish(db, mount, DiskMountStatus.REJECTED.value)
    lifecycle.record_audit(
        db,
        mount,
        "disk_mount_rejected",
        actor_user_id=user.user_id,
        correlation_id=_correlation_id(request),
        detail={"reason": body.reason},
    )
    db.commit()
    db.refresh(mount)
    return mount_out(db, mount)


def _reject_address_in_uplink_range(cluster: PveCluster, address: str) -> None:
    """The cluster's static uplink range is handed out automatically (lab runs,
    template builds, `cluster_range` mounts) -- a static address inside it would
    collide with one of those sooner or later."""
    if cluster.lab_appliance_uplink_ip_mode != "static_range":
        return
    try:
        start = ipaddress.IPv4Address(cluster.lab_appliance_uplink_ip_range_start)
        end = ipaddress.IPv4Address(cluster.lab_appliance_uplink_ip_range_end)
    except ValueError:
        return
    if start <= ipaddress.IPv4Address(address) <= end:
        raise ResurgamApiError(
            code="disk_mount_address_in_uplink_range",
            message=f"{address} lies in this cluster's lab uplink range ({start}-{end}), which RESURGAM hands out "
            "automatically. Pick an address outside it, or choose ip_mode cluster_range to get one from the range.",
            status_code=409,
            details={"range_start": str(start), "range_end": str(end)},
        )


def _require_not_requester(mount: DiskMount, user: AuthenticatedUser) -> None:
    """Same rule as restore plans (`restore_execution._require_not_self_approval`):
    a plain 403, not exempted for platform admins."""
    if mount.created_by_user_id == user.user_id:
        raise ResurgamApiError(
            code="self_approval_forbidden",
            message="You cannot approve or reject your own disk mount; another operator has to.",
            status_code=403,
        )


@router.post("/{mount_id}/extend", response_model=DiskMountOut)
def extend_disk_mount(
    mount_id: uuid.UUID,
    body: DiskMountExtendRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> DiskMountOut:
    """Adds hours to a ready mount's lifetime (at most 72 h remaining). Audited."""
    mount = _mount_or_404(db, mount_id, user, for_update=True)
    if mount.status != DiskMountStatus.READY.value:
        raise ResurgamApiError(
            code="disk_mount_not_ready",
            message="Only a ready disk mount can be extended.",
            status_code=409,
        )
    previous = mount.expires_at
    expires_at = lifecycle.extend(mount, body.hours)
    lifecycle.record_audit(
        db,
        mount,
        "disk_mount_extended",
        actor_user_id=user.user_id,
        correlation_id=_correlation_id(request),
        detail={
            "hours": body.hours,
            "previous_expires_at": previous.isoformat() if previous else None,
            "expires_at": expires_at.isoformat(),
        },
    )
    db.commit()
    db.refresh(mount)
    return mount_out(db, mount)


@router.post("/{mount_id}/stop", response_model=DiskMountOut)
def stop_disk_mount(
    mount_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> DiskMountOut:
    """Ends a disk mount now: a pending or queued one ends at once; a provisioning
    or ready one goes to `stopping` and the agent removes the helper VM, unmaps the
    disks and deletes its snippet. Idempotent for a mount already stopping. Audited."""
    mount = _mount_or_404(db, mount_id, user, for_update=True)
    if mount.status == DiskMountStatus.STOPPING.value:
        return mount_out(db, mount)
    if mount.status not in ACTIVE_STATUSES:
        raise ResurgamApiError(
            code="disk_mount_finished", message="This disk mount has already ended.", status_code=409
        )
    now = datetime.now(UTC)
    previous_status = mount.status
    mount.stop_requested_at = now
    mount.stop_requested_by_user_id = user.user_id
    lifecycle.request_teardown(db, mount, DiskMountStatus.STOPPED.value, now=now)
    lifecycle.record_audit(
        db,
        mount,
        "disk_mount_stop_requested",
        actor_user_id=user.user_id,
        correlation_id=_correlation_id(request),
        detail={"previous_status": previous_status, "status": mount.status},
    )
    db.commit()
    db.refresh(mount)
    return mount_out(db, mount)


@router.post("/{mount_id}/force-finish", response_model=DiskMountOut)
def force_finish_disk_mount(
    mount_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(_require_platform_admin),
) -> DiskMountOut:
    """Platform admins only: ends a mount stuck in `stopping` (the agent is gone,
    or keeps failing to tear it down) without waiting for the agent. The mount
    ends `failed`, its uplink address is freed, and its error names what may
    still exist on the node (helper VM, mapped devices, snippet) for a manual
    cleanup. Audited."""
    mount = _mount_or_404(db, mount_id, user, for_update=True)
    if mount.status != DiskMountStatus.STOPPING.value:
        raise ResurgamApiError(
            code="disk_mount_not_stopping",
            message="Only a disk mount that is being torn down (stopping) can be force-finished.",
            status_code=409,
        )
    leftovers = lifecycle.leftovers_text(mount)
    lifecycle.give_up_teardown(db, mount, "Teardown force-finished by an administrator.")
    lifecycle.record_audit(
        db,
        mount,
        "disk_mount_force_finished",
        actor_user_id=user.user_id,
        correlation_id=_correlation_id(request),
        detail={"leftovers": leftovers, "teardown_attempts": mount.teardown_attempts},
    )
    db.commit()
    db.refresh(mount)
    return mount_out(db, mount)
