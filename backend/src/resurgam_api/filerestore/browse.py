"""File-level browsing/download of a backup snapshot's contents — not
from the manifest by name, but manifest §13's File Indexing/Recovery
capability. Calls `resurgam_worker.pbs_client` (the restore-worker
component) in-process rather than as a separate service, mirroring
ADR 0006's existing precedent for the Collector: no job queue exists
yet to route this through one properly, and this is simpler to build
and test now.

Dispatches on `BackupGroup.backup_type`:
- VM (qemu disk images): `resurgam_worker.pbs_client.file_restore`,
  wrapping `proxmox-file-restore`. Confirmed live (2026-09-15) against a
  real PVE 9.2 node — a real Windows Server VM backup's disk archive,
  partitions, and full NTFS directory tree all listed and downloaded
  correctly, standalone (no PVE API involved, just the CLI directly, same
  as this module calls it). The key requirement discovered live: this
  binary, `proxmox-backup-restore-image`, and `pve-qemu-kvm` must all come
  from the *same* apt repo release (they're a co-tested set on a real PVE
  host) — independently mixing the separate PBS-only and PVE-only repos,
  or pairing them with the wrong Debian release, reproducibly breaks the
  host-to-guest vsock handshake (see ADR 0011 for the full diagnosis).
- CT/host (pxar-based): `resurgam_worker.pbs_client.catalog`, wrapping
  `proxmox-backup-client catalog dump`/`restore` — confirmed live
  against a real PBS 4.2 instance (2026-09-14).
"""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from resurgam_worker.pbs_client import catalog, file_restore
from resurgam_worker.pbs_client.errors import (
    InvalidPathError,
    PathNotMountableError,
    PbsBinaryNotFoundError,
    PbsClientError,
    PbsCommandFailedError,
    PbsOutputParseError,
)
from resurgam_worker.zip_builder import ZipBuilder, ZipReport, ZipWriteError
from sqlalchemy.orm import Session

from resurgam_api import encryption_keys
from resurgam_api.config import Settings
from resurgam_api.filerestore import capability, storage
from resurgam_api.filerestore.sessions import stop_stale_session_if_needed
from resurgam_api.models.datastore import Datastore
from resurgam_api.models.inventory import BackupGroup, BackupSnapshot, BackupType
from resurgam_api.models.pbs import PbsInstance, TlsTrustMode
from resurgam_api.models.secrets import SecretReference
from resurgam_api.schemas.filerestore import FileEntryOut
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

# The standard content archive name PBS uses for a CT/host (pxar-based)
# backup — not user-configurable in normal usage, so not worth threading
# through as a parameter yet.
CT_HOST_ARCHIVE_NAME = "root.pxar"


class FileRestoreError(Exception):
    """An HTTP-appropriate wrapper around a `pbs_client` error — the API
    layer raises `ResurgamApiError` from this without needing to know
    `pbs_client`'s own exception hierarchy.
    """

    def __init__(self, code: str, message: str, status_code: int) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _wrap_pbs_error(exc: PbsClientError) -> FileRestoreError:
    if isinstance(exc, PbsBinaryNotFoundError):
        # Raised in the file-restore/file-index worker, i.e. on the host that runs it (the
        # appliance VM itself, not the API container) -- say so, and how to get the tools.
        return FileRestoreError(
            "file_restore_unavailable",
            f"{exc.binary_name} is not installed on the host running the file-restore worker. "
            f"{capability.APPLIANCE_PACKAGE_FIX}",
            status_code=503,
        )
    if isinstance(exc, InvalidPathError):
        return FileRestoreError("invalid_path", str(exc), status_code=400)
    if isinstance(exc, PathNotMountableError):
        return FileRestoreError("path_not_mountable", str(exc), status_code=422)
    if isinstance(exc, (PbsCommandFailedError, PbsOutputParseError)):
        return FileRestoreError("file_restore_failed", str(exc), status_code=502)
    return FileRestoreError("file_restore_failed", str(exc), status_code=502)


