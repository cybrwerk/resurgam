import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class FileIndexJob(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Manifest §13's reopened persistent File Indexing feature (ADR
    0016 -- see docs/adr for the full design). One row per (group,
    snapshot) index attempt -- a bulk "index all snapshots" trigger
    creates N of these (one per snapshot lacking a live index), not one
    row that internally iterates snapshots: both `catalog.dump_catalog()`
    and `file_restore.list_path()` take a single `snapshot` argument, so
    representing bulk as many rows lets it reuse the exact same claim/
    progress/pause/cancel machinery a manual per-snapshot trigger uses.

    `backup_group_id`/`backup_snapshot_id` stay RESTRICT, matching
    `FileRestoreJob`/`RestorePlan` -- the worker re-derives the
    datastore/repository from these on every run.

    Unlike `FileRestoreJob`, there is no `expires_at` TTL: a job can be
    legitimately `paused` for hours (its customer's time window closed
    for most of the day), which a single "must complete by" deadline
    can't distinguish from a crashed worker. `last_heartbeat_at` is the
    liveness signal instead -- updated at every checkpoint (batch
    flush / directory visited / pause decision) whether the job is
    actively running or correctly paused; only a heartbeat that stops
    advancing means the worker died mid-job.
    """

    __tablename__ = "file_index_jobs"
    __table_args__ = (
        Index("ix_file_index_jobs_status_created_at", "status", "created_at"),
        Index("ix_file_index_jobs_snapshot_status", "backup_snapshot_id", "status"),
        Index("ix_file_index_jobs_group_created_at", "backup_group_id", "created_at"),
        Index("ix_file_index_jobs_heartbeat", "status", "last_heartbeat_at"),
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # "pending" | "running" | "paused" | "completed" | "failed" | "cancelled"
    # "paused" is claimable exactly like "pending" -- it exists purely so
    # the UI can show "waiting for its time window" instead of "never
    # started".
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)
    # "manual" | "bulk" | "scheduled" | "guest_exec_hook" (ADR 0021 --
    # created by a PVE hookscript firing at a real backup's completion,
    # relayed via resurgam-lab-agent, see api/v1/lab_agent.py)

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
    # Null for trigger="scheduled" -- no human actor initiated it.
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Guest-exec File Indexing (ADR 0021). Snapshotted from
    # BackupGroup.file_index_method at job-creation time -- matches
    # RestoreTestPlan.customer_id's own "snapshot at creation, don't
    # re-read mutable parent state" precedent, so the agent's dispatch
    # and the UI's rendering don't need a join back to BackupGroup. Only
    # ever "external_mount" (unchanged worker path), "guest_exec_live",
    # or "guest_exec_ephemeral".
    file_index_method: Mapped[str] = mapped_column(String(20), nullable=False, default="external_mount")
    # For "guest_exec_ephemeral": a freshly allocated clone VMID (via
    # next_free_vmid, same as Recovery Lab plan creation). For
    # "guest_exec_live": the production VM's own real VMID/node, set
    # directly from the hookscript trigger payload. Null for
    # "external_mount".
    guest_exec_target_vmid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    guest_exec_target_node: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Decided once, at claim time (api/v1/lab_agent.py::claim_lab_agent_file_index_job),
    # from the group's guest-exec job history and
    # CustomerFileIndexSettings.full_reindex_every_n_runs: true for a
    # group's first-ever guest-exec run, or every Nth run thereafter.
    # Governs both the `since` cutoff handed to the agent (None on a full
    # run) and whether entry-completion carries forward unchanged rows
    # from the prior run (skipped on a full run -- a full run's own
    # fresh walk is already deletion-accurate, so nothing needs carrying
    # forward, which is what actually reconciles deletions/renames an
    # incremental -newermt pass can never see).
    is_full_reindex: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- progress / resumption ---
    entries_indexed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # VM only: the directory currently/about to be visited.
    current_path: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    # VM only: the not-yet-visited directory frontier -- lets a paused/
    # budget-checkpointed job resume exactly where it left off instead of
    # re-walking from "/". CT/host leaves this null: `dump_catalog()` has
    # no partial-fetch mode (confirmed via its own docstring), so CT/host
    # resume just re-issues the whole (cheap, sub-second) call and relies
    # on `file_index_entries`'s (file_index_job_id, path) unique
    # constraint to no-op re-inserts already written before the pause.
    pending_paths: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
