import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class RestorePlanVmidIntent(str, Enum):
    NEW = "new"
    EXISTING = "existing"


class RestorePlanNetworkMode(str, Enum):
    NONE = "none"
    ISOLATED = "isolated"
    ORIGINAL = "original"
    CUSTOM = "custom"


class RestorePlanMethod(str, Enum):
    """`full` copies the backup to the target storage before the guest exists
    (PVE restore). `instant` (ADR 0030, VMs only) runs the VM straight from the
    backup and moves its disks to storage while it runs."""

    FULL = "full"
    INSTANT = "instant"


class RestorePlanExecutionStatus(str, Enum):
    """EPIC 9 (manifest §11.2): the state machine tracked once a plan moves
    from "reviewed text" to "actually executing via the PVE API". Absence
    (column is NULL) means execution has never been attempted.

    Full restore: `restore_requested` -> `restore_running` -> `finalizing` ->
    `completed` (or `restore_failed` / `finalize_failed`). Instant Recovery
    (ADR 0030): `queued` -> `mapping` -> `starting` -> `running_from_backup` ->
    `moving_disks` -> `finishing` -> `completed`; `discarding` -> `discarded`;
    `failed`.
    """

    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    RESTORE_REQUESTED = "restore_requested"
    RESTORE_RUNNING = "restore_running"
    RESTORE_FAILED = "restore_failed"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FINALIZE_FAILED = "finalize_failed"
    # Instant Recovery (ADR 0030 §5). `queued`: waiting for the agent;
    # `mapping`/`starting`: the agent maps the disks / starts the VM;
    # `running_from_backup`: the VM runs, its disks read from PBS;
    # `moving_disks`: PVE copies the disks to the target storage, one after
    # another; `finishing`: all disks moved, the agent unmaps;
    # `discarding` -> `discarded`: VM and overlays removed; `failed`.
    QUEUED = "queued"
    MAPPING = "mapping"
    STARTING = "starting"
    RUNNING_FROM_BACKUP = "running_from_backup"
    MOVING_DISKS = "moving_disks"
    FINISHING = "finishing"
    DISCARDING = "discarding"
    DISCARDED = "discarded"
    FAILED = "failed"


class RestorePlanApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class RestorePlan(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Manifest EPIC 8/§11.1: a reviewed, traceable restore plan — the MVP's
    entire output is this record plus a generated `qmrestore`/`pct restore`
    command for a human to run; nothing here executes anything.

    EPIC 9 (manifest §11.2) extends this same row with the columns below
    once automated orchestration actually executes the plan via the PVE
    API, exactly as originally reserved here. One execution attempt per
    plan: a failed execution leaves the plan terminal, and retrying means
    creating a new plan (cheap, and re-validates live PVE state, which is
    wanted after a failure anyway) rather than modeling attempt history.
    `ttl`/`cleanup_status` are still deliberately excluded -- scheduled
    expiry/cleanup of a restored VM needs a scheduler that doesn't exist
    yet (ADR 0006) and isn't actually named in EPIC 9's own requirement
    bullets, only hinted at by this model's own earlier docstring.
    """

    __tablename__ = "restore_plans"

    backup_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backup_snapshots.id", ondelete="RESTRICT"), nullable=False
    )
    # Denormalized from the snapshot's group, avoiding a join for listing.
    backup_group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backup_groups.id", ondelete="RESTRICT"), nullable=False
    )
    # effective_customer_id at creation time -- snapshotted, not live, so a
    # later customer reassignment doesn't retroactively change who a past
    # plan is attributed to (same spirit as ADR 0009's VMID-uniqueness
    # caution about not assuming identity is stable across time/scope).
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    pve_cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pve_clusters.id", ondelete="RESTRICT"), nullable=False
    )

    target_node: Mapped[str] = mapped_column(String(255), nullable=False)
    target_storage: Mapped[str] = mapped_column(String(255), nullable=False)
    vmid_intent: Mapped[str] = mapped_column(String(20), nullable=False)
    target_vmid: Mapped[int] = mapped_column(Integer, nullable=False)
    temp_name: Mapped[str] = mapped_column(String(255), nullable=False)

    network_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    network_bridge: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Only ever changes the *wording* of the generated instruction text
    # (whether it includes a "to start manually" line) -- never executes.
    start_after_restore: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    network_danger_acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    checks_result: Mapped[list] = mapped_column(JSONB, nullable=False)
    checks_performed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generated_command: Mapped[str] = mapped_column(Text, nullable=False)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # EPIC 9 execution state -- all nullable, absence means "never
    # attempted". See RestorePlanExecutionStatus for the state machine.
    execution_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    upid: Mapped[str | None] = mapped_column(String(255), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    execution_started_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    execution_exit_status: Mapped[str | None] = mapped_column(String(255), nullable=True)
    execution_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    execution_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Updated on every poll that successfully reaches PVE, regardless of
    # what PVE reports -- the staleness check anchors on this, not
    # `executed_at`, so a legitimately long-running restore that PVE keeps
    # confirming is "running" is never force-failed by an arbitrary clock;
    # only a prolonged *inability to reach PVE at all* is treated as stuck.
    execution_last_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # EPIC 9 four-eyes approval (manifest §11.2's "Vier-Augen-Freigabe
    # optional erzwingen") -- set at plan-creation time from
    # PveCluster.require_restore_approval; NULL means not applicable (the
    # cluster doesn't require it). Enforced at the execute gate regardless
    # of when this was set, decoupling "visible in the list view" from
    # "safe to execute".
    approval_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Instant Recovery (ADR 0030, migration 0070) -------------------------
    method: Mapped[str] = mapped_column(String(20), nullable=False, default="full", server_default="full")
    # The original VM's state when the plan was created (`running`, `stopped`,
    # `absent`, `unknown`) and where it was found -- decides whether the
    # original network may be used and whether the MAC address is kept.
    ir_original_vm: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ir_keep_mac: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # What the agent created, reported as it happens (a discard after any crash
    # knows what to remove):
    # [{"slot": "scsi0", "archive": "drive-scsi0.img.fidx", "device": "/dev/loop3", "unit": "resurgam-ir-..."}]
    ir_mapped_devices: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    # [{"slot": "scsi0", "volume": "resurgam-lab-overlays:123/vm-123-ir-scsi0.qcow2"}]
    ir_overlay_volumes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    # EFI vars / TPM state imported to the target storage before the start.
    ir_imported_volumes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    ir_vm_created: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Devices of the original config that could not come along (not in the
    # backup, passthrough) -- shown to the operator.
    ir_skipped_disks: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    ir_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ir_last_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ir_running_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Agent heartbeat while the VM runs from the backup.
    ir_last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ir_overlay_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ir_storage_total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ir_storage_free_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Move to storage: [{"slot", "status": pending|running|done|failed, "upid", "error"}]
    ir_move_disks: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    ir_move_bwlimit_kib: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ir_move_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ir_move_requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    ir_discard_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ir_discard_requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # The last agent-side error of a finish/discard attempt (retried) and when
    # the agent may try again.
    ir_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    ir_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ir_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
