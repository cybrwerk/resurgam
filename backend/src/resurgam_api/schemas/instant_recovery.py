"""Lab agent backchannel of Instant Recovery (ADR 0030). The agent's own copies
of these models live in `restore-worker`'s `lab_agent/client.py`."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class InstantRecoveryMappedDevice(BaseModel):
    slot: str = Field(description="Disk slot of the VM, e.g. `scsi0`.", pattern=r"^(ide|sata|scsi|virtio)\d{1,2}$")
    archive: str = Field(description="Disk archive in the snapshot, e.g. `drive-scsi0.img.fidx`.", max_length=128)
    device: str = Field(
        description="Loop device the archive is mapped on, e.g. `/dev/loop3`.", pattern=r"^/dev/loop\d{1,5}$"
    )
    unit: str = Field(
        description="systemd scope that keeps the mapping alive across agent restarts (without `.scope`).",
        pattern=r"^resurgam-ir-[0-9a-f-]{36}-[a-z0-9]{2,12}$",
    )


class InstantRecoveryVolume(BaseModel):
    slot: str = Field(description="Drive slot, e.g. `scsi0`, `efidisk0`, `tpmstate0`.", pattern=r"^[a-z]+\d{1,2}$")
    volume: str = Field(description="PVE volume ID, e.g. `local-lvm:vm-123-disk-0`.", max_length=255)


class InstantRecoveryAgentJobOut(BaseModel):
    id: UUID = Field(description="The restore plan's ID.")
    kind: Literal["start", "watch", "finish", "discard"] = Field(
        description=(
            "`start`: claim it, map the disks, create and start the VM. `watch`: the VM runs from the backup; send a "
            "heartbeat about once a minute and report a lost mapping. `finish`: every disk was moved -- unmap and "
            "remove the overlays. `discard`: stop and delete the VM, unmap, remove the overlays."
        )
    )
    target_node: str = Field(description="Node the VM runs on; only the agent on this node acts on the job.")
    target_vmid: int = Field(description="VMID of the recovered VM.")
    name: str = Field(description="Name the VM was created with (a discard checks it before deleting anything).")
    vm_created: bool = Field(
        description="Whether the agent created the VM. False: a discard must not touch whatever VM has this VMID now."
    )
    mapped_devices: list[InstantRecoveryMappedDevice] = Field(
        default_factory=list, description="Mappings created so far (what `finish`/`discard` must unmap)."
    )
    snapshot_path: str = Field(description="The backup, in proxmox-backup-client notation (`vm/116/2026-...Z`).")


class InstantRecoveryClaimOut(BaseModel):
    id: UUID = Field(description="The restore plan's ID.")
    target_node: str = Field(description="Node to run the VM on.")
    target_vmid: int = Field(description="Free VMID for the new VM.")
    target_storage: str = Field(
        description="Where the disks are moved to later; EFI vars and TPM state are imported here before the start."
    )
    name: str = Field(description="Name of the new VM.")
    pbs_host: str = Field(description="PBS host the backup is read from.")
    pbs_port: int = Field(description="PBS API port.")
    pbs_datastore: str = Field(description="PBS datastore of the backup.")
    pbs_namespace: str = Field(default="", description="PBS namespace of the backup (empty = root).")
    snapshot_path: str = Field(description="The backup, in proxmox-backup-client notation.")
    network_mode: Literal["none", "isolated", "original", "custom"] = Field(
        description=(
            "`original`: NICs as in the backup. `none`: NICs kept but disconnected (`link_down=1`). `isolated`: on "
            "`network_bridge`, disconnected. `custom`: on `network_bridge`, connected."
        )
    )
    network_bridge: str | None = Field(default=None, description="Bridge for `isolated`/`custom` (and `none`, if set).")
    keep_mac: bool = Field(
        description="Keep the backup's MAC addresses. False when the original VM runs or its state is unknown."
    )
    min_free_bytes: int = Field(description="Refuse to start when less is free on the agent's overlay storage.")
    encryption_keyfile: str | None = Field(
        default=None, description="PBS encryption key content when the datastore has one (ADR 0022). Never log it."
    )


class InstantRecoveryProgressRequest(BaseModel):
    """Exactly one resource per call, reported the moment it exists."""

    mapped_device: InstantRecoveryMappedDevice | None = Field(default=None, description="A disk was just mapped.")
    overlay_volume: InstantRecoveryVolume | None = Field(
        default=None, description="An overlay was just created on top of a mapped disk."
    )
    imported_volume: InstantRecoveryVolume | None = Field(
        default=None, description="EFI vars or TPM state were just imported to the target storage."
    )
    vm_created: bool | None = Field(default=None, description="True once `qm create` succeeded.")
    phase: Literal["starting"] | None = Field(default=None, description="`starting`: `qm start` is being called.")


class InstantRecoveryStatusRequest(BaseModel):
    status: Literal["running", "failed", "lost", "finished", "finish_failed", "discarded", "discard_failed"] = Field(
        description=(
            "`running`: the VM started from the backup. `failed`: the start failed (`cleaned_up` says whether "
            "everything was removed again). `lost`: a mapping of a running VM is gone. `finished`: unmapped after the "
            "move. `discarded`: VM and overlays removed. `*_failed`: that attempt failed and is retried."
        )
    )
    error: str | None = Field(default=None, max_length=4000, description="What went wrong, for the operator.")
    cleaned_up: bool = Field(default=False, description="For `failed`: whether the agent removed what it created.")
    skipped_disks: list[str] = Field(
        default_factory=list, max_length=64, description="For `running`: devices of the original VM left out."
    )


class InstantRecoveryHeartbeatRequest(BaseModel):
    overlay_bytes: int = Field(ge=0, description="Bytes the overlays occupy (allocated, not virtual size).")
    storage_total_bytes: int = Field(ge=0, description="Size of the filesystem holding the overlays.")
    storage_free_bytes: int = Field(ge=0, description="Free space on that filesystem.")