def _epoch_to_iso8601(epoch_seconds: int | None) -> str | None:
    """`file_restore.list_path()` returns `mtime` as a Unix epoch integer
    (confirmed live 2026-09-15), unlike `catalog.dump_catalog()`'s ISO8601
    string (CT/host) — normalized here so `FileEntryOut.mtime` is a single
    consistent shape regardless of which backend answered.
    """
    if epoch_seconds is None:
        return None
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def snapshot_identifier(group: BackupGroup, snapshot: BackupSnapshot) -> str:
    """`<backup-type>/<backup-id>/<ISO8601 backup-time>` — the snapshot
    identifier PBS's own CLI tools expect, confirmed live this session
    (e.g. `ct/100/2023-09-20T00:00:03Z`).

    `snapshot.snapshot_time` is stored UTC-normalized (`collector.sync`
    constructs it via `datetime.fromtimestamp(..., tz=timezone.utc)`),
    but Postgres converts a `timestamptz` column to the *session's*
    timezone on read — on a server whose timezone isn't UTC (confirmed
    live, ADR 0016: a freshly `apt`-installed PostgreSQL inherits the
    host's local timezone, unlike the project's own docker-compose
    Postgres image, which defaults to UTC), the datetime object handed
    back carries a non-UTC offset with the wall-clock numbers already
    shifted. `.astimezone(timezone.utc)` normalizes before formatting —
    without it, `strftime(...Z)` silently mislabels a local wall-clock
    time as UTC, producing a snapshot identifier PBS reports as
    nonexistent (confirmed live: a real CT snapshot's real 02:00:04+02:00
    reading formatted as `...T02:00:04Z`, two hours off the real
    `...T00:00:04Z` snapshot).
    """
    utc_time = snapshot.snapshot_time.astimezone(UTC)
    return f"{group.backup_type}/{group.backup_id}/{utc_time.strftime('%Y-%m-%dT%H:%M:%SZ')}"


def pbs_fingerprint(db: Session, datastore: Datastore) -> str | None:
    """The PBS certificate fingerprint pinned for this datastore's instance (`fingerprint` TLS trust
    mode), for the CLI tools' `PBS_FINGERPRINT`; None for a CA-trusted instance."""
    instance = db.get(PbsInstance, datastore.pbs_instance_id)
    if instance is None or instance.tls_trust_mode != TlsTrustMode.FINGERPRINT.value:
        return None
    return instance.tls_fingerprint_sha256 or None


def repository_token_and_keyfile(
    db: Session, settings: Settings, datastore: Datastore, snapshot: BackupSnapshot | None = None
) -> tuple[str, str, str | None]:
    """`<token-id>@<host>:<port>:<datastore>` — the PBS CLI `--repository`
    shape (confirmed live this session), plus the token *secret*
    separately for the `PBS_PASSWORD` env var. The stored secret is the
    single colon-joined `token-id:secret` string RESURGAM asks operators
    for everywhere else (matches the `Authorization: PBSAPIToken=...`
    REST header shape) — split here since the CLI wants the two parts in
    different places, not because the stored shape itself is wrong.

    Also resolves the encryption key `snapshot` needs (ADR 0031): exactly the
    one its fingerprint names, None for an unencrypted backup or when no
    snapshot is given. Raises `FileRestoreError` (409, `encryption_key_missing`)
    when the backup is encrypted with a key RESURGAM does not hold.
    """
    instance = db.get(PbsInstance, datastore.pbs_instance_id)
    secret_ref = db.get(SecretReference, instance.api_token_secret_ref_id)
    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    full_token = provider.retrieve(secret_ref.reference_key)
    token_id, _, token_secret = full_token.partition(":")
    repository = f"{token_id}@{instance.host}:{instance.port}:{datastore.name}"

    keyfile: str | None = None
    if snapshot is not None:
        try:
            keyfile = encryption_keys.keyfile_for_snapshot(db, settings, snapshot)
        except encryption_keys.EncryptionKeyMissingError as exc:
            raise FileRestoreError("encryption_key_missing", str(exc), 409) from exc

    return repository, token_secret, keyfile


def list_directory(
    db: Session, settings: Settings, group: BackupGroup, snapshot: BackupSnapshot, datastore: Datastore, path: str
) -> list[FileEntryOut]:
    repository, token, keyfile = repository_token_and_keyfile(db, settings, datastore, snapshot)
    snapshot_id = snapshot_identifier(group, snapshot)

    try:
        if group.backup_type == BackupType.VM.value:
            stop_stale_session_if_needed(
                snapshot_id, token=token, max_age_seconds=settings.file_restore_max_session_duration_seconds
            )
            entries = file_restore.list_path(
                repository,
                snapshot_id,
                path,
                namespace=group.namespace,
                token=token,
                keyfile=keyfile,
                fingerprint=pbs_fingerprint(db, datastore),
            )
            return [
                FileEntryOut(path=e.path, name=e.name, type=e.type.value, size=e.size, mtime=_epoch_to_iso8601(e.mtime))
                for e in entries
            ]

        all_entries = catalog.dump_catalog(
            repository,
            snapshot_id,
            namespace=group.namespace,
            token=token,
            keyfile=keyfile,
            fingerprint=pbs_fingerprint(db, datastore),
        )
        children = catalog.list_directory(all_entries, path)
        return [
            FileEntryOut(
                path=e.path, name=e.path.rsplit("/", 1)[-1] or "/", type=e.type.value, size=e.size, mtime=e.mtime
            )
            for e in children
        ]
    except PbsClientError as exc:
        raise _wrap_pbs_error(exc) from exc


