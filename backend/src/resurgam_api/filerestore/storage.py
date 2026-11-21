"""Where file-restore download content lives: files in `Settings.file_restore_output_dir`.

The file-restore worker (native on the appliance host, ADR 0013) writes them; the API container
reads them through a read-only bind mount of the same directory (appliance/docker-compose.yml)
and streams them to the browser. Content never goes through Postgres any more (it did until
migration 0056, capped at 500 MB).

Layout, all named after the job id so a sweep can tell what belongs to which job:
- `<job id>.bin`      completed single-file download
- `<job id>.zip`      completed folder/multi-selection archive
- `<job id>.part`     being written (renamed into place when complete)
- `<job id>.staging/` scratch space while extracting (CT/host restore target, per-entry
                      intermediate files); always removed when the job ends
Staging lives here, not in /tmp, on purpose: the worker unit has `PrivateTmp=true`, /tmp may be
a small tmpfs, and the final rename must stay on one file system.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from resurgam_api.config import Settings

logger = logging.getLogger("resurgam_api.filerestore.storage")

_JOB_FILE = re.compile(r"^(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?P<suffix>\..+)?$")


def output_dir(settings: Settings) -> Path:
    if settings.file_restore_output_dir:
        return Path(settings.file_restore_output_dir)
    return Path(tempfile.gettempdir()) / "resurgam-file-restore"


def ensure_output_dir(settings: Settings) -> Path:
    """Worker side: create the directory if it's missing (dev; on the appliance host-setup.sh
    creates it with the right owner)."""
    directory = output_dir(settings)
    directory.mkdir(mode=0o750, parents=True, exist_ok=True)
    return directory


def storage_name(job_id: uuid.UUID, job_type: str) -> str:
    return f"{job_id}.zip" if job_type == "archive" else f"{job_id}.bin"


def part_path(settings: Settings, job_id: uuid.UUID) -> Path:
    return output_dir(settings) / f"{job_id}.part"


def staging_path(settings: Settings, job_id: uuid.UUID) -> Path:
    return output_dir(settings) / f"{job_id}.staging"


def content_path(settings: Settings, name: str | None) -> Path | None:
    """The stored content file for `name` (a job's `result_storage_name`), or None if there is
    none. Refuses anything that isn't a plain job file name, so a tampered DB value can't point
    the download endpoint outside the output directory."""
    if not name or _JOB_FILE.match(name) is None or "/" in name:
        return None
    path = output_dir(settings) / name
    return path if path.is_file() else None


@dataclass
class DiskSpace:
    free_bytes: int
    total_bytes: int


def disk_space(settings: Settings) -> DiskSpace | None:
    """Free/total bytes of the output directory's file system, None when it doesn't exist (yet).

    Looks at the directory itself or, if missing, its nearest existing parent -- the API
    container sees the bind mount, so this measures the appliance disk the worker writes to."""
    probe = output_dir(settings)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        st = os.statvfs(probe)
    except OSError:
        return None
    return DiskSpace(free_bytes=st.f_bavail * st.f_frsize, total_bytes=st.f_blocks * st.f_frsize)


def remove_job_files(settings: Settings, job_id: uuid.UUID) -> None:
    """Deletes every file/directory belonging to `job_id` (content, part, staging)."""
    directory = output_dir(settings)
    for suffix in (".bin", ".zip", ".part", ".staging"):
        remove_path(directory / f"{job_id}{suffix}")


def remove_content(settings: Settings, name: str | None) -> None:
    if name and _JOB_FILE.match(name) and "/" not in name:
        remove_path(output_dir(settings) / name)


def remove_path(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        logger.warning("file_restore: could not remove %s", path, exc_info=True)


def sweep_orphans(settings: Settings, keep: set[str]) -> int:
    """Deletes job files in the output directory that no live job owns: `keep` holds the
    `result_storage_name`s of completed jobs still within their download window plus
    `<id>.part`/`<id>.staging` of jobs currently running. Catches what a crash left behind.
    Entries that don't look like job files are never touched. Returns how many were removed."""
    directory = output_dir(settings)
    if not directory.is_dir():
        return 0
    removed = 0
    for entry in directory.iterdir():
        if _JOB_FILE.match(entry.name) is None or entry.name in keep:
            continue
        remove_path(entry)
        removed += 1
    return removed
