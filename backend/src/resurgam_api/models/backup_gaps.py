import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class GuestGapReason(str, Enum):
    # PVE's own `GET /cluster/backup-info/not-backed-up` lists the guest.
    NO_JOB = "no_job"
    # Only disabled jobs cover it -- PVE's endpoint counts those as coverage
    # (pve-manager `get_included_vmids` ignores `enabled`), so RESURGAM
    # finds these itself (ADR 0032).
    ONLY_DISABLED_JOBS = "only_disabled_jobs"


class PveGuestBackupGap(Base, UUIDPrimaryKeyMixin):
    """A PVE guest that no enabled backup job covers, as of the cluster's last
    check (`PveCluster.backup_gaps_checked_at`). A live mirror refreshed by the
    collector: a guest that is covered again (or removed) loses its row.
    `first_seen_at` survives every refresh, so the UI can say how long the
    guest has been unprotected.
    """

    __tablename__ = "pve_guest_backup_gaps"
    __table_args__ = (UniqueConstraint("pve_cluster_id", "vmid", name="uq_pve_guest_backup_gap"),)

    pve_cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pve_clusters.id", ondelete="CASCADE"), nullable=False
    )
    vmid: Mapped[int] = mapped_column(Integer, nullable=False)
    guest_type: Mapped[str] = mapped_column(String(10), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str] = mapped_column(String(30), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GapExceptionKind(str, Enum):
    PVE_GUEST = "pve_guest"
    PVE_CLUSTER = "pve_cluster"
    BACKUP_GROUP = "backup_group"
    DATASTORE = "datastore"


class BackupGapException(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """An operator accepting a 3-2-1 gap with a reason (ADR 0032). Append-only
    like `ComplianceAttestation` (migration 0073's trigger rejects UPDATE and
    DELETE): the newest row per target is the current state, revoking writes a
    row with `active = False`, and every earlier row stays as history.

    Which target columns are set depends on `target_kind`:
    `pve_guest` -> `pve_cluster_id` + `vmid`; `pve_cluster` -> `pve_cluster_id`;
    `backup_group` -> `datastore_id` + `namespace` + `backup_type` + `backup_id`
    (the group's stable identity, so the exception survives a new generation);
    `datastore` -> `datastore_id`. No foreign keys on these on purpose: the
    history must outlive a removed cluster or datastore.
    """

    __tablename__ = "backup_gap_exceptions"

    target_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    pve_cluster_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    vmid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    datastore_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    namespace: Mapped[str | None] = mapped_column(String(255), nullable=True)
    backup_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    backup_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