@dataclass
class DownloadResult:
    """A completed download/archive job's stored content (see `filerestore/storage.py`)."""

    filename: str
    size_bytes: int
    storage_name: str
    report: dict | None = None


ProgressFn = Callable[[dict], None]


def _format_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{n} B"


def _too_large(size: int, settings: Settings, *, so_far: bool = False) -> FileRestoreError:
    amount = f"at least {_format_bytes(size)} written so far" if so_far else _format_bytes(size)
    return FileRestoreError(
        "content_too_large",
        f"The content ({amount}) exceeds the configured limit of "
        f"{_format_bytes(settings.file_restore_max_content_bytes)} per download "
        "(FILE_RESTORE_MAX_CONTENT_BYTES). Select fewer or smaller entries, or raise the limit.",
        status_code=413,
    )


def require_free_space(settings: Settings, needed_bytes: int = 0) -> None:
    """Refuses to start (or continue) when the output directory's file system would drop below
    `Settings.file_restore_min_free_bytes` -- checked by the API when a job is created and by the
    worker before and during extraction. No-op when the directory can't be measured."""
    space = storage.disk_space(settings)
    if space is None:
        return
    required = settings.file_restore_min_free_bytes + needed_bytes
    if space.free_bytes < required:
        raise FileRestoreError(
            "insufficient_disk_space",
            f"Not enough free disk space for file-restore downloads: {_format_bytes(space.free_bytes)} free in "
            f"{storage.output_dir(settings)}, {_format_bytes(required)} needed (estimated content plus the "
            f"{_format_bytes(settings.file_restore_min_free_bytes)} reserve, FILE_RESTORE_MIN_FREE_BYTES). "
            "Wait for earlier downloads to expire, or enlarge the appliance disk.",
            status_code=507,
        )


def _deadline_exceeded(settings: Settings) -> FileRestoreError:
    return FileRestoreError(
        "deadline_exceeded",
        f"The download took longer than the allowed {settings.file_restore_extract_timeout_seconds // 60} minutes "
        "in total (FILE_RESTORE_EXTRACT_TIMEOUT_SECONDS) and was stopped. Select fewer or smaller entries.",
        status_code=504,
    )


