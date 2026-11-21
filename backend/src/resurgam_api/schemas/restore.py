from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from resurgam_api.models.restore import (
    RestorePlanApprovalStatus,
    RestorePlanExecutionStatus,
    RestorePlanMethod,
    RestorePlanNetworkMode,
    RestorePlanVmidIntent,
)


class RestorePlanCreateRequest(BaseModel):
    backup_snapshot_id: UUID
    pve_cluster_id: UUID
    target_node: str
    target_storage: str
    vmid_intent: RestorePlanVmidIntent
    target_vmid: int
    temp_name: str | None = None
    network_mode: RestorePlanNetworkMode
    network_bridge: str | None = None
    start_after_restore: bool = False
    network_danger_acknowledged: bool = False
    method: RestorePlanMethod = Field(
        default=RestorePlanMethod.FULL,
        description=(
            "`full` (default): PVE copies the whole backup to `target_storage`, then the guest exists. "
            "`instant` (VMs only, ADR 0030): the cluster's paired lab agent runs the VM straight from the "
            "backup within minutes; its disks are moved to `target_storage` later, while it runs "
            "(`POST /restore-plans/{id}/instant-recovery/move`). Needs Instant Recovery enabled on the "
            "cluster, `target_node` = the agent's node and a free `target_vmid`."
        ),
    )

    @model_validator(mode="after")
    def _bridge_required_for_custom_mode(self) -> "RestorePlanCreateRequest":
        if self.network_mode == RestorePlanNetworkMode.CUSTOM and not self.network_bridge:
            raise ValueError("network_bridge is required when network_mode is 'custom'")
        return self


class RestorePlanCheckEntry(BaseModel):
    check: str
    status: str
    message: str


class RestorePlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    backup_snapshot_id: UUID
    backup_group_id: UUID
    customer_id: UUID | None
    pve_cluster_id: UUID
    target_node: str
    target_storage: str
    vmid_intent: RestorePlanVmidIntent
    target_vmid: int
    temp_name: str
    network_mode: RestorePlanNetworkMode
    network_bridge: str | None
    start_after_restore: bool
    network_danger_acknowledged: bool
    checks_result: list[RestorePlanCheckEntry]
    checks_performed_at: datetime
    generated_command: str
    created_by_user_id: UUID | None
    created_at: datetime

    # EPIC 9: execution + four-eyes approval state, all None until acted on.
    execution_status: RestorePlanExecutionStatus | None = None
    upid: str | None = None
    executed_at: datetime | None = None
    execution_started_by_user_id: UUID | None = None
    execution_exit_status: str | None = None
    execution_completed_at: datetime | None = None
    execution_error: str | None = None
    execution_last_confirmed_at: datetime | None = None
    approval_status: RestorePlanApprovalStatus | None = None
    approved_by_user_id: UUID | None = None
    approved_at: datetime | None = None
    approval_notes: str | None = None

    method: RestorePlanMethod = Field(
        default=RestorePlanMethod.FULL, description="`full` (copy, then start) or `instant` (run from the backup)."
    )
    instant_recovery: "InstantRecoveryStateOut | None" = Field(
        default=None, description="Instant Recovery state; only set when `method` is `instant`."
    )

    @model_validator(mode="before")
    @classmethod
    def _attach_instant_recovery(cls, data):
        from resurgam_api.models.restore import RestorePlan

        if isinstance(data, RestorePlan) and data.method == RestorePlanMethod.INSTANT.value:
            state = InstantRecoveryStateOut.from_plan(data)
            fields = {name: getattr(data, name) for name in cls.model_fields if name != "instant_recovery"}
            return {**fields, "instant_recovery": state}
        return data


class InstantRecoveryOriginalVmOut(BaseModel):
    status: str = Field(
        description=(
            "State of the backed-up VM when the plan was created: `running`, `stopped`, `absent` (no VM with "
            "this VMID on any checked cluster) or `unknown` (a cluster could not be reached)."
        )
    )
    cluster_name: str | None = Field(default=None, description="Cluster where the original VM was found.")
    node: str | None = Field(default=None, description="Node where the original VM was found.")
    unreachable_clusters: list[str] = Field(
        default_factory=list, description="Clusters that could not be asked (their state is unknown)."
    )


