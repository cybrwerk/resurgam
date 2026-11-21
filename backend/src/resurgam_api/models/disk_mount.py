import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class DiskMountStatus(str, Enum):
    """ADR 0025 §4. Terminal: `rejected`, `stopped`, `expired`, `failed`.
    `stopping` means "the agent still has to tear something down" (after a
    stop, the lifetime ending, or a failed provisioning that left resources)."""

    PENDING_APPROVAL = "pending_approval"
    QUEUED = "queued"
    PROVISIONING = "provisioning"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    EXPIRED = "expired"
    FAILED = "failed"
    REJECTED = "rejected"


TERMINAL_STATUSES = frozenset(
    {
        DiskMountStatus.STOPPED.value,
        DiskMountStatus.EXPIRED.value,
        DiskMountStatus.FAILED.value,
        DiskMountStatus.REJECTED.value,
    }
)
ACTIVE_STATUSES = frozenset(
    {
        DiskMountStatus.PENDING_APPROVAL.value,
        DiskMountStatus.QUEUED.value,
        DiskMountStatus.PROVISIONING.value,
        DiskMountStatus.READY.value,
        DiskMountStatus.STOPPING.value,
    }
)


class DiskMount(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One Disk Mount (ADR 0025): the selected disks of one VM backup snapshot,
    mapped read-only on a lab node by `resurgam-lab-agent`, attached to a
    disposable helper VM and shared over SMB and/or NFS for a limited time.

    The share password is never stored in plaintext: only Fernet ciphertext
    (`share_password_ciphertext`) until the agent's claim hands it over, then
    NULL. Every resource the agent creates (`helper_vmid`, `mapped_devices`,
    `snippet_volume`) is reported as it comes into existence, so a teardown
    after any crash or restart knows exactly what to remove."""

    __tablename__ = "disk_mounts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_approval', 'queued', 'provisioning', 'ready', 'stopping', 'stopped', 'expired', "
            "'failed', 'rejected')",
            name="ck_disk_mounts_status",
        ),
        CheckConstraint("ip_mode IN ('dhcp', 'static', 'cluster_range')", name="ck_disk_mounts_ip_mode"),
        CheckConstraint("vlan_tag IS NULL OR vlan_tag BETWEEN 1 AND 4094", name="ck_disk_mounts_vlan_tag"),
    )

    # --- what --------------------------------------------------------------
    backup_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backup_snapshots.id", ondelete="RESTRICT"), nullable=False
    )
    backup_group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backup_groups.id", ondelete="RESTRICT"), nullable=False
    )
    # Snapshotted at creation (same reasoning as `RestorePlan.customer_id`).
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    # `drive-scsi0.img.fidx`, ... in the order they are attached (disk 1, 2, ...).
    disk_archives: Mapped[list] = mapped_column(JSONB, nullable=False)

    # --- where -------------------------------------------------------------
    pve_cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pve_clusters.id", ondelete="RESTRICT"), nullable=False
    )
    target_node: Mapped[str] = mapped_column(String(63), nullable=False)
    target_storage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bridge: Mapped[str] = mapped_column(String(64), nullable=False)
    vlan_tag: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ip_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    # `static`: as entered (`192.168.10.50/24`); `cluster_range`: the allocated
    # address with the range's prefix; `dhcp`: NULL.
    ip_address_cidr: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gateway: Mapped[str | None] = mapped_column(String(45), nullable=True)

    # --- how ---------------------------------------------------------------
    protocols: Mapped[list] = mapped_column(JSONB, nullable=False)
    allowed_clients: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    smb_username: Mapped[str | None] = mapped_column(String(32), nullable=True)
    share_password_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    lifetime_hours: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- lifecycle ---------------------------------------------------------
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    # Which terminal status a `stopping` mount ends in: stopped | expired | failed.
    teardown_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Four-eyes (`PveCluster.require_restore_approval`), same rules as restore plans.
    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stop_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stop_requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- what the agent created (reported as it happens) -------------------
    helper_vmid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # [{"archive": "drive-scsi0.img.fidx", "device": "/dev/loop3"}]
    mapped_devices: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    snippet_volume: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # From the helper's result: IPv4 addresses and the per-volume report.
    helper_addresses: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    volumes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    teardown_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_teardown_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
