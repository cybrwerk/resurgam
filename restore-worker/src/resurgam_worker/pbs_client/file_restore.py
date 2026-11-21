from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import time

from pydantic import ValidationError

from resurgam_worker.pbs_client._subprocess import LONG_COMMAND_TIMEOUT_SECONDS, Guard, materialized_keyfile
from resurgam_worker.pbs_client._subprocess import run as _run
from resurgam_worker.pbs_client._subprocess import run_guarded as _run_guarded
from resurgam_worker.pbs_client._subprocess import run_json as _run_json
from resurgam_worker.pbs_client.errors import (
    InvalidPathError,
    PathNotMountableError,
    PbsBinaryNotFoundError,
    PbsCommandFailedError,
    PbsOutputParseError,
)
from resurgam_worker.pbs_client.models import (
    ExtractResult,
    FileRestoreEntryType,
    FileRestoreListEntry,
    RestoreSessionStatus,
)

_DEFAULT_BINARY = "proxmox-file-restore"

# Seen live (2026-11-13, appliance, Windows VM index): `list` answered a
# perfectly mountable directory with `{"code": ..., "message": "Unable to
# acquire lock \"/var/log/proxmox-backup/tasks/.active.lock\" - Interrupted
# system call (os error 4)"}` -- same shape as a genuinely unmountable
# partition, so it used to be skipped and the directory silently left out
# of the index. These messages are transient; retry before giving up.
_TRANSIENT_MESSAGE_MARKERS = ("os error 4", "interrupted system call", "unable to acquire lock")
_TRANSIENT_RETRY_DELAYS_SECONDS = (0.5, 2.0, 5.0)


def _is_transient_message(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _TRANSIENT_MESSAGE_MARKERS)


_ENTRY_TYPE_BY_CHAR = {
    "f": FileRestoreEntryType.FILE,
    "d": FileRestoreEntryType.DIRECTORY,
    "l": FileRestoreEntryType.SYMLINK,
    # 'v' is the disk-archive/partition-selector level (e.g. the top-level
    # "drive-scsi0.img.fidx" entry, or a partition under it) — not a
    # filesystem directory, but browsable exactly like one.
    "v": FileRestoreEntryType.DIRECTORY,
}


def _resolve_binary(binary: str) -> str:
    binary_path = shutil.which(binary)
    if binary_path is None:
        raise PbsBinaryNotFoundError(binary)
    return binary_path


def _normalize_archive_path(path: str) -> str:
    """Validate a path *within* a PBS snapshot archive against traversal (manifest §14).

    This is not a local filesystem path — it addresses content inside the
    archive — but it is still attacker-controllable input, so it must never
    contain ".." components.
    """
    if not path:
        raise InvalidPathError(path, "path must not be empty")
    if not path.startswith("/"):
        raise InvalidPathError(path, "archive path must be absolute (start with '/')")
    if ".." in path.split("/"):
        raise InvalidPathError(path, "path must not contain '..' components")
    return path


def _normalize_local_target(target: str) -> str:
    """Validate a local filesystem destination for extract() against traversal."""
    if not target:
        raise InvalidPathError(target, "target must not be empty")
    if target == "-":
        raise InvalidPathError(target, "use extract_to_bytes() for stdout extraction")
    if not target.startswith("/"):
        raise InvalidPathError(target, "target must be an absolute path")
    if ".." in target.split("/"):
        raise InvalidPathError(target, "target must not contain '..' components")
    return target


def _decode_filepath(raw_filepath: str) -> str:
    """`list`'s JSON output carries each entry's path as base64 in a
    `filepath` field — confirmed live (2026-09-15). Only the root entry's
    decoded value already has a leading `/`; every nested entry decodes to
    a bare relative-looking path (e.g. `drive-scsi0.img.fidx/part/2`), so
    one is added here for a consistent absolute-path convention matching
    `catalog.CatalogEntry` (CT/host).
    """
    decoded = base64.b64decode(raw_filepath).decode()
    return decoded if decoded.startswith("/") else f"/{decoded}"


