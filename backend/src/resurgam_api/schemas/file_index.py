import uuid
from datetime import datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FileIndexJobStatus = Literal["pending", "running", "paused", "completed", "failed", "cancelled"]
FileIndexJobTrigger = Literal["manual", "bulk", "scheduled", "guest_exec_hook"]
FileIndexMethod = Literal["external_mount", "guest_exec_live", "guest_exec_ephemeral"]


class FileIndexJobOut(BaseModel):
    """Polled by the frontend (mirrors `FileRestoreJobOut`'s shape, ADR
    0013) until `status` reaches a terminal value -- though unlike a
    file-restore job, `paused` is a real, expected non-terminal state a
    caller may see repeatedly (waiting for its customer's time window).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: FileIndexJobStatus
    trigger: FileIndexJobTrigger
    file_index_method: FileIndexMethod = Field(
        description="How this job indexes: 'external_mount' (PBS file-restore/catalog tools, unchanged), "
        "'guest_exec_live' (a PVE hookscript ran guest-exec against the already-running production VM at its own real backup), "
        "or 'guest_exec_ephemeral' (a disposable snapshot clone was booted, indexed, and destroyed)."
    )
    guest_exec_target_vmid: int | None = Field(
        default=None,
        description="For a guest-exec method: the VMID indexing actually ran against -- a freshly allocated clone for "
        "'guest_exec_ephemeral', or the production VM's own real VMID for 'guest_exec_live'.",
    )
    guest_exec_target_node: str | None = Field(default=None, description="The PVE node guest_exec_target_vmid runs on.")
    backup_group_id: uuid.UUID
    workload_label: str = Field(
        description="The backup group's resolved name and VMID/CTID (e.g. 'Office-PC (116)') -- resolved "
        "server-side so a job list never needs a second lookup just to show which workload a job belongs to."
    )
    backup_snapshot_id: uuid.UUID
    entries_indexed: int
    current_path: str | None = None
    cancel_requested: bool
    error_code: str | None = None
    error_message: str | None = None
    paused_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime


class BulkFileIndexJobResult(BaseModel):
    created_job_ids: list[uuid.UUID]
    skipped_already_indexed: int


class FileIndexEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    backup_snapshot_id: uuid.UUID
    backup_group_id: uuid.UUID
    archive: str
    path: str
    filename: str
    type: Literal["file", "directory", "symlink", "other"]
    size_bytes: int | None = None
    mtime: datetime | None = None


class FileIndexSearchResponse(BaseModel):
    items: list[FileIndexEntryOut]
    has_more: bool


class SnapshotIndexStatusOut(BaseModel):
    """Batched "which of this group's snapshots have a completed index"
    answer -- the Unified File Workspace's calendar/tree/browser all need
    this once per group, not once per snapshot. Raised directly by the
    user: the frontend previously had no real way to know this at all,
    only a fragile "is the root listing empty" heuristic.
    """

    indexed_snapshot_ids: list[uuid.UUID]


class CustomerFileIndexSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    customer_id: uuid.UUID
    enabled: bool
    retention_days: int
    schedule_enabled: bool
    schedule_interval_hours: int | None = None
    window_start_time: time | None = None
    window_end_time: time | None = None
    next_scheduled_run_at: datetime | None = None
    guest_exec_pve_cluster_id: uuid.UUID | None = Field(
        default=None,
        description="Default lab-enabled PVE cluster a 'guest_exec_ephemeral' job's disposable clone boots on.",
    )
    guest_exec_target_node: str | None = Field(
        default=None, description="Default PVE node for 'guest_exec_ephemeral' boots."
    )
    guest_exec_target_storage: str | None = Field(
        default=None, description="Default PVE storage for 'guest_exec_ephemeral' boots."
    )
    full_reindex_every_n_runs: int | None = Field(
        default=None,
        description="Every Nth guest-exec index for a group forces a full walk (instead of incremental) and purges "
        "stale entries not seen in that run -- reconciles deletions/renames an incremental pass can't see. "
        "Null disables periodic full reconciliation.",
    )


def _validate_window(schedule_interval_hours: int | None, schedule_enabled: bool, window_start, window_end) -> None:
    if schedule_enabled and schedule_interval_hours is None:
        raise ValueError("schedule_interval_hours is required when schedule_enabled is true")
    if (window_start is None) != (window_end is None):
        raise ValueError("window_start_time and window_end_time must be set together")


class CustomerFileIndexSettingsCreateRequest(BaseModel):
    enabled: bool = True
    retention_days: int
    schedule_enabled: bool = False
    schedule_interval_hours: int | None = None
    window_start_time: time | None = None
    window_end_time: time | None = None
    guest_exec_pve_cluster_id: uuid.UUID | None = None
    guest_exec_target_node: str | None = None
    guest_exec_target_storage: str | None = None
    full_reindex_every_n_runs: int | None = None

    @model_validator(mode="after")
    def _check(self) -> "CustomerFileIndexSettingsCreateRequest":
        _validate_window(
            self.schedule_interval_hours, self.schedule_enabled, self.window_start_time, self.window_end_time
        )
        return self


class CustomerFileIndexSettingsUpdateRequest(BaseModel):
    enabled: bool | None = None
    retention_days: int | None = None
    schedule_enabled: bool | None = None
    schedule_interval_hours: int | None = None
    window_start_time: time | None = None
    window_end_time: time | None = None
    guest_exec_pve_cluster_id: uuid.UUID | None = None
    guest_exec_target_node: str | None = None
    guest_exec_target_storage: str | None = None
    full_reindex_every_n_runs: int | None = None
