import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import UUIDPrimaryKeyMixin


class UsageMeasurement(Base, UUIDPrimaryKeyMixin):
    """Manifest §9.3/§8: a point-in-time capacity snapshot for one
    datastore. Append-only per manifest §8's "Messwerte append-only
    erfassen" — never updated, only inserted, one row per Collector sync
    pass (on-demand only, ADR 0006, same cadence as everything else the
    Collector captures).

    `used_bytes`/`available_bytes`/`total_bytes` come from PBS's own
    datastore-usage endpoint (confirmed live against a real PBS 4.2
    instance) — the manifest §9.3 "PBS-Datastore-Belegung": physical,
    deduplicated-and-compressed disk usage. `logical_bytes` is the
    manifest's separately-named "logisch geschützter Datenmenge" — the sum
    of every current snapshot's own (undeduplicated) size, from
    RESURGAM's own already-synced `backup_snapshots` table, not a second
    PBS call. The gap between the two is the dedup/compression ratio PBS
    is providing; the manifest's other two distinctions ("darunterliegende
    Dateisystembelegung" and "temporärer Restore-Speicher") aren't
    separately tracked — the former is assumed identical to PBS's own
    `total`/`avail` since a datastore is its own filesystem mountpoint,
    and the latter has no restore-worker integration yet to measure. The
    group/snapshot counts and GC timestamp are likewise computed from
    RESURGAM's own already-synced `backup_groups`/`backup_snapshots`/
    `task_runs` tables, not a second PBS call.
    """

    __tablename__ = "usage_measurements"

    datastore_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datastores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    available_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    logical_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    group_count_vm: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    group_count_ct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    group_count_host: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshot_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    oldest_snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    newest_snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_successful_gc_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # S3-backed datastores only (ADR 0033). `used/available/total_bytes` are
    # then the local cache filesystem; `s3_data_bytes` is the chunk data the
    # last garbage collection found (`gc-status.disk-bytes`) -- the bucket's
    # size, null until a GC ran. The `s3_*` counters are PBS's `s3-statistics`
    # as read at `captured_at`: since the datastore's last counter reset, so
    # deltas between rows must allow for a reset (see `inventory.usage`).
    s3_data_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    s3_get_requests: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    s3_put_requests: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    s3_post_requests: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    s3_head_requests: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    s3_delete_requests: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    s3_uploaded_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    s3_downloaded_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