def list_path(
    repository: str,
    snapshot: str,
    path: str,
    *,
    namespace: str = "",
    token: str | None = None,
    keyfile: str | None = None,
    fingerprint: str | None = None,
    binary: str = _DEFAULT_BINARY,
) -> list[FileRestoreListEntry]:
    """List directory contents at `path` inside `snapshot` via `proxmox-file-restore list`.

    Confirmed live (2026-09-15) against a real PVE 9.2 node: a real Windows
    Server VM backup's disk archive, partitions, and NTFS directory tree
    all listed correctly and quickly (~4s including a cold VM boot).

    `keyfile` -- see `map.map_snapshot`'s docstring.
    """
    binary_path = _resolve_binary(binary)
    archive_path = _normalize_archive_path(path)

    command = [
        binary_path,
        "list",
        snapshot,
        archive_path,
        "--repository",
        repository,
        "--output-format",
        "json",
    ]
    if namespace:
        command += ["--ns", namespace]

    with materialized_keyfile(keyfile) as keyfile_path:
        if keyfile_path is not None:
            command += ["--keyfile", keyfile_path]
        raw = _run_json(command, token=token, fingerprint=fingerprint)
        for delay in _TRANSIENT_RETRY_DELAYS_SECONDS:
            if not (isinstance(raw, dict) and _is_transient_message(str(raw.get("message", "")))):
                break
            time.sleep(delay)
            raw = _run_json(command, token=token, fingerprint=fingerprint)
    if isinstance(raw, dict) and "code" in raw and "message" in raw:
        raise PathNotMountableError(path, str(raw["message"]))
    # The tool also reports other failures as a bare `{"message": ...}` with exit code 0 -- e.g.
    # "Permission denied (os error 13)" (the "needs root" of ADR 0011) or a TLS/fingerprint error.
    # Surface the message instead of failing to parse it as a listing.
    if isinstance(raw, dict) and "message" in raw:
        raise PbsCommandFailedError(command, 0, str(raw["message"]))
    try:
        entries = []
        for entry in raw:
            entries.append(
                FileRestoreListEntry(
                    path=_decode_filepath(entry["filepath"]),
                    name=entry["text"],
                    type=_ENTRY_TYPE_BY_CHAR.get(entry["type"], FileRestoreEntryType.OTHER),
                    leaf=entry["leaf"],
                    size=entry.get("size"),
                    mtime=entry.get("mtime"),
                )
            )
        return entries
    except (KeyError, TypeError, ValidationError) as exc:
        raise PbsOutputParseError(command, json.dumps(raw), exc) from exc


def extract(
    repository: str,
    snapshot: str,
    path: str,
    target: str,
    *,
    namespace: str = "",
    token: str | None = None,
    keyfile: str | None = None,
    fingerprint: str | None = None,
    binary: str = _DEFAULT_BINARY,
) -> ExtractResult:
    """Extract a file or directory from `snapshot` at `path` to a local
    `target` path. `keyfile` -- see `map.map_snapshot`'s docstring.
    """
    binary_path = _resolve_binary(binary)
    archive_path = _normalize_archive_path(path)
    local_target = _normalize_local_target(target)

    command = [
        binary_path,
        "extract",
        snapshot,
        archive_path,
        local_target,
        "--repository",
        repository,
    ]
    if namespace:
        command += ["--ns", namespace]

    with materialized_keyfile(keyfile) as keyfile_path:
        if keyfile_path is not None:
            command += ["--keyfile", keyfile_path]
        result = _run(command, token=token, fingerprint=fingerprint)
    if result.returncode != 0:
        raise PbsCommandFailedError(command, result.returncode, result.stderr)
    return ExtractResult(target=local_target, success=True)


def extract_to_bytes(
    repository: str,
    snapshot: str,
    path: str,
    *,
    namespace: str = "",
    token: str | None = None,
    keyfile: str | None = None,
    fingerprint: str | None = None,
    binary: str = _DEFAULT_BINARY,
) -> bytes:
    """Extract a single file from `snapshot` at `path` and return its raw bytes.

    Uses `proxmox-file-restore extract ... -` (stdout streaming target).
    Confirmed live (2026-09-15) against a real Windows Server VM backup —
    completed in well under a second for a small file. Loads the whole
    file into memory: acceptable for now, not suitable for very large
    files — a real streaming implementation belongs in the Restore
    Worker's session handling in a later milestone.

    `keyfile` -- see `map.map_snapshot`'s docstring.
    """
    binary_path = _resolve_binary(binary)
    archive_path = _normalize_archive_path(path)

    command = [
        binary_path,
        "extract",
        snapshot,
        archive_path,
        "-",
        "--repository",
        repository,
    ]
    if namespace:
        command += ["--ns", namespace]

    with materialized_keyfile(keyfile) as keyfile_path:
        if keyfile_path is not None:
            command += ["--keyfile", keyfile_path]
        result = _run(command, capture_text=False, token=token, fingerprint=fingerprint)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace") if isinstance(result.stderr, bytes) else result.stderr
        raise PbsCommandFailedError(command, result.returncode, stderr)
    return result.stdout


