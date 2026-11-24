import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint, exists
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, aliased, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class FileIndexEntry(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One indexed file/directory entry -- manifest §13's named fields
    exactly (Snapshot-ID, Backup-Gruppe, Archiv/Disk, Pfad, Dateiname,
    Typ, Groesse, Zeitstempel). No file *contents* are ever indexed, per
    the manifest's explicit "keine Dateiinhalte indexieren".

    CASCADE, not RESTRICT, on every FK here -- unlike `FileIndexJob`
    (which RESTRICTs so a job can still re-derive datastore info after
    its snapshot exists), an entry has no purpose once its snapshot/
    group/customer is gone: it's pure derived search data, safe to
    cascade-delete. `backup_group_id`/`customer_id` are denormalized off
    the snapshot's own chain (rather than joined at query time) since the
    dominant query -- a customer- or group-scoped filename search -- is
    this feature's single most latency-sensitive path.

    `created_at` (via `CreatedAtMixin`) doubles as "indexed at" --
    retention is keyed off it, not the file's own `mtime` (a file's
    filesystem timestamp has nothing to do with how long RESURGAM should
    keep its index of it around).
    """

    __tablename__ = "file_index_entries"
    __table_args__ = (
        UniqueConstraint("file_index_job_id", "path", name="uq_file_index_entry_job_path"),
        Index("ix_file_index_entries_snapshot", "backup_snapshot_id"),
        Index("ix_file_index_entries_scope", "customer_id", "backup_group_id"),
        # Supports the Explorer-style browse endpoint's one-level
        # `path LIKE 'prefix%'` query -- a plain btree doesn't support
        # prefix-LIKE index scans under a non-C collation, only an
        # explicit text_pattern_ops index does (migration 0022).
        Index(
            "ix_file_index_entries_snapshot_path_pattern",
            "backup_snapshot_id",
            "path",
            postgresql_ops={"path": "text_pattern_ops"},
        ),
        # Trigram GIN index for substring ("contains") filename search --
        # the first `pg_trgm` usage in this codebase (see migration 0020,
        # which also runs `CREATE EXTENSION IF NOT EXISTS pg_trgm`). A
        # plain btree can only support exact/prefix matches; manifest
        # §13's "global search" is expected to support anywhere-substring
        # matches (e.g. `*.log`).
        Index(
            "ix_file_index_entries_filename_trgm",
            "filename",
            postgresql_using="gin",
            postgresql_ops={"filename": "gin_trgm_ops"},
        ),
        Index("ix_file_index_entries_linked_from", "linked_from_entry_id"),
    )

    file_index_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("file_index_jobs.id", ondelete="CASCADE"), nullable=False
    )
    backup_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backup_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    backup_group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backup_groups.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=True
    )

    # e.g. "root.pxar" (a CT; a host backup can have other or several pxar archives) or
    # "drive-scsi0.img.fidx" (VM, one per disk).
    archive: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(String(4096), nullable=False)
    # Denormalized basename of `path` -- the column the search UI's
    # filename filter (including the trigram substring index) matches
    # against, so it never has to derive this from `path` at query time.
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # file | directory | symlink | other
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mtime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # "Reuse and link" (manifest §13, ADR 0016), raised directly by the
    # user after watching a real VM re-index 250k+ largely unchanged
    # entries on every snapshot. When a newly discovered entry's (type,
    # size_bytes, mtime) exactly matches the most recently indexed entry
    # at the same path for the same group, this points at that entry
    # instead of duplicating its `size_bytes`/`mtime` -- always at a
    # *root* (never-linked) entry, one hop, never chained. `ON DELETE
    # RESTRICT` (unlike every other FK here, which CASCADE): a row other
    # rows still link to for their data is not safe to drop silently, so
    # both purge paths that can delete entries (retention sweep,
    # orphaned-snapshot cleanup) skip anything still referenced -- this
    # constraint is a backstop against a bug in that skip logic, not
    # something that should ever fire in normal operation.
    linked_from_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("file_index_entries.id", ondelete="RESTRICT"), nullable=True
    )


def not_still_linked() -> object:
    """ "Reuse and link" (manifest §13, ADR 0016): a row other, newer rows
    still link to for their own `size_bytes`/`mtime` isn't safe to purge,
    even past its own retention cutoff or once its snapshot is gone from
    PBS -- it stays alive for exactly as long as something still depends
    on it. Shared by both places that can delete `file_index_entries`
    (`worker.file_index_worker._sweep_expired_retention`,
    `collector.sync._purge_orphaned_file_index_entries`) so they apply
    the identical rule.
    """
    linker = aliased(FileIndexEntry)
    return ~exists().where(linker.linked_from_entry_id == FileIndexEntry.id)