class _DiskGuard:
    """Called by `run_guarded` every couple of seconds while a PBS tool writes, and by the ZIP
    builder before every member. The tools' output size isn't known up front (a VM directory),
    so the guard watches the *drop in free space* since the job started: that is what the job
    has written so far, at the cost of one statvfs call.

    Also the job's stop switch: past `deadline` (a `time.monotonic()` value -- the whole job's
    time budget, not one tool call's) it raises `deadline_exceeded`, and `abort_check()` (the
    worker's "was this job cancelled?" probe) may raise to stop the job."""

    def __init__(
        self,
        settings: Settings,
        *,
        deadline: float | None = None,
        abort_check: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.deadline = deadline
        self.abort_check = abort_check
        space = storage.disk_space(settings)
        self.start_free = space.free_bytes if space else None

    def __call__(self, report: ZipReport | None = None) -> None:
        if self.abort_check is not None:
            self.abort_check()
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise _deadline_exceeded(self.settings)
        if report is not None:
            # `pending_bytes`: the member about to be written -- refuse it before it crosses the limit.
            upcoming = report.total_bytes + report.pending_bytes
            if upcoming > self.settings.file_restore_max_content_bytes:
                raise _too_large(upcoming, self.settings)
        space = storage.disk_space(self.settings)
        if space is None or self.start_free is None:
            return
        written = self.start_free - space.free_bytes
        # Staged data + the growing ZIP can briefly reach about twice the content size.
        if written > 2 * self.settings.file_restore_max_content_bytes:
            raise _too_large(written, self.settings, so_far=True)
        require_free_space(self.settings)


def _finish(part: Path, final: Path) -> int:
    size = part.stat().st_size
    os.replace(part, final)
    return size


def download_file(
    db: Session,
    settings: Settings,
    group: BackupGroup,
    snapshot: BackupSnapshot,
    datastore: Datastore,
    path: str,
    *,
    job_id,
    deadline: float | None = None,
    abort_check: Callable[[], None] | None = None,
) -> DownloadResult:
    """A single file, streamed to `<output dir>/<job id>.bin` -- never held in memory or stored
    in Postgres (it was until migration 0056). `deadline`/`abort_check`: see `_DiskGuard`."""
    repository, token, keyfile = repository_token_and_keyfile(db, settings, datastore, snapshot)
    snapshot_id = snapshot_identifier(group, snapshot)
    filename = path.rsplit("/", 1)[-1] or "download"
    directory = storage.ensure_output_dir(settings)
    part = storage.part_path(settings, job_id)
    final = directory / storage.storage_name(job_id, "download")
    require_free_space(settings)
    guard = _DiskGuard(settings, deadline=deadline, abort_check=abort_check)

    try:
        if group.backup_type == BackupType.VM.value:
            stop_stale_session_if_needed(
                snapshot_id, token=token, max_age_seconds=settings.file_restore_max_session_duration_seconds
            )
            file_restore.extract_to_file(
                repository,
                snapshot_id,
                path,
                str(part),
                namespace=group.namespace,
                token=token,
                keyfile=keyfile,
                fingerprint=pbs_fingerprint(db, datastore),
                timeout=settings.file_restore_extract_timeout_seconds,
                guard=guard,
            )
        else:
            catalog.extract_file_to_path(
                repository,
                snapshot_id,
                CT_HOST_ARCHIVE_NAME,
                path,
                str(part),
                staging_dir=str(storage.staging_path(settings, job_id)),
                namespace=group.namespace,
                token=token,
                keyfile=keyfile,
                fingerprint=pbs_fingerprint(db, datastore),
                timeout=settings.file_restore_extract_timeout_seconds,
                guard=guard,
            )
    except PbsClientError as exc:
        raise _wrap_pbs_error(exc) from exc

    size = part.stat().st_size
    if size > settings.file_restore_max_content_bytes:
        part.unlink(missing_ok=True)
        raise _too_large(size, settings)
    _finish(part, final)
    return DownloadResult(filename=filename, size_bytes=size, storage_name=final.name)


# `FileRestoreJob.result_filename` is String(255) -- and 255 is also what most file systems
# allow for the name the browser saves.
_MAX_FILENAME = 255
_ZIP_SUFFIX = ".zip"


def _shorten(name: str, limit: int) -> str:
    """`name` cut to `limit` characters, keeping a short extension (`report.docx` stays a .docx)."""
    if len(name) <= limit:
        return name
    stem, dot, ext = name.rpartition(".")
    if dot and stem and 0 < len(ext) <= 16:
        return stem[: limit - len(ext) - 1] + "." + ext
    return name[:limit]


def archive_filename(group: BackupGroup, snapshot: BackupSnapshot, paths: list[str]) -> str:
    """One selected entry: `<its name>.zip`. Several: `<type>-<id>-<snapshot time>.zip`. Never
    longer than 255 characters (a 252+ character entry name is shortened, extension kept)."""
    limit = _MAX_FILENAME - len(_ZIP_SUFFIX)
    if len(paths) == 1:
        name = paths[0].rstrip("/").rsplit("/", 1)[-1]
        if name:
            return _shorten(name, limit) + _ZIP_SUFFIX
    stamp = snapshot.snapshot_time.astimezone(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    return _shorten(f"{group.backup_type}-{group.backup_id}-{stamp}", limit) + _ZIP_SUFFIX


def _relative_parts(path: str, base: str) -> list[str]:
    return list(PurePosixPath(path).relative_to(PurePosixPath(base)).parts)


def build_archive(
    db: Session,
    settings: Settings,
    group: BackupGroup,
    snapshot: BackupSnapshot,
    datastore: Datastore,
    paths: list[str],
    base: str,
    *,
    job_id,
    on_progress: ProgressFn | None = None,
    deadline: float | None = None,
    abort_check: Callable[[], None] | None = None,
) -> DownloadResult:
    """Folder / multi-selection download: every entry of `paths` (files and/or directories of
    one snapshot) in ONE ZIP at `<output dir>/<job id>.zip`, names relative to `base` (their
    common parent). An entry that can't be extracted is skipped and listed in the report, not
    fatal -- the job only fails when nothing at all could be extracted, or a limit is hit.

    VM: each entry separately via `proxmox-file-restore extract ... -` -- a directory with
    `--format zip` (built inside the restore micro-VM, then merged), a file plain -- into the
    staging directory; see `file_restore.extract_to_file` for why not a local target dir.
    CT/host: one `proxmox-backup-client restore` with a `--pattern` per entry into the staging
    directory (`catalog.restore_selection`), then zipped from there.

    `deadline` caps the whole archive (each tool call also has its own timeout), `abort_check`
    lets the worker stop it on cancel -- both checked by the guard every couple of seconds and
    before every ZIP member. A failed write to the ZIP itself (disk full) fails the job with
    `archive_write_failed` and deletes the partial file.
    """
    repository, token, keyfile = repository_token_and_keyfile(db, settings, datastore, snapshot)
    snapshot_id = snapshot_identifier(group, snapshot)
    fingerprint = pbs_fingerprint(db, datastore)
    storage.ensure_output_dir(settings)
    staging = storage.staging_path(settings, job_id)
    staging.mkdir(mode=0o700)
    part = storage.part_path(settings, job_id)
    final = storage.output_dir(settings) / storage.storage_name(job_id, "archive")
    require_free_space(settings)
    guard = _DiskGuard(settings, deadline=deadline, abort_check=abort_check)
    total = len(paths)

    state = {"phase": "extracting", "entries_done": 0}

    def emit(report: ZipReport) -> None:
        if on_progress is not None:
            on_progress({**state, "entries_total": total, "bytes_done": report.total_bytes})

    def progress(phase: str, done: int, zip_builder: ZipBuilder) -> None:
        state.update(phase=phase, entries_done=done)
        emit(zip_builder.report)

    builder = ZipBuilder(part, check=guard, on_progress=emit)
    try:
        with builder:
            run = _archive_vm if group.backup_type == BackupType.VM.value else _archive_ct
            run(
                settings,
                group,
                snapshot_id,
                paths,
                base,
                staging,
                builder,
                guard,
                progress,
                repository=repository,
                token=token,
                keyfile=keyfile,
                fingerprint=fingerprint,
            )
    except PbsClientError as exc:
        part.unlink(missing_ok=True)
        raise _wrap_pbs_error(exc) from exc
    except ZipWriteError as exc:
        part.unlink(missing_ok=True)
        raise FileRestoreError(
            "archive_write_failed",
            f"The ZIP could not be written to {storage.output_dir(settings)} ({exc.cause.strerror or exc.cause}); "
            "the partial archive was deleted. Free up disk space (or wait for earlier downloads to expire) and "
            "try again.",
            status_code=507,
        ) from exc
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        storage.remove_path(staging)

    report = builder.report
    if report.file_count + report.directory_count + report.symlink_count == 0:
        part.unlink(missing_ok=True)
        reasons = "; ".join(f"{s.path}: {s.reason}" for s in report.skipped[:5])
        raise FileRestoreError(
            "nothing_extracted",
            f"None of the {total} selected entries could be extracted. {reasons}",
            status_code=422,
        )
    size = _finish(part, final)
    return DownloadResult(
        filename=archive_filename(group, snapshot, paths),
        size_bytes=size,
        storage_name=final.name,
        report=report.as_dict(),
    )


def _archive_vm(
    settings,
    group,
    snapshot_id,
    paths,
    base,
    staging,
    builder,
    guard,
    progress,
    *,
    repository,
    token,
    keyfile,
    fingerprint,
) -> None:
    stop_stale_session_if_needed(
        snapshot_id, token=token, max_age_seconds=settings.file_restore_max_session_duration_seconds
    )
    # The entry types decide how each one is extracted (plain file vs. ZIP of a directory) --
    # one listing per distinct parent, cheap once the restore VM is up (ADR 0011: ~1s warm).
    by_path: dict[str, object] = {}
    failed_parents: dict[str, str] = {}
    for parent in sorted({str(PurePosixPath(p).parent) for p in paths}):
        try:
            for entry in file_restore.list_path(
                repository,
                snapshot_id,
                parent,
                namespace=group.namespace,
                token=token,
                keyfile=keyfile,
                fingerprint=fingerprint,
            ):
                by_path[entry.path] = entry
        except PbsClientError as exc:
            failed_parents[parent] = str(exc)

    known_bytes = sum(e.size or 0 for e in by_path.values() if e.path in paths and e.type.value == "file")
    if known_bytes > settings.file_restore_max_content_bytes:
        raise _too_large(known_bytes, settings)
    require_free_space(settings, 2 * known_bytes)

    for index, path in enumerate(paths):
        progress("extracting", index, builder)
        parent = str(PurePosixPath(path).parent)
        entry = by_path.get(path)
        if entry is None:
            reason = failed_parents.get(parent) or "not found in the snapshot"
            builder.skip(path, f"could not be listed: {reason}" if parent in failed_parents else reason)
            continue
        parts = _relative_parts(path, base)
        try:
            if entry.type.value == "directory":
                tmp = staging / f"{index}.zip"
                file_restore.extract_to_file(
                    repository,
                    snapshot_id,
                    path,
                    str(tmp),
                    as_zip=True,
                    namespace=group.namespace,
                    token=token,
                    keyfile=keyfile,
                    fingerprint=fingerprint,
                    timeout=settings.file_restore_extract_timeout_seconds,
                    guard=guard,
                )
                builder.add_zip(tmp, parts[:-1], parent)
                tmp.unlink(missing_ok=True)
            else:
                tmp = staging / f"{index}.bin"
                file_restore.extract_to_file(
                    repository,
                    snapshot_id,
                    path,
                    str(tmp),
                    namespace=group.namespace,
                    token=token,
                    keyfile=keyfile,
                    fingerprint=fingerprint,
                    timeout=settings.file_restore_extract_timeout_seconds,
                    guard=guard,
                )
                builder.add_tree(tmp, parts, path)
        except (PbsCommandFailedError, PathNotMountableError, PbsOutputParseError, InvalidPathError) as exc:
            builder.skip(path, str(exc))
    progress("packing", len(paths), builder)


def _archive_ct(
    settings,
    group,
    snapshot_id,
    paths,
    base,
    staging,
    builder,
    guard,
    progress,
    *,
    repository,
    token,
    keyfile,
    fingerprint,
) -> None:
    # `catalog dump` is metadata only and fast (0.55s for 63k entries live, ADR 0011) -- it tells
    # us which selected entries exist and their exact total size before the slow restore.
    entries = catalog.dump_catalog(
        repository, snapshot_id, namespace=group.namespace, token=token, keyfile=keyfile, fingerprint=fingerprint
    )
    known = {e.path for e in entries}
    present = [p for p in paths if p in known]
    for path in paths:
        if path not in known:
            # The catalog parser only knows files, directories and symlinks (`catalog._CATALOG_LINE`),
            # so a FIFO/device/socket lands here too -- none of which a ZIP could hold anyway.
            builder.skip(path, "not found in the snapshot (or a special file such as a FIFO or device)")
    if not present:
        return
    selected = set(present)

    def is_selected(path: str) -> bool:
        return any(str(candidate) in selected for candidate in (PurePosixPath(path), *PurePosixPath(path).parents))

    estimate = sum(e.size or 0 for e in entries if e.type.value == "file" and is_selected(e.path))
    if estimate > settings.file_restore_max_content_bytes:
        raise _too_large(estimate, settings)
    # Staged copy + ZIP (staged files are deleted as they go in, so this is the worst case).
    require_free_space(settings, 2 * estimate)

    progress("extracting", 0, builder)
    target = staging / "root"
    target.mkdir()
    failed = catalog.restore_selection(
        repository,
        snapshot_id,
        CT_HOST_ARCHIVE_NAME,
        str(target),
        present,
        namespace=group.namespace,
        token=token,
        keyfile=keyfile,
        fingerprint=fingerprint,
        timeout=settings.file_restore_extract_timeout_seconds,
        guard=guard,
    )
    # `restore --pattern` never creates a selected directory that has no entries below it
    # (found against proxmox-backup-client 4.2.8 in a local test, 2026-10-08) -- it is still part of
    # the selection, so it goes into the ZIP as the empty directory it is.
    directories = {e.path for e in entries if e.type.value == "directory"}
    non_empty = {str(parent) for e in entries for parent in PurePosixPath(e.path).parents}
    for index, path in enumerate(present):
        progress("packing", index, builder)
        if path in failed:
            builder.skip(path, failed[path])
            continue
        local = target / path.lstrip("/")
        if path in directories and path not in non_empty and not local.exists():
            local.mkdir(parents=True)
        builder.add_tree(local, _relative_parts(path, base), path)
    progress("packing", len(paths), builder)
