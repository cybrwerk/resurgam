import uuid
from datetime import datetime, time

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Time
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class CustomerFileIndexSettings(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Per-customer opt-in + config for manifest §13's optional global
    search (`FileIndexJob`/`FileIndexEntry`). Unlike `PricingPlan`
    (nullable `customer_id` = a sparse global default plus a sparse
    per-customer override), there is no meaningful "global default" here
    -- every field (enabled, retention, schedule, window) is inherently
    customer-specific -- so `customer_id` is a plain non-null UNIQUE
    column, not a partial-unique-index-enforced optional override.

    Row absence means "never opted in"; `enabled` stays a real column
    (rather than "delete the row to disable") so an admin can pause a
    customer's indexing without losing their configured retention/
    schedule/window -- deleting this row is a separate, explicit action
    that also purges that customer's `file_index_entries`
    (`api/v1/file_index.py`'s DELETE endpoint), not something toggling
    `enabled` off ever does implicitly.
    """

    __tablename__ = "customer_file_index_settings"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False)

    schedule_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Interval-since-last-run, not cron syntax -- matches this feature's
    # "poll-based, scoped only to file indexing, no new cron/timer
    # component" scheduling design (no general scheduler exists anywhere
    # in this codebase yet, ADR 0006).
    schedule_interval_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Daily allowed window, UTC time-of-day (matches the rest of this
    # database being UTC throughout). Both null = no window configured =
    # unrestricted. Required together if either is set (validated at the
    # schema/endpoint level, not the DB) -- an overnight window (e.g.
    # 22:00-04:00) is handled via wraparound comparison in the worker,
    # not by storing two rows.
    window_start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    window_end_time: Mapped[time | None] = mapped_column(Time, nullable=True)

    # The worker reads/advances this itself each poll cycle rather than
    # recomputing "is this due" from interval + last-run every cycle.
    next_scheduled_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Guest-exec File Indexing (ADR 0021). The default lab-enabled PVE
    # cluster a BackupGroup.file_index_method="guest_exec_ephemeral"
    # job boots its disposable clone on -- validated (at job-creation
    # time) only once a group actually uses that method; not needed at
    # all for "guest_exec_live", which runs on whichever cluster
    # already hosts the VM.
    guest_exec_pve_cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pve_clusters.id", ondelete="SET NULL"), nullable=True
    )
    # The node/storage every "guest_exec_ephemeral" job's disposable
    # clone boots on within that cluster -- same "operator sets it once"
    # precedent as the cluster field itself, since job creation
    # (manual/bulk/scheduled) has no per-job place to ask for these the
    # way Recovery Lab's own plan-creation request does.
    guest_exec_target_node: Mapped[str | None] = mapped_column(String(255), nullable=True)
    guest_exec_target_storage: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Shared by both guest-exec methods: an incremental (-newermt) pass
    # only reports new/modified files, never deletions/renames. Every
    # Nth index for a group forces a full walk instead and purges any
    # previously-indexed path not seen in that run -- the same
    # trade-off backup tools with periodic "active full" runs already
    # make. None disables periodic full reconciliation (an explicit,
    # documented risk if left off: deleted/renamed paths accumulate as
    # stale entries forever).
    full_reindex_every_n_runs: Mapped[int | None] = mapped_column(Integer, nullable=True)
