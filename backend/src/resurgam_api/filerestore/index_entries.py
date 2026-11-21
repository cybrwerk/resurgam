"""Transforms raw `pbs_client` listing results into `FileIndexEntry`-shaped
rows for manifest §13's persistent File Indexing (ADR 0016) -- separate
from `browse.py`, which stays focused on on-demand, one-directory-at-a-time
browsing. The actual walk (looping over directories for the VM path,
checkpointing between them) lives in `worker/file_index_worker.py`, not
here -- this module is pure, side-effect-free row construction so it can
be unit-tested without a database or a subprocess.
"""

import uuid
from datetime import UTC, datetime

from resurgam_worker.pbs_client.models import CatalogEntry, FileRestoreEntryType, FileRestoreListEntry

from resurgam_api.filerestore.browse import CT_HOST_ARCHIVE_NAME

# Both `dump_catalog()` (CT/host) and `file_restore.list_path()` (VM)
# include a synthetic entry for the archive/snapshot root itself
# (path "/") -- not a real file or a meaningful search result, so it's
# never turned into an index row.
_ROOT_PATH = "/"


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1] or "/"


def _parse_iso_mtime(raw: str | None) -> datetime | None:
    """`CatalogEntry.mtime` (CT/host) is an ISO8601 string, confirmed live
    against a real PBS 4.2 instance -- e.g. `2023-02-20T06:23:01Z`.
    """
    if raw is None:
        return None
    return datetime.fromisoformat(raw)


def _epoch_to_datetime(raw: int | None) -> datetime | None:
    """`FileRestoreListEntry.mtime` (VM) is a Unix epoch integer, unlike
    `CatalogEntry.mtime`'s ISO8601 string -- confirmed live against a real
    PVE 9.2 node (see `filerestore/browse.py::_epoch_to_iso8601`, which
    does the equivalent normalization for the on-demand browsing path).
    """
    if raw is None:
        return None
    return datetime.fromtimestamp(raw, tz=UTC)


def catalog_entries_to_rows(
    entries: list[CatalogEntry],
    *,
    file_index_job_id: uuid.UUID,
    backup_snapshot_id: uuid.UUID,
    backup_group_id: uuid.UUID,
    customer_id: uuid.UUID | None,
) -> list[dict]:
    """CT/host path: every entry in one `dump_catalog()` result shares the
    same single archive name (PBS's pxar convention -- see
    `browse.CT_HOST_ARCHIVE_NAME`), unlike the VM path below where each
    entry's archive is derived from its own path.
    """
    rows = []
    for entry in entries:
        if entry.path == _ROOT_PATH:
            continue
        rows.append(
            {
                "file_index_job_id": file_index_job_id,
                "backup_snapshot_id": backup_snapshot_id,
                "backup_group_id": backup_group_id,
                "customer_id": customer_id,
                "archive": CT_HOST_ARCHIVE_NAME,
                "path": entry.path,
                "filename": _basename(entry.path),
                "type": entry.type.value,
                "size_bytes": entry.size,
                "mtime": _parse_iso_mtime(entry.mtime),
                "linked_from_entry_id": None,
            }
        )
    return rows


def file_restore_entries_to_rows(
    entries: list[FileRestoreListEntry],
    *,
    file_index_job_id: uuid.UUID,
    backup_snapshot_id: uuid.UUID,
    backup_group_id: uuid.UUID,
    customer_id: uuid.UUID | None,
) -> list[dict]:
    """VM path: a snapshot can have several disk archives (e.g.
    `drive-scsi0.img.fidx`, `drive-scsi1.img.fidx`), each addressed as the
    first path segment under `/` (confirmed via `file_restore.list_path`'s
    own `_decode_filepath` convention) -- so `archive` is derived
    per-entry from its own path rather than passed in once, unlike the
    CT/host case above.
    """
    rows = []
    for entry in entries:
        if entry.path == _ROOT_PATH:
            continue
        archive = entry.path.strip("/").split("/", 1)[0]
        rows.append(
            {
                "file_index_job_id": file_index_job_id,
                "backup_snapshot_id": backup_snapshot_id,
                "backup_group_id": backup_group_id,
                "customer_id": customer_id,
                "archive": archive,
                "path": entry.path,
                "filename": _basename(entry.path),
                "type": entry.type.value,
                "size_bytes": entry.size,
                "mtime": _epoch_to_datetime(entry.mtime),
                "linked_from_entry_id": None,
            }
        )
    return rows


def _repeats_its_own_parent_segment(path: str) -> bool:
    """True when a path's last two segments are identical (e.g.
    `.../usr/bin/X11/X11`) -- the signature of walking into a
    self-referential symlink whose target is one of its own ancestor
    directories.

    Confirmed live against a real Ubuntu VM that `proxmox-file-restore
    list` resolves a symlink to its *target's* type before this walker
    ever sees it: `/usr/bin/X11` (a classic symlink pointing back into
    its own parent) was reported as `type: 'd'` (plain directory), never
    `'l'` (symlink) -- so filtering by `FileRestoreListEntry.type` alone
    (the first attempt at this fix) never actually triggers for this
    case, and the walker looped `/X11/X11/X11/...` until the per-run
    directory budget cut it off, burning an entire run on one loop
    instead of the rest of the real tree. This catches it after exactly
    one repeat instead. Narrower than "does any ancestor share this
    name" on purpose: two *directly* nested directories with the same
    literal name is a vanishingly rare real pattern (unlike, say, two
    same-named directories at unrelated depths), so the false-positive
    cost of this heuristic stays low.
    """
    segments = path.rstrip("/").split("/")
    return len(segments) >= 2 and segments[-1] == segments[-2]


def browsable_paths(entries: list[FileRestoreListEntry]) -> list[str]:
    """Which of a directory's children the VM-path BFS walker should push
    onto its `pending_paths` frontier -- `leaf=False` covers directories
    *and* the disk-archive/partition-selector level (type `v`), both of
    which are browsable one level deeper, per `FileRestoreListEntry`'s own
    docstring.

    Two loop guards on top of that: `type != SYMLINK` (defense in depth
    for whatever case *does* report `'l'`, unconfirmed either way) and
    `_repeats_its_own_parent_segment` (the actual fix for the confirmed
    live case, where PBS reports a directory-resolving symlink's type as
    plain `'d'`). The symlink itself is still indexed as its own entry;
    its target's real content is reachable via its own actual path
    elsewhere in the tree, so nothing is lost by never walking into it.
    """
    return [
        entry.path
        for entry in entries
        if not entry.leaf
        and entry.type != FileRestoreEntryType.SYMLINK
        and not _repeats_its_own_parent_segment(entry.path)
    ]
