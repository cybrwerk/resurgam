"""Which disks a VM backup snapshot holds -- PBS `GET /admin/datastore/{store}/files`.

apidoc.js (pbs.proxmox.com/docs/api-viewer, checked 2026-10-08): "List snapshot
files." Parameters `store`, `ns` (optional), `backup-type`, `backup-id`,
`backup-time` (Unix epoch); returns `[{filename, size?, crypt-mode?}]`
("Returns the list of archive files inside a backup snapshots."). Permission:
Datastore.Audit or Datastore.Read on the datastore/namespace -- RESURGAM's
PBS token already needs Audit for everything else. A VM disk is a fixed-index
archive named `drive-<slot>.img.fidx` (the same name `proxmox-backup-client
map` takes, ADR 0019).
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from resurgam_api.connectivity.models import TrustConfig
from resurgam_api.connectivity.pbs_api import _get_data

DISK_ARCHIVE_SUFFIX = ".img.fidx"


@dataclass(frozen=True)
class SnapshotDisk:
    archive: str
    slot: str
    size_bytes: int | None
    crypt_mode: str | None


class SnapshotFilesError(Exception):
    pass


def snapshot_path(backup_type: str, backup_id: str, snapshot_time: datetime) -> str:
    """`vm/116/2026-09-14T00:48:57Z` -- proxmox-backup-client's snapshot notation
    (same format as `restore.command.pbs_volid` and the lab agent)."""
    return f"{backup_type}/{backup_id}/{snapshot_time.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}"


def archive_slot(archive: str) -> str:
    return archive.removeprefix("drive-").removesuffix(DISK_ARCHIVE_SUFFIX)


def list_snapshot_disks(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    timeout: float,
    *,
    datastore: str,
    namespace: str,
    backup_type: str,
    backup_id: str,
    snapshot_time: datetime,
) -> list[SnapshotDisk]:
    params: dict = {
        "backup-type": backup_type,
        "backup-id": backup_id,
        "backup-time": int(snapshot_time.timestamp()),
    }
    if namespace:
        params["ns"] = namespace
    data, error = _get_data(host, port, trust, token, f"/api2/json/admin/datastore/{datastore}/files", timeout, params)
    if error is not None:
        raise SnapshotFilesError(error)
    if not isinstance(data, list):
        raise SnapshotFilesError("unexpected response shape: data is not a list")
    disks = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("filename") or "")
        if not name.startswith("drive-") or not name.endswith(DISK_ARCHIVE_SUFFIX):
            continue
        size = entry.get("size")
        disks.append(
            SnapshotDisk(
                archive=name,
                slot=archive_slot(name),
                size_bytes=int(size) if isinstance(size, int) else None,
                crypt_mode=entry.get("crypt-mode"),
            )
        )
    return sorted(disks, key=lambda d: d.archive)