class InstantRecoveryMoveDiskOut(BaseModel):
    slot: str = Field(description="Disk slot of the VM, e.g. `scsi0`.")
    status: str = Field(description="`pending`, `running`, `done` or `failed`.")
    upid: str | None = Field(default=None, description="PVE task ID of this disk's move, once started.")
    error: str | None = Field(default=None, description="Why the move failed (PVE task exit status).")


class InstantRecoveryStateOut(BaseModel):
    """What RESURGAM knows about a VM running from its backup (ADR 0030)."""

    original_vm: InstantRecoveryOriginalVmOut | None = Field(
        default=None, description="The original VM's state at plan creation (network and MAC decisions)."
    )
    keep_mac: bool = Field(
        description="Whether the VM keeps the backup's MAC addresses (only when the original VM is not running)."
    )
    disks: list[str] = Field(
        default_factory=list, description="Disk slots running from the backup through a qcow2 overlay."
    )
    imported_volumes: list[str] = Field(
        default_factory=list, description="EFI vars / TPM state volumes imported to the target storage before start."
    )
    skipped_devices: list[str] = Field(
        default_factory=list,
        description="Devices of the original VM left out: disks not in the backup (`backup=0`), passthrough devices.",
    )
    running_since: datetime | None = Field(default=None, description="When the VM started running from the backup.")
    last_heartbeat_at: datetime | None = Field(
        default=None,
        description="Last report from the lab agent while the VM runs from the backup (about every minute).",
    )
    overlay_bytes: int | None = Field(
        default=None, description="Bytes the VM has written since the start (space its overlays occupy)."
    )
    overlay_storage_total_bytes: int | None = Field(default=None, description="Size of the agent's overlay storage.")
    overlay_storage_free_bytes: int | None = Field(
        default=None, description="Free space on the agent's overlay storage."
    )
    move_disks: list[InstantRecoveryMoveDiskOut] = Field(
        default_factory=list, description="Per-disk progress of Move to storage (one disk after another)."
    )
    move_bwlimit_kib: int | None = Field(default=None, description="Bandwidth limit of the move in KiB/s, if set.")
    last_error: str | None = Field(default=None, description="Last error of the agent (start, finish or discard).")
    ended_at: datetime | None = Field(
        default=None, description="When the plan reached `completed`, `discarded` or `failed`."
    )

    @classmethod
    def from_plan(cls, plan) -> "InstantRecoveryStateOut":
        return cls(
            original_vm=InstantRecoveryOriginalVmOut.model_validate(plan.ir_original_vm)
            if plan.ir_original_vm
            else None,
            keep_mac=plan.ir_keep_mac,
            disks=[str(v.get("slot")) for v in plan.ir_overlay_volumes or []],
            imported_volumes=[str(v.get("slot")) for v in plan.ir_imported_volumes or []],
            skipped_devices=list(plan.ir_skipped_disks or []),
            running_since=plan.ir_running_since,
            last_heartbeat_at=plan.ir_last_heartbeat_at,
            overlay_bytes=plan.ir_overlay_bytes,
            overlay_storage_total_bytes=plan.ir_storage_total_bytes,
            overlay_storage_free_bytes=plan.ir_storage_free_bytes,
            move_disks=[InstantRecoveryMoveDiskOut.model_validate(d) for d in plan.ir_move_disks or []],
            move_bwlimit_kib=plan.ir_move_bwlimit_kib,
            last_error=plan.ir_last_error,
            ended_at=plan.ir_ended_at,
        )


class InstantRecoveryMoveRequest(BaseModel):
    bwlimit_kib: int | None = Field(
        default=None,
        ge=1,
        le=10_000_000,
        description=(
            "Optional bandwidth limit for the disk moves in KiB/s (PVE `move_disk` `bwlimit`). Empty = the "
            "cluster's default migration limit."
        ),
    )


class InstantRecoveryDiscardRequest(BaseModel):
    confirm: bool = Field(
        description=(
            "Must be true. Discard stops and deletes the VM; everything written since it started from the "
            "backup is lost. The backup itself was never written to."
        )
    )


class RestorePlanRejectRequest(BaseModel):
    reason: str


RestorePlanOut.model_rebuild()