def extract_to_file(
    repository: str,
    snapshot: str,
    path: str,
    out_path: str,
    *,
    as_zip: bool = False,
    namespace: str = "",
    token: str | None = None,
    keyfile: str | None = None,
    fingerprint: str | None = None,
    timeout: float = LONG_COMMAND_TIMEOUT_SECONDS,
    guard: Guard | None = None,
    binary: str = _DEFAULT_BINARY,
) -> None:
    """Streams one entry of a VM snapshot into the local file `out_path`, never through memory.

    `extract <snapshot> <path> - ...` writes to stdout, which `_run_guarded` points straight at
    `out_path` -- for a single file (`as_zip=False`) this is exactly the command `extract_to_bytes`
    has run since 2026-09-15 (live-verified), just not captured.

    `as_zip=True` adds `--format zip`, for a directory. Vendor source (proxmox-backup.git,
    checked 2026-10-08): `proxmox-file-restore/src/main.rs::extract` with target `-` asks the
    restore daemon for the requested `FileRestoreFormat` (`pbs-api-types/src/file_restore.rs`:
    `plain | pxar | zip | tar`, kebab-case; `plain` "only works for single files") and copies the
    stream to stdout; the daemon (`proxmox-restore-daemon/src/proxmox_restore_daemon/api.rs::
    extract`) builds it with `proxmox-compression`'s `zip_directory`, which names entries relative
    to the directory's *parent* (so they start with the directory's own name), stores symlinks as
    their target (not followed), sets the UTF-8 (EFS) flag, switches to ZIP64 past 4 GiB, and
    skips a file it can't read with only a log line inside the micro-VM -- invisible here.

    Why not a local target directory instead: with a path target the tool decodes a pxar stream
    on the host (`extract_sub_dir_seq`), applying ownership via chown, and pbs-client's
    `pxar/metadata.rs` only tolerates EOPNOTSUPP, not the EPERM an unprivileged worker gets
    (ADR 0013 addendum: the workers run without root) -- live-confirmed: `extract <path>
    <target-dir>` fails unprivileged with "failed to set ownership" (backend/packaging/systemd/
    README.md). The ZIP is built inside the micro-VM as root and needs no host-side metadata.

    `guard` -- see `_subprocess.run_guarded`. A failed call removes the partial `out_path`.
    """
    binary_path = _resolve_binary(binary)
    archive_path = _normalize_archive_path(path)
    local_target = _normalize_local_target(out_path)

    command = [binary_path, "extract", snapshot, archive_path, "-", "--repository", repository]
    if as_zip:
        command += ["--format", "zip"]
    if namespace:
        command += ["--ns", namespace]

    try:
        with materialized_keyfile(keyfile) as keyfile_path:
            if keyfile_path is not None:
                command += ["--keyfile", keyfile_path]
            result = _run_guarded(
                command, token=token, fingerprint=fingerprint, timeout=timeout, stdout_path=local_target, guard=guard
            )
        if result.returncode != 0:
            raise PbsCommandFailedError(command, result.returncode, result.stderr)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(local_target)
        raise


def status(*, token: str | None = None, binary: str = _DEFAULT_BINARY) -> list[RestoreSessionStatus]:
    """List active restore VM sessions via `proxmox-file-restore status`.

    Confirmed live (2026-09-15): the real shape is `{"Qemu": {"ids":
    {"<session-name>": {"cid", "pid", "timeout", "uptime"}}}}`, or
    `{"Qemu": {}}` with no `ids` key at all when nothing is running —
    flattened here into a list keyed by session name.
    """
    binary_path = _resolve_binary(binary)
    command = [binary_path, "status", "--output-format", "json"]
    raw = _run_json(command, token=token)
    try:
        ids = raw["Qemu"].get("ids", {})
        return [RestoreSessionStatus(name=name, **info) for name, info in ids.items()]
    except (KeyError, TypeError, ValidationError) as exc:
        raise PbsOutputParseError(command, json.dumps(raw), exc) from exc


def stop(name: str, *, token: str | None = None, binary: str = _DEFAULT_BINARY) -> None:
    """Stop an active restore session by name via `proxmox-file-restore stop`.

    `name` is the full session name as returned by `status()` (e.g.
    `qemu_tenant-a@pbs@pbs1.example.internal:8007:backup-store:tenant-a/vm/116/2026-09-14T00:48:57Z`),
    confirmed live (2026-09-15) — cleanly stopped a real running restore VM.
    """
    if not name:
        raise InvalidPathError(name, "session name must not be empty")

    binary_path = _resolve_binary(binary)
    command = [binary_path, "stop", name]
    result = _run(command, token=token)
    if result.returncode != 0:
        raise PbsCommandFailedError(command, result.returncode, result.stderr)
