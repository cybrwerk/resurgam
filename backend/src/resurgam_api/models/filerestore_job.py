import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class FileRestoreJob(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """The first real implementation of ADR 0002's Postgres-native job
    queue (`SELECT ... FOR UPDATE SKIP LOCKED`) -- see ADR 0013. Both
    listing and downloading a snapshot's files now run out-of-process in
    `worker/file_restore_worker.py`, not inside the FastAPI request
    handler: CT/host downloads can block for minutes regardless of file
    size (the PBS CLI must stream the whole pxar archive), and VM
    file-restore beyond the top-level disk listing requires the calling
    process to be root, which the API process must not be.

    `backup_group_id`/`backup_snapshot_id` stay RESTRICT (the worker
    re-derives everything else, including the `Datastore`, from these) --
    matches `RestorePlan`'s FK conventions. `customer_id`/`actor_user_id`
    are SET NULL and denormalized at creation time, also matching
    `RestorePlan`, so this row keeps its provenance even if the actor or
    the customer assignment is later deleted.
    """

    __tablename__ = "file_restore_jobs"

    job_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "list" | "download" | "archive"
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # "pending" | "running" | "completed" | "failed" | "expired" | "cancelled" (pending or running;
    # the worker never overwrites it)

    backup_group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backup_groups.id", ondelete="RESTRICT"), nullable=False
    )
    backup_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backup_snapshots.id", ondelete="RESTRICT"), nullable=False
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # `list`/`download`: the entry. `archive`: the selection's common parent directory (what the
    # ZIP's paths are relative to); the selected entries themselves are in `paths`.
    path: Mapped[str] = mapped_column(String(4096), nullable=False)
    # `archive` jobs only -- the selected snapshot paths (files and/or directories).
    paths: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # `list` jobs only -- FileEntryOut[]-shaped.
    result: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # `download`/`archive` jobs only: the name the browser saves the content under.
    result_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # `download`/`archive`: size of the stored content file in bytes (the ZIP itself for archives).
    result_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # `download`/`archive`: the content file's name inside `Settings.file_restore_output_dir`
    # (`<job id>.bin` / `<job id>.zip`). Cleared when the expiry sweep deletes the file -- null on
    # a completed job means "content no longer available". Never exposed through the API.
    result_storage_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # `archive` jobs only -- zip_builder.ZipReport.as_dict(): counts, total size, skipped and
    # renamed entries.
    archive_report: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # While running (download/archive): {"phase", "entries_done", "entries_total", "bytes_done"},
    # updated by the worker so the UI can show more than "running".
    progress: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Pending: "must be claimed by" (pushed forward while the job's worker lane is busy). Running:
    # "must be done by" -- set at claim to the job's run budget plus grace. Completed
    # download/archive: "content stays downloadable until" -- reset to completion time + TTL when
    # the job finishes, so a long extraction doesn't eat the download window. The worker's sweep
    # expires stale pending/running jobs and deletes the content file of completed ones past this
    # (see ADR 0013 and worker/file_restore_worker.py).
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
