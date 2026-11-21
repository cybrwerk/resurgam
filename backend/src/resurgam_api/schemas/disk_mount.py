"""Request/response models of Disk Mounts (ADR 0025): the user API
(`/disk-mounts`) and the agent backchannel (`/lab-agent/disk-mount-jobs`)."""

import ipaddress
import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from resurgam_api.schemas.lab_setup import PveNodeName

DEFAULT_LIFETIME_HOURS = 4
MAX_LIFETIME_HOURS = 72
MAX_EXTEND_HOURS = 24
MAX_DISKS = 8
MAX_ALLOWED_CLIENTS = 8
MIN_CLIENT_PREFIX = 8

DiskMountStatusValue = Literal[
    "pending_approval", "queued", "provisioning", "ready", "stopping", "stopped", "expired", "failed", "rejected"
]
DiskMountProtocol = Literal["smb", "nfs"]
DiskMountIpMode = Literal["dhcp", "static", "cluster_range"]

_ARCHIVE_PATTERN = re.compile(r"^drive-(ide|sata|scsi|virtio|efidisk|tpmstate)\d+\.img\.fidx$")
_BRIDGE_PATTERN = r"^[A-Za-z][A-Za-z0-9_.\-]{0,15}$"
_STORAGE_PATTERN = r"^[A-Za-z][A-Za-z0-9\-_.]*$"


def parse_client(value: str) -> str:
    """One NFS/SMB client restriction: an IPv4 address or CIDR, nothing broader
    than /8. Returns the normalised network (`192.168.10.0/24`, `10.1.2.3/32`)."""
    try:
        network = ipaddress.IPv4Network(value.strip(), strict=False)
    except ValueError as exc:
        raise ValueError(
            f"'{value}' is not an IPv4 address or network (e.g. 192.168.10.25 or 192.168.10.0/24)."
        ) from exc
    if network.prefixlen < MIN_CLIENT_PREFIX:
        raise ValueError(
            f"'{value}' is too broad: allow at most a /{MIN_CLIENT_PREFIX} network. Enter the address or subnet of "
            "the machines that will mount the share."
        )
    return str(network)


