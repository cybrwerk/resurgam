import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FileEntryOut(BaseModel):
    """One entry in a snapshot's file listing — the common shape both the
    CT/host (pxar catalog) and VM (proxmox-file-restore) backends are
    normalized into, so the frontend's browser doesn't need to know which
    one answered a given request.
    """

    path: str
    name: str
    type: Literal["file", "directory", "symlink", "other"]
    size: int | None = None
    mtime: str | None = None


CapabilityLocation = Literal["appliance_host", "local"]
_LOCATION_DESCRIPTION = (
    "Where the check ran: `appliance_host` = natively on the appliance VM, as the file-restore/file-index "
    "workers' own identity (reported by resurgam-host-probe.service -- all file-restore work runs there, never in "
    "the API container); `local` = in the API's own process (development, where API and workers share one host)."
)


class CapabilityCheckOut(BaseModel):
    name: str = Field(
        description="What was checked: a tool (`proxmox-backup-client`, `proxmox-file-restore`, "
        "`proxmox-backup-restore-image`, `pve-qemu-kvm`), a device (`/dev/kvm`, `/dev/vhost-vsock`) or a directory "
        "the tools must be able to write (`/run/proxmox-backup`, `/var/log/proxmox-backup/file-restore`)."
    )
    status: Literal["ok", "degraded", "missing"] = Field(
        description="`ok` = usable; `degraded` = present but partly unusable; `missing` = absent or unusable."
    )
    message: str = Field(description="What was found, e.g. the tool's version or why the check failed.")
    location: CapabilityLocation = Field(default="local", description=_LOCATION_DESCRIPTION)


class BackupTypeCapabilityOut(BaseModel):
    backup_type: Literal["vm", "ct", "host"] = Field(
        description="PBS backup type: `vm` (disk images, browsed via proxmox-file-restore's micro-VM), `ct`/`host` "
        "(pxar archives, browsed via proxmox-backup-client's catalog)."
    )
    available: bool = Field(description="True when every check this backup type's file browsing needs is `ok`.")
    location: CapabilityLocation = Field(description=_LOCATION_DESCRIPTION)
    missing: list[str] = Field(
        description="Names of the failed checks (see `checks`) -- empty when `available` is true."
    )
    message: str = Field(
        description="Operator-facing summary: what is missing, on which side, and how to fix it (e.g. which update "
        "or console command installs the tools on the appliance host)."
    )


class CapabilityReportOut(BaseModel):
    """File-restore capability (manifest §18.12): never fails, every check resolves to a status."""

    checks: list[CapabilityCheckOut] = Field(description="Every individual check, in a stable order.")
    backup_types: list[BackupTypeCapabilityOut] = Field(
        default_factory=list, description="Per backup type: can its files be browsed/downloaded here, and if not, why."
    )
    location: CapabilityLocation = Field(default="local", description=_LOCATION_DESCRIPTION)
    checked_at: str | None = Field(
        default=None,
        description="When the checks ran (ISO 8601) -- on the appliance the host probe's last run (at boot, after "
        "each update's host setup, or `systemctl restart resurgam-host-probe`); null for a live local check.",
    )


FileRestoreJobType = Literal["list", "download", "archive"]


class FileRestoreJobCreateRequest(BaseModel):
    """What to do in the snapshot. `list` and `download` take one `path`; `archive` takes
    `paths` (one or more files and/or directories) and produces a single ZIP."""

    job_type: FileRestoreJobType = Field(
        description="`list` = list the directory at `path`; `download` = one file at `path`; `archive` = the "
        "files and/or directories in `paths` as one ZIP (folder download, multi-selection download)."
    )
    path: str | None = Field(
        default=None,
        max_length=4096,
        description="Absolute path inside the snapshot, as returned in a listing's `path` field (e.g. `/etc/hosts`, "
        "or for a VM `/drive-scsi0.img.fidx/part/2/Users`). Required for `list` and `download`; not used for "
        "`archive`.",
    )
    paths: list[str] | None = Field(
        default=None,
        description="`archive` only: the selected entries' absolute snapshot paths (files and/or directories of "
        "this one snapshot). Inside the ZIP every entry keeps its path relative to the selection's common parent "
        "directory. Duplicates and entries inside another selected directory are dropped. At most "
        "`FILE_RESTORE_ARCHIVE_MAX_ENTRIES` (default 1000) entries; the snapshot root `/` itself can't be selected.",
    )


class ArchiveSkippedEntryOut(BaseModel):
    path: str = Field(description="Path inside the snapshot of the entry that is NOT in the ZIP.")
    reason: str = Field(description="Why it was skipped, e.g. not found, unreadable, or a device/FIFO/socket.")


class ArchiveRenamedEntryOut(BaseModel):
    path: str = Field(description="Original path inside the snapshot.")
    archive_name: str = Field(
        description="The name it has inside the ZIP -- changed because Windows can't create the original (e.g. "
        "`:` or `?` in it, a reserved name like `CON`, trailing dots) or because it only differed in case from "
        "another name in the same folder."
    )


