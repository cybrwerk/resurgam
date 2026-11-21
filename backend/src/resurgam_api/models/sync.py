import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import UUIDPrimaryKeyMixin


class SyncJob(Base, UUIDPrimaryKeyMixin):
    """Manifest §8 names `sync_jobs` as a core entity: a PBS "Sync Job"
    config (pull-based replication from a remote PBS into a local
    datastore) — config data, upserted by the Collector like
    `PbsInstance`/`BackupGroup`, not append-only like `UsageMeasurement`.

    Field shapes come from PBS documentation only, not a verified live
    example (docs/milestone-1-pbs-pve-api-assumptions.md) — this table
    stores exactly what `connectivity.pbs_api.list_sync_jobs()` parses,
    nothing more.
    """

    __tablename__ = "sync_jobs"
    __table_args__ = (UniqueConstraint("pbs_instance_id", "job_id", name="uq_sync_job_identity"),)

    pbs_instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pbs_instances.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[str] = mapped_column(String(255), nullable=False)
    remote_name: Mapped[str] = mapped_column(String(255), nullable=False)
    remote_store: Mapped[str] = mapped_column(String(200), nullable=False)
    local_store_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Best-effort link to a RESURGAM-imported Datastore matching
    # local_store_name on this PBS instance — null if that datastore
    # hasn't been imported (or no longer matches by name).
    local_datastore_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datastores.id", ondelete="SET NULL"), nullable=True
    )
    schedule: Mapped[str | None] = mapped_column(String(255), nullable=True)
    remove_vanished: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    comment: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