class DiskMountCreateRequest(BaseModel):
    """A new disk mount: which disks of which VM backup, on which lab cluster/
    node, on which network, shared how, for how long."""

    model_config = ConfigDict(extra="forbid")

    backup_snapshot_id: UUID = Field(description="The VM backup snapshot whose disks are mounted.")
    disk_archives: list[str] = Field(
        min_length=1,
        max_length=MAX_DISKS,
        description="The snapshot's disk archives to mount, e.g. `drive-scsi0.img.fidx` (from "
        "`GET /disk-mounts/snapshot-disks`). Attached in this order as disk 1, 2, ...",
    )
    pve_cluster_id: UUID = Field(
        description="Lab cluster that runs the helper VM (`GET /disk-mounts/placement`, must be `ready`)."
    )
    target_node: PveNodeName = Field(
        description="Node for the helper VM. Must be the node `resurgam-lab-agent` runs on: the backup's disks are "
        "mapped there as local block devices."
    )
    target_storage: str | None = Field(
        default=None,
        max_length=64,
        pattern=_STORAGE_PATTERN,
        description="Storage for the helper's own disk when a linked clone of the helper template is not possible "
        "(full clone). Omitted: the template's storage.",
    )
    bridge: str | None = Field(
        default=None,
        pattern=_BRIDGE_PATTERN,
        description="PVE bridge the helper's network card is attached to (e.g. `vmbr0`). Required unless "
        "`ip_mode` is `cluster_range`, which always uses the lab uplink bridge.",
    )
    vlan_tag: int | None = Field(
        default=None,
        ge=1,
        le=4094,
        description="Optional VLAN tag on the helper's network card (PVE `net0` `tag=`). Not allowed with "
        "`cluster_range` (the uplink's tag is used).",
    )
    ip_mode: DiskMountIpMode = Field(
        default="dhcp",
        description="`dhcp`: the network hands out an address. `static`: `ip_address_cidr` (+ optional "
        "`gateway`). `cluster_range`: an address from the cluster's lab uplink static range, freed when the mount "
        "ends.",
    )
    ip_address_cidr: str | None = Field(
        default=None,
        max_length=64,
        description="With `static`: the helper's IPv4 address with prefix length, e.g. `192.168.10.50/24`.",
    )
    gateway: str | None = Field(
        default=None,
        max_length=45,
        description="With `static`: optional IPv4 gateway inside that subnet (only needed if clients are in other "
        "subnets).",
    )
    protocols: list[DiskMountProtocol] = Field(
        min_length=1,
        max_length=2,
        description="`smb` (SMB3, one generated user + password, read-only) and/or `nfs` (read-only, restricted to "
        "`allowed_clients`).",
    )
    allowed_clients: list[str] = Field(
        default_factory=list,
        max_length=MAX_ALLOWED_CLIENTS,
        description="IPv4 addresses or networks allowed to connect (e.g. `192.168.10.25`, `192.168.10.0/24`; at "
        "most a /8). Required for NFS, which has no password; if given, SMB is restricted to them too.",
    )
    lifetime_hours: int = Field(
        default=DEFAULT_LIFETIME_HOURS,
        ge=1,
        le=MAX_LIFETIME_HOURS,
        description=f"How long the share stays up once ready (default {DEFAULT_LIFETIME_HOURS} h, at most "
        f"{MAX_LIFETIME_HOURS} h). Extendable; the helper is removed automatically afterwards.",
    )

    @field_validator("disk_archives")
    @classmethod
    def _archives(cls, value: list[str]) -> list[str]:
        for archive in value:
            if not _ARCHIVE_PATTERN.fullmatch(archive):
                raise ValueError(
                    f"'{archive}' is not a VM disk archive name (expected e.g. drive-scsi0.img.fidx). Pick the disks "
                    "from GET /disk-mounts/snapshot-disks."
                )
        if len(set(value)) != len(value):
            raise ValueError("Each disk can be selected only once.")
        return value

    @field_validator("protocols")
    @classmethod
    def _protocols(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("Each protocol can be selected only once.")
        return sorted(value, key=["smb", "nfs"].index)

    @field_validator("allowed_clients")
    @classmethod
    def _clients(cls, value: list[str]) -> list[str]:
        normalised = [parse_client(v) for v in value]
        return list(dict.fromkeys(normalised))

    @model_validator(mode="after")
    def _network(self) -> "DiskMountCreateRequest":
        if "nfs" in self.protocols and not self.allowed_clients:
            raise ValueError(
                "NFS has no password, so allowed_clients is required: enter the address or subnet of the machines "
                "that will mount the export (e.g. 192.168.10.25)."
            )
        if self.ip_mode == "cluster_range":
            if self.vlan_tag is not None or self.ip_address_cidr or self.gateway:
                raise ValueError(
                    "With ip_mode cluster_range the lab uplink's bridge, VLAN tag and range are used; leave vlan_tag, "
                    "ip_address_cidr and gateway empty."
                )
            return self
        if not self.bridge:
            raise ValueError("bridge is required (e.g. vmbr0), unless ip_mode is cluster_range.")
        if self.ip_mode == "dhcp":
            if self.ip_address_cidr or self.gateway:
                raise ValueError("ip_address_cidr and gateway are only used with ip_mode static.")
            return self
        if not self.ip_address_cidr:
            raise ValueError("ip_mode static needs ip_address_cidr, e.g. 192.168.10.50/24.")
        try:
            interface = ipaddress.IPv4Interface(self.ip_address_cidr.strip())
        except ValueError as exc:
            raise ValueError(
                f"ip_address_cidr '{self.ip_address_cidr}' is not an IPv4 address with prefix (e.g. 192.168.10.50/24)."
            ) from exc
        if "/" not in self.ip_address_cidr or not 8 <= interface.network.prefixlen <= 30:
            raise ValueError("ip_address_cidr needs a prefix length between /8 and /30, e.g. 192.168.10.50/24.")
        if interface.ip in (interface.network.network_address, interface.network.broadcast_address):
            raise ValueError(f"{interface.ip} is the network or broadcast address of {interface.network}.")
        self.ip_address_cidr = str(interface)
        if self.gateway:
            try:
                gateway = ipaddress.IPv4Address(self.gateway.strip())
            except ValueError as exc:
                raise ValueError(f"gateway '{self.gateway}' is not an IPv4 address.") from exc
            if gateway not in interface.network or gateway == interface.ip:
                raise ValueError(f"gateway {gateway} must be another address inside {interface.network}.")
            self.gateway = str(gateway)
        return self


class DiskMountRejectRequest(BaseModel):
    reason: str = Field(
        min_length=1, max_length=2000, description="Why the mount is rejected (shown to the requester)."
    )


class DiskMountExtendRequest(BaseModel):
    hours: int = Field(
        ge=1,
        le=MAX_EXTEND_HOURS,
        description=f"Hours to add to the remaining lifetime (1-{MAX_EXTEND_HOURS}); the total remaining time is "
        f"capped at {MAX_LIFETIME_HOURS} h.",
    )


class DiskMountVolumeOut(BaseModel):
    disk_index: int = Field(description="Which selected disk (1 = the first archive in `disk_archives`).")
    archive: str = Field(description="The disk archive, e.g. `drive-scsi0.img.fidx`.")
    partition: str = Field(description="`part2`, `whole` (no partition table) or `<vg>-<lv>` for an LVM volume.")
    fstype: str | None = Field(description="File system detected by the helper (`blkid`), `null` if none.")
    size_bytes: int | None = Field(default=None, description="Size of the partition/volume.")
    name: str | None = Field(
        default=None, description="Folder in the SMB share / NFS export path, set when `mounted` is true."
    )
    mounted: bool = Field(description="Whether the helper mounted it (always read-only).")
    reason: str | None = Field(
        default=None,
        description="Why not mounted: `encrypted` (LUKS/BitLocker, no key), `unsupported` (e.g. ReFS, ZFS), "
        "`swap`, `no_filesystem`, `mount_failed`, `lvm_activation_failed`, `lvm_unreadable`.",
    )
    detail: str | None = Field(default=None, description="The mount error, when `reason` is `mount_failed`.")


class DiskMountConnectionOut(BaseModel):
    host: str | None = Field(description="Address of the helper VM clients connect to; `null` until it is ready.")
    smb_unc: str | None = Field(description="SMB path for Windows Explorer / `net use`, e.g. `\\\\10.0.0.5\\restore`.")
    smb_url: str | None = Field(description="The same share as a URL for macOS/Linux, e.g. `smb://10.0.0.5/restore`.")
    smb_username: str | None = Field(description="SMB user of this mount (the password was shown once at creation).")
    nfs_exports: list[str] = Field(
        description="One `host:/path` per mounted volume, for `mount -t nfs -o ro <export> /mnt/x`."
    )


class DiskMountOut(BaseModel):
    """One disk mount as shown in the list and detail views. Never contains the
    share password."""

    id: UUID = Field(description="Disk mount id.")
    status: DiskMountStatusValue = Field(description="Lifecycle state (ADR 0025 §4).")
    teardown_reason: str | None = Field(
        description="While `stopping`: why (`stopped`, `expired`, `failed`); the final state it ends in."
    )
    customer_id: UUID | None = Field(description="Tenant the backup belongs to.")
    customer_name: str | None = Field(default=None, description="Tenant name.")
    backup_group_id: UUID = Field(description="Backup group (the VM).")
    backup_snapshot_id: UUID = Field(description="Snapshot whose disks are mounted.")
    snapshot_time: datetime | None = Field(default=None, description="When the snapshot was taken.")
    workload_label: str | None = Field(
        default=None, description="The VM's name/id as shown elsewhere, e.g. `vm/116 web01`."
    )
    disk_archives: list[str] = Field(description="Mounted disk archives, in disk order.")
    pve_cluster_id: UUID = Field(description="Lab cluster running the helper VM.")
    pve_cluster_name: str | None = Field(default=None, description="That cluster's display name.")
    target_node: str = Field(description="Node running the helper VM.")
    bridge: str = Field(description="Bridge of the helper's network card.")
    vlan_tag: int | None = Field(description="VLAN tag, if any.")
    ip_mode: DiskMountIpMode = Field(description="How the helper gets its address.")
    ip_address_cidr: str | None = Field(description="Static or allocated address with prefix; `null` with DHCP.")
    gateway: str | None = Field(description="Static gateway, if any.")
    protocols: list[DiskMountProtocol] = Field(description="Shared via.")
    allowed_clients: list[str] = Field(description="Client restriction (NFS, and SMB when set).")
    smb_username: str | None = Field(description="SMB user, when SMB is enabled.")
    lifetime_hours: int = Field(description="Requested lifetime.")
    created_at: datetime = Field(description="When the mount was requested.")
    created_by_user_id: UUID | None = Field(description="Who requested it.")
    approval_required: bool = Field(description="Whether a second operator has to approve it (cluster setting).")
    approved_by_user_id: UUID | None = Field(description="Who approved or rejected it.")
    approved_at: datetime | None = Field(description="When it was approved or rejected.")
    approval_notes: str | None = Field(description="Rejection reason.")
    ready_at: datetime | None = Field(description="When the share became available.")
    expires_at: datetime | None = Field(description="When the helper is removed automatically (set once ready).")
    ended_at: datetime | None = Field(description="When it reached a final state.")
    error: str | None = Field(description="What went wrong (provisioning or teardown).")
    helper_vmid: int | None = Field(description="VMID of the helper VM while it exists.")
    volumes: list[DiskMountVolumeOut] = Field(description="Per partition/volume: mounted or why not.")
    connection: DiskMountConnectionOut = Field(description="How to connect (filled once ready).")


class DiskMountCreatedOut(BaseModel):
    """`POST /disk-mounts` -- the only response that ever contains the share
    password."""

    mount: DiskMountOut = Field(description="The new disk mount.")
    share_password: str | None = Field(
        description="The SMB password, shown exactly once (only Fernet ciphertext is stored until the helper got "
        "it, then nothing). `null` for an NFS-only mount. Lost? Stop the mount and create a new one."
    )


class SnapshotDiskOut(BaseModel):
    archive: str = Field(description="Disk archive name, e.g. `drive-scsi0.img.fidx`.")
    slot: str = Field(description="The VM's disk slot the archive came from, e.g. `scsi0`.")
    size_bytes: int | None = Field(description="Disk size from the backup manifest.")
    crypt_mode: str | None = Field(description="`none`, `encrypt` or `sign-only` (PBS manifest).")


class DiskMountPlacementOut(BaseModel):
    pve_cluster_id: UUID = Field(description="Lab cluster id.")
    display_name: str = Field(description="Cluster name.")
    ready: bool = Field(description="True when disk mounts can be created on this cluster.")
    problems: list[str] = Field(
        description="What is missing: `agent_token_missing`, `agent_not_seen`, `snippet_storage_missing`, "
        "`helper_template_missing`, `cluster_disabled`."
    )
    require_approval: bool = Field(description="Whether mounts on this cluster need a second operator's approval.")
    helper_template_vmid: int | None = Field(description="The share helper template's VMID.")
    lab_agent_last_seen_at: datetime | None = Field(description="When the lab agent last checked in.")
    lab_agent_node: str | None = Field(
        default=None,
        description="The node a lab agent was paired on (ADR 0027): the helper template can then be built by the "
        "agent instead of over SSH.",
    )
    uplink_bridge: str | None = Field(description="Lab uplink bridge (used by `ip_mode` `cluster_range`).")
    uplink_vlan_tag: int | None = Field(description="Lab uplink VLAN tag.")
    uplink_static_range: bool = Field(description="Whether `cluster_range` is available (uplink has a static range).")


class HelperTemplateUpdateRequest(BaseModel):
    helper_template_vmid: int | None = Field(
        ge=100,
        le=999999999,
        description="VMID of an existing share helper template on the lab node (built by RESURGAM); `null` turns "
        "disk mounts off for this cluster.",
    )


# --- agent backchannel ----------------------------------------------------------------------


class DiskMountAgentJobOut(BaseModel):
    """One unit of work for `resurgam-lab-agent`. `provision`: claim it, then
    build the helper. `teardown`: remove what the mount recorded."""

    id: UUID = Field(description="Disk mount id.")
    kind: Literal["provision", "teardown"] = Field(description="What to do.")
    target_node: str = Field(description="Node the mount was requested for (must be the agent's node).")
    helper_name: str = Field(
        description="Name the helper VM has/gets (`rsgm-mount-<id8>`); a teardown only "
        "destroys a VM with exactly this name."
    )
    helper_vmid: int | None = Field(description="Teardown: the helper VM to remove, if one was created.")
    mapped_devices: list[dict] = Field(description="Teardown: `[{archive, device}]` to unmap.")
    snippet_volume: str | None = Field(description="Teardown: snippet to delete.")
    snapshot_path: str = Field(description="`<type>/<id>/<time>`, used to recognise this mount's mappings.")


class DiskMountDiskOut(BaseModel):
    archive: str = Field(description="Disk archive to map.")
    serial: str = Field(description="QEMU drive serial for this disk (`serial=`).")
    slot: str = Field(description="Helper drive slot to attach the mapped device to, e.g. `scsi1`.")


class DiskMountClaimOut(BaseModel):
    """Everything needed to build the helper. The only place the share password
    and the PBS encryption key are handed out; the backend forgets the password
    in the same transaction."""

    id: UUID = Field(description="Disk mount id.")
    target_node: str = Field(description="Node to build on.")
    target_storage: str | None = Field(description="Storage for a full clone, if a linked clone fails.")
    helper_name: str = Field(description="VM name for the helper.")
    helper_template_vmid: int = Field(description="Share helper template to clone.")
    pbs_host: str = Field(description="PBS host.")
    pbs_port: int = Field(description="PBS port.")
    pbs_datastore: str = Field(description="Datastore.")
    pbs_namespace: str = Field(description="Namespace ('' = root).")
    snapshot_path: str = Field(description="`<type>/<id>/<time>` for proxmox-backup-client.")
    disks: list[DiskMountDiskOut] = Field(description="Disks to map and attach, in order.")
    net0: str = Field(description="PVE `net0` value (bridge, optional `tag=`).")
    ipconfig0: str = Field(description="PVE `ipconfig0` value.")
    snippet_storage: str = Field(description="Storage (content `snippets`) for the cloud-init user data.")
    snippet_filename: str = Field(description="File name of the snippet.")
    user_data: str = Field(description="cloud-init user data (no secrets).")
    smb_username: str | None = Field(description="SMB user, if SMB is enabled.")
    share_password: str | None = Field(description="SMB password, set via `qm guest exec --pass-stdin`.")
    encryption_keyfile: str | None = Field(description="PBS encryption key content, if the datastore has one.")


class DiskMountProgressRequest(BaseModel):
    helper_vmid: int | None = Field(default=None, ge=100, le=999999999, description="VMID about to be used.")
    mapped_device: dict | None = Field(default=None, description="`{archive, device}` just mapped.")
    snippet_volume: str | None = Field(
        default=None,
        max_length=255,
        pattern=r"^[A-Za-z][A-Za-z0-9\-_.]*:snippets/resurgam-[a-z0-9-]+\.yaml$",
        description="Snippet volume just written: `<storage>:snippets/<this mount's snippet_filename>` (anything else "
        "is rejected, since a teardown deletes it).",
    )


class DiskMountStatusRequest(BaseModel):
    status: Literal["ready", "failed"] = Field(description="Outcome of provisioning.")
    error: str | None = Field(default=None, max_length=4000, description="With `failed`: what went wrong.")
    cleaned_up: bool = Field(
        default=False, description="With `failed`: whether the agent already removed everything it created."
    )
    addresses: list[str] = Field(default_factory=list, max_length=16, description="Helper IPv4 addresses.")
    volumes: list[dict] = Field(default_factory=list, max_length=256, description="The helper's per-volume report.")


class DiskMountTeardownStatusRequest(BaseModel):
    status: Literal["done", "failed"] = Field(description="Outcome of the teardown.")
    error: str | None = Field(default=None, max_length=4000, description="With `failed`: what is left and why.")