class ArchiveReportOut(BaseModel):
    """What a completed `archive` job's ZIP contains -- the skipped list is the place to look
    before relying on the archive being complete."""

    file_count: int = Field(description="Regular files in the ZIP.")
    directory_count: int = Field(description="Directory entries in the ZIP (empty directories included).")
    symlink_count: int = Field(description="Symbolic links, stored as links (not followed).")
    total_bytes: int = Field(description="Uncompressed size of all files in the ZIP, in bytes.")
    skipped_count: int = Field(description="Entries that could not be added; the first 200 are listed in `skipped`.")
    renamed_count: int = Field(description="Entries stored under a changed name; the first 200 are in `renamed`.")
    skipped: list[ArchiveSkippedEntryOut] = Field(description="Skipped entries with the reason (up to 200).")
    renamed: list[ArchiveRenamedEntryOut] = Field(description="Renamed entries (up to 200).")


class FileRestoreJobProgressOut(BaseModel):
    phase: Literal["extracting", "packing"] = Field(
        description="`extracting` = the PBS tool is extracting the selection from the backup; `packing` = the ZIP "
        "is being written."
    )
    entries_done: int = Field(description="Selected entries finished so far.")
    entries_total: int = Field(description="Selected entries in total.")
    bytes_done: int = Field(description="Uncompressed bytes written into the ZIP so far.")


class FileRestoreJobOut(BaseModel):
    """Polled by the frontend (ADR 0013) until `status` reaches a
    terminal value. `result` is only populated for a completed `list` job;
    a completed `download`/`archive` job's content is fetched separately via
    `GET /file-restore/jobs/{id}/download` (or a link from `POST .../download-ticket`), not
    embedded here.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="Job id.")
    job_type: FileRestoreJobType = Field(description="See `FileRestoreJobCreateRequest.job_type`.")
    status: Literal["pending", "running", "completed", "failed", "expired", "cancelled"] = Field(
        description="`pending` = queued for the file-restore worker; `running`; `completed`; `failed` (see "
        "`error_code`/`error_message`); `expired` = not finished within the job TTL; `cancelled`."
    )
    workload_label: str = Field(
        description="The backup group's resolved name and VMID/CTID (e.g. 'Office-PC (116)') -- resolved "
        "server-side so a job list never needs a second lookup just to show which workload a job belongs to."
    )
    path: str = Field(
        description="`list`/`download`: the requested path. `archive`: the selection's common parent directory -- "
        "what the paths inside the ZIP are relative to."
    )
    paths: list[str] | None = Field(default=None, description="`archive` only: the selected entries.")
    result: list[FileEntryOut] | None = Field(default=None, description="Completed `list` job: the directory entries.")
    result_filename: str | None = Field(
        default=None, description="Completed `download`/`archive` job: the file name the content is saved under."
    )
    result_size_bytes: int | None = Field(
        default=None, description="Completed `download`/`archive` job: size of the content (the ZIP itself) in bytes."
    )
    content_available: bool = Field(
        default=False,
        description="True while a completed `download`/`archive` job's content can still be downloaded; false once "
        "its download window (`expires_at`) has passed and the file was deleted.",
    )
    archive_report: ArchiveReportOut | None = Field(
        default=None, description="Completed `archive` job: what the ZIP contains, and what was skipped or renamed."
    )
    progress: FileRestoreJobProgressOut | None = Field(
        default=None, description="Running `archive` job: how far the worker is. Null otherwise."
    )
    error_code: str | None = Field(
        default=None,
        description="Failed job: machine-readable reason, e.g. `file_restore_failed`, `file_restore_unavailable`, "
        "`content_too_large`, `insufficient_disk_space`, `nothing_extracted`, `internal_error`.",
    )
    error_message: str | None = Field(default=None, description="Failed job: what went wrong and how to fix it.")
    expires_at: datetime = Field(
        description="Pending/running: the job must be finished by then. Completed download/archive: the content "
        "stays downloadable until then (the window starts when the job completes)."
    )
    started_at: datetime | None = Field(default=None, description="When the worker picked the job up.")
    completed_at: datetime | None = Field(default=None, description="When the job reached a terminal status.")
    created_at: datetime = Field(description="When the job was created.")


class DownloadTicketOut(BaseModel):
    download_url: str = Field(
        description="Path, relative to the API base (`/api/v1`), that downloads the content without an "
        "`Authorization` header -- for a plain browser navigation, so the file streams straight to disk instead of "
        "being buffered in the page. Carries a signed ticket bound to this job and user."
    )
    expires_at: datetime = Field(
        description="The link must be opened before this (default 60 s, FILE_RESTORE_DOWNLOAD_TICKET_TTL_SECONDS); "
        "a download already started keeps running."
    )
