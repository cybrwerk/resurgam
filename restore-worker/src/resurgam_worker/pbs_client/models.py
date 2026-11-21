from enum import Enum

from pydantic import BaseModel


class FileRestoreEntryType(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    OTHER = "other"


class FileRestoreListEntry(BaseModel):
    """One entry from `proxmox-file-restore list --output-format json`,
    confirmed live (2026-09-15) against a real PVE 9.2 node with a real
    Windows Server VM backup. The Milestone-0 guess this replaced was
    wrong on every field: the real shape is `filepath` (base64-encoded,
    not a plain `path`), `text` (display name), `leaf` (bool — false for
    anything browsable, including the top-level disk archive entry itself
    and partitions, not just directories), `type` as a single character
    (`f`/`d`/`l`/`v` — `v` is the disk-archive/partition-selector level,
    which behaves exactly like a directory from a browsing perspective),
    and `mtime` as a Unix epoch integer, not an ISO8601 string (unlike
    `CatalogEntry.mtime` below, which *is* ISO8601 — the two PBS tools use
    different conventions).

    `path` and `type` here are normalized by `list_path()` before this
    model is constructed: `path` is the base64-decoded `filepath` with a
    leading `/` added if missing (only the root entry's `filepath` already
    has one), and `type` is mapped from the raw single-character code.
    """

    path: str
    name: str
    type: FileRestoreEntryType
    leaf: bool
    size: int | None = None
    mtime: int | None = None


class RestoreSessionStatus(BaseModel):
    """One running restore VM, from `proxmox-file-restore status
    --output-format json`. Confirmed live (2026-09-15): the real shape is
    `{"Qemu": {"ids": {"<session-name>": {"cid", "pid", "timeout",
    "uptime"}}}}` (or `{"Qemu": {}}` with no `ids` key at all when no
    session is running) — completely different from the Milestone-0 guess
    of a flat `[{name, state, pid}, ...]` list. `status()` flattens the
    `ids` map into a list of these, using each key as `name`.
    """

    name: str
    cid: int
    pid: int
    timeout: int
    uptime: int


class CatalogEntryType(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"


class CatalogEntry(BaseModel):
    """One entry from `proxmox-backup-client catalog dump` (CT/host
    snapshots — pxar-based, distinct from `FileRestoreListEntry` above,
    which is `proxmox-file-restore`'s shape for VM disk images).
    Confirmed live against a real PBS 4.2 instance (2026-09-14): the
    `path`/type/size/mtime fields below all matched real output for a
    real container snapshot's full file tree. `path` is root-relative
    (`/etc/hosts`, `/` for the archive root) — `catalog.dump_catalog()`
    strips the `./<archive>.pxar.didx` prefix the raw CLI output carries
    on every line before this model is ever constructed.
    """

    path: str
    type: CatalogEntryType
    size: int | None = None
    mtime: str | None = None


class ExtractResult(BaseModel):
    """Outcome of an extract-to-path operation.

    Constructed by the wrapper from the subprocess result, not parsed from
    CLI JSON output (extract does not emit JSON on success).
    """

    target: str
    success: bool


class MappedImage(BaseModel):
    """Outcome of `proxmox-backup-client map` -- confirmed live
    (2026-09-19) against a real PBS 4.2 instance: a 130MB VM disk
    archive mapped as `/dev/loop0` with zero copy, a real readable MBR
    disk (`fdisk -l` confirmed a real partition table). `map` has no
    `--output-format json` option (unlike `proxmox-file-restore`) --
    its plain-text stdout is parsed with a documented, tested regex,
    same discipline as `catalog.py`'s plain-text parsing.
    """

    device_path: str
