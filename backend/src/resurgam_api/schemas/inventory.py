from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

FileIndexMethod = Literal["external_mount", "guest_exec_live", "guest_exec_ephemeral"]


class BackupGroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    datastore_id: UUID
    namespace: str
    backup_type: str
    backup_id: str
    resolved_name: str | None
    owner: str | None
    last_backup_at: datetime | None
    last_verify_status: str
    protection_policy_id: UUID | None
    protection_status: str
    customer_id: UUID | None
    effective_customer_id: UUID | None
    file_index_enabled: bool
    file_index_exclude_patterns: list[str]
    file_index_method: FileIndexMethod = Field(
        description="How this group is indexed: 'external_mount' (PBS file-restore/catalog tools, default), "
        "'guest_exec_live' (a PVE hookscript indexes the live production VM at its own real backup's completion), "
        "or 'guest_exec_ephemeral' (boots a disposable snapshot clone, indexes it, destroys it). VM-only in practice."
    )
    guest_exec_live_pve_cluster_id: UUID | None = Field(
        default=None,
        description="Required only for 'guest_exec_live': the PVE cluster hosting this group's production VM -- "
        "where resurgam-lab-agent sets the hookscript. Distinct from any lab-compute cluster used for "
        "'guest_exec_ephemeral'.",
    )
    last_synced_at: datetime
    superseded_at: datetime | None = Field(
        default=None,
        description=(
            "Set when this VMID slot's guest identity changed (a reused VMID) and this "
            "row was closed out in favor of a new generation -- see collector.sync's "
            "split-on-rename behavior. Null means this is the current generation."
        ),
    )
    latest_crypt_mode: str | None = Field(
        default=None,
        description="Encryption of this group's newest backup ('none', 'encrypt', 'sign-only', 'mixed'), "
        "or null when not known yet.",
    )
    latest_key_fingerprint: str | None = Field(
        default=None,
        description="Key fingerprint of this group's newest backup, null if it is unencrypted.",
    )
    key_fingerprints: list[str] = Field(
        default_factory=list,
        description="Every encryption key fingerprint used by any backup of this group, oldest first. More than "
        "one means the key was changed: older and newer backups need different keys.",
    )
    key_changed: bool = Field(
        default=False,
        description="True when the newest backup uses a different key (or no key) than the backup before it.",
    )
    removed_from_pbs_at: datetime | None = Field(
        default=None,
        description=(
            "Set when the Collector's most recent sync no longer found this backup on PBS at "
            "all (deleted/pruned, not renamed -- see superseded_at for that case). History is "
            "preserved; this only means it's no longer current. Cleared automatically if PBS "
            "reports it again in a later sync."
        ),
    )


class SetFileIndexExcludePatternsRequest(BaseModel):
    patterns: list[str]


class SetFileIndexMethodRequest(BaseModel):
    method: FileIndexMethod
    guest_exec_live_pve_cluster_id: UUID | None = Field(
        default=None,
        description="Required when method is 'guest_exec_live' -- the PVE cluster hosting this group's "
        "production VM. Ignored for the other two methods.",
    )


class FileIndexDefaultExcludePatternsOut(BaseModel):
    patterns: list[str] = Field(
        description=(
            "The regex patterns every newly synced backup group starts with (see "
            "BackupGroup.file_index_exclude_patterns's default). Exposed read-only so the "
            "frontend can offer to reapply the same list to a group whose patterns were "
            "cleared or customized, without hardcoding a second copy of it."
        )
    )


class BackupSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    backup_group_id: UUID
    snapshot_time: datetime
    verify_status: str
    size_bytes: int | None
    crypt_mode: str | None = Field(
        default=None,
        description="Client-side encryption of this backup's data as PBS reports it: 'none', 'encrypt', "
        "'sign-only' (signed but readable), 'mixed' (archives differ), or null when not known yet "
        "(the next inventory sync fills it in).",
    )
    key_fingerprint: str | None = Field(
        default=None,
        description="Fingerprint of the encryption key this backup was made with (e.g. 'ad:a6:9b:...'), "
        "null for an unencrypted backup. Reading the backup needs exactly this key.",
    )
    last_synced_at: datetime


class TaskRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    pbs_instance_id: UUID
    upid: str
    task_type: str
    worker_id: str | None
    status: str
    started_at: datetime
    ended_at: datetime | None
    task_user: str | None
    last_synced_at: datetime


class TaskSyncOut(BaseModel):
    reachable: bool
    tasks_created: int
    tasks_updated: int
    error: str | None


class DatastoreSyncOut(BaseModel):
    datastore_id: UUID
    datastore_name: str
    reachable: bool
    groups_created: int
    groups_updated: int
    groups_removed: int
    snapshots_created: int
    snapshots_updated: int
    error: str | None


class SyncJobSyncOut(BaseModel):
    reachable: bool
    sync_jobs_created: int
    sync_jobs_updated: int
    sync_jobs_removed: int = Field(
        default=0,
        description="Sync jobs no longer configured on PBS whose RESURGAM mirror row was deleted this pass.",
    )
    error: str | None


class DatastoreDiscoveryOut(BaseModel):
    """Automatic datastore discovery, run at the start of every Collector pass."""

    reachable: bool = Field(description="Whether PBS's datastore configuration (GET /config/datastore) was readable.")
    created: list[str] = Field(
        default_factory=list, description="Datastores newly found on PBS and imported automatically (unassigned)."
    )
    updated: list[str] = Field(default_factory=list, description="Known datastores whose path or comment changed.")
    removed: list[str] = Field(
        default_factory=list,
        description="Datastores PBS no longer lists, newly marked removed_from_pbs_at (nothing is deleted).",
    )
    restored: list[str] = Field(
        default_factory=list, description="Previously removed datastores that PBS lists again (mark cleared)."
    )
    error: str | None = Field(default=None, description="Connectivity error, when reachable is false.")


class AclSyncOut(BaseModel):
    reachable: bool = Field(description="Whether the ACL listing call to PBS succeeded.")
    entries_synced: int = Field(
        description="How many ACL entries PBS reported this pass (a full replace, not a delta)."
    )
    error: str | None = Field(default=None, description="Connectivity error, when reachable is false.")


class PbsInstanceSyncOut(BaseModel):
    tasks: TaskSyncOut
    sync_jobs: SyncJobSyncOut
    acl: AclSyncOut
    datastores: list[DatastoreSyncOut]
    discovery: DatastoreDiscoveryOut | None = Field(
        default=None,
        description="Datastore discovery result (new / updated / removed / restored datastore names) for this pass.",
    )


class SyncJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    pbs_instance_id: UUID
    job_id: str
    remote_name: str
    remote_store: str
    local_store_name: str
    local_datastore_id: UUID | None
    schedule: str | None
    remove_vanished: bool | None
    comment: str | None
    last_synced_at: datetime
