from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from resurgam_worker.pbs_client._subprocess import LONG_COMMAND_TIMEOUT_SECONDS, Guard, materialized_keyfile
from resurgam_worker.pbs_client._subprocess import run as _run
from resurgam_worker.pbs_client._subprocess import run_guarded as _run_guarded
from resurgam_worker.pbs_client.errors import (
    InvalidPathError,
    PbsBinaryNotFoundError,
    PbsCommandFailedError,
    SelectionTooLargeError,
)
from resurgam_worker.pbs_client.models import CatalogEntry, CatalogEntryType

_DEFAULT_BINARY = "proxmox-backup-client"
# `restore` (used by extract_file_to_bytes even for a single file, via
# --pattern) walks the archive's chunk stream sequentially rather than
# jumping straight to the matching entry — confirmed live (2026-09-15)
# against a real snapshot with ~63k catalog entries: extracting one
# small file near the end of the tree took over 120s. `catalog dump`
# itself stays fast (metadata only, no chunk data transfer), so only
# `restore` gets the longer budget.
_RESTORE_TIMEOUT_SECONDS = 300

# A real captured line (2026-09-14, against a real PBS 4.2 instance):
#   f "./root.pxar.didx/boot/grub/unicode.pf2" 2394108 2023-02-20T06:23:01Z
#   d "./root.pxar.didx/boot"
# Type char, quoted path, and size+mtime only for files. Symlinks ('l')
# were observed in the type-char position but never with content past
# the path in the sample captured — treated the same as a directory
# entry (no size/mtime) until a real symlink line is actually seen.
_CATALOG_LINE = re.compile(r'^([dfl])\s+"(.+?)"(?:\s+(\d+)\s+(\S+))?\s*$')

_ENTRY_TYPE_BY_CHAR = {
    "d": CatalogEntryType.DIRECTORY,
    "f": CatalogEntryType.FILE,
    "l": CatalogEntryType.SYMLINK,
}


def _resolve_binary(binary: str) -> str:
    binary_path = shutil.which(binary)
    if binary_path is None:
        raise PbsBinaryNotFoundError(binary)
    return binary_path


def _normalize_archive_path(path: str) -> str:
    """Same traversal guard as `file_restore._normalize_archive_path` —
    duplicated rather than imported to keep this module usable on its own,
    since the two PBS tools (`proxmox-file-restore` vs `proxmox-backup-client`)
    are independent enough that a future split into separate modules/files
    shouldn't need to know about each other.
    """
    if not path:
        raise InvalidPathError(path, "path must not be empty")
    if not path.startswith("/"):
        raise InvalidPathError(path, "archive path must be absolute (start with '/')")
    if ".." in path.split("/"):
        raise InvalidPathError(path, "path must not contain '..' components")
    return path


def _parse_catalog_line(line: str) -> tuple[str, CatalogEntry] | None:
    """Returns (raw_path, entry) with `raw_path` still carrying the
    `./<archive>.pxar.didx/...` prefix `dump_catalog` strips off — kept
    separate so the archive name can be inferred from the first line
    without a second parsing pass.
    """
    match = _CATALOG_LINE.match(line)
    if match is None:
        return None
    type_char, raw_path, size, mtime = match.groups()
    return raw_path, CatalogEntry(
        path=raw_path,
        type=_ENTRY_TYPE_BY_CHAR[type_char],
        size=int(size) if size is not None else None,
        mtime=mtime,
    )


def dump_catalog(
    repository: str,
    snapshot: str,
    *,
    namespace: str = "",
    token: str | None = None,
    keyfile: str | None = None,
    fingerprint: str | None = None,
    binary: str = _DEFAULT_BINARY,
) -> list[CatalogEntry]:
    """Full file listing of a CT/host snapshot via `proxmox-backup-client
    catalog dump` — confirmed live against a real PBS 4.2 instance
    (2026-09-14): a real container snapshot's entire guest file tree came
    back correctly, including nested directories several levels deep.

    Unlike `file_restore.list_path` (VM disk images, one directory level
    per call, via `proxmox-file-restore`), this dumps the *entire* catalog
    in one call — there is no per-directory listing mode for this tool.
    For a large snapshot this is a real, if unavoidable, cost of using
    this tool at all rather than `proxmox-file-restore` (which recent PBS
    versions can also use for CT/host content, but that binary isn't
    available in every environment — see the capability report).

    Returns entries with paths already stripped of the `./<archive
    name>.pxar.didx` prefix `catalog dump` includes on every line, so a
    caller sees clean root-relative paths (`/etc/hosts`, not
    `./root.pxar.didx/etc/hosts`) — the archive's own root entry becomes
    `/`.

    `keyfile` -- see `map.map_snapshot`'s docstring. Confirmed live
    (2026-09-26) that `catalog dump --help` lists `--keyfile` directly.
    """
    binary_path = _resolve_binary(binary)
    command = [binary_path, "catalog", "dump", snapshot, "--repository", repository]
    if namespace:
        command += ["--ns", namespace]

    with materialized_keyfile(keyfile) as keyfile_path:
        if keyfile_path is not None:
            command += ["--keyfile", keyfile_path]
        result = _run(command, token=token, fingerprint=fingerprint)
    if result.returncode != 0:
        raise PbsCommandFailedError(command, result.returncode, result.stderr)

    entries: list[CatalogEntry] = []
    archive_prefix: str | None = None
    # Confirmed live (2026-09-15): `catalog dump` writes the actual
    # listing to stderr, not stdout — it goes through the client's
    # logging framework rather than being treated as command output.
    # stdout is empty on a successful run. Reading both (in this order)
    # keeps this working regardless of which stream a given client
    # version chooses; non-matching lines are already skipped below.
    for line in (result.stdout + result.stderr).splitlines():
        parsed = _parse_catalog_line(line)
        if parsed is None:
            continue
        raw_path, entry = parsed

        # The first line is always the archive's own root (e.g.
        # `./root.pxar.didx`) — every subsequent path is prefixed with it.
        if archive_prefix is None:
            archive_prefix = raw_path.removeprefix("./")
            entries.append(entry.model_copy(update={"path": "/"}))
            continue

        relative = raw_path.removeprefix("./").removeprefix(archive_prefix)
        entries.append(entry.model_copy(update={"path": relative or "/"}))

    return entries


def list_directory(entries: list[CatalogEntry], directory: str) -> list[CatalogEntry]:
    """Filters an already-fetched `dump_catalog()` result down to the
    immediate children of `directory` — browsing one level at a time
    client-side against a single cached dump, rather than re-invoking the
    CLI (which has no per-directory mode anyway) on every expand.
    """
    directory = _normalize_archive_path(directory) if directory != "/" else "/"
    dir_prefix = "/" if directory == "/" else directory + "/"

    children = []
    for entry in entries:
        if entry.path == directory or not entry.path.startswith(dir_prefix):
            continue
        remainder = entry.path[len(dir_prefix) :]
        if remainder and "/" not in remainder:
            children.append(entry)
    return children


def restore_archive(
    repository: str,
    snapshot: str,
    archive_name: str,
    target_dir: str,
    *,
    namespace: str = "",
    pattern: str | None = None,
    token: str | None = None,
    keyfile: str | None = None,
    fingerprint: str | None = None,
    binary: str = _DEFAULT_BINARY,
) -> None:
    """Restores a named archive (e.g. `root.pxar`) to a local directory via
    `proxmox-backup-client restore`.

    `pattern`, when given, is passed through as `--pattern` to limit
    extraction to matching paths — confirmed live (2026-09-15) that a
    single-file pattern (e.g. `/etc/hostname`) extracts only that file,
    not the whole archive, which is what `extract_file_to_bytes` below
    relies on. Also always passes `--ignore-ownership`/
    `--ignore-permissions`: without them, extraction fails outright when
    this process isn't running as root (`failed to set ownership:
    Operation not permitted`), confirmed live against a real symlink
    entry in the same session.

    `keyfile` -- see `map.map_snapshot`'s docstring.
    """
    if not target_dir.startswith("/"):
        raise InvalidPathError(target_dir, "target_dir must be an absolute path")

    binary_path = _resolve_binary(binary)
    command = [
        binary_path,
        "restore",
        snapshot,
        archive_name,
        target_dir,
        "--repository",
        repository,
        "--ignore-ownership",
        "true",
        "--ignore-permissions",
        "true",
    ]
    if namespace:
        command += ["--ns", namespace]
    if pattern:
        command += ["--pattern", pattern]

    with materialized_keyfile(keyfile) as keyfile_path:
        if keyfile_path is not None:
            command += ["--keyfile", keyfile_path]
        result = _run(command, token=token, timeout=_RESTORE_TIMEOUT_SECONDS, fingerprint=fingerprint)
    if result.returncode != 0:
        raise PbsCommandFailedError(command, result.returncode, result.stderr)


def extract_file_to_bytes(
    repository: str,
    snapshot: str,
    archive_name: str,
    path: str,
    *,
    namespace: str = "",
    token: str | None = None,
    keyfile: str | None = None,
    fingerprint: str | None = None,
    binary: str = _DEFAULT_BINARY,
) -> bytes:
    """Extracts one file's bytes from a CT/host snapshot. Restores just
    that file (via `restore_archive`'s `--pattern` support) to a
    throwaway temp directory, reads it back out, and always cleans the
    temp directory up afterward — even on a read failure.
    """
    archive_path = _normalize_archive_path(path)
    with tempfile.TemporaryDirectory(prefix="resurgam-catalog-restore-") as tmp_dir:
        restore_archive(
            repository,
            snapshot,
            archive_name,
            tmp_dir,
            namespace=namespace,
            pattern=archive_path,
            token=token,
            keyfile=keyfile,
            fingerprint=fingerprint,
            binary=binary,
        )
        local_path = Path(tmp_dir) / archive_path.lstrip("/")
        if not local_path.is_file():
            raise InvalidPathError(path, "not a file in the restored archive")
        return local_path.read_bytes()


# `--pattern` takes a glob (pathpatterns crate), not a literal path: `*`, `?` and `[...]` are
# special and `\\` escapes the next character (pathpatterns.git src/pattern.rs, checked
# 2026-10-08). File names containing them are common enough (`[draft] notes.txt`) to matter.
_PATTERN_SPECIAL = set("\\*?[]")

# If restoring the whole selection in one call fails, every entry is retried on its own so one
# broken entry doesn't sink the rest -- but each `restore` streams the entire archive again
# (minutes on a large CT, see `_RESTORE_TIMEOUT_SECONDS`), so only up to this many.
RETRY_INDIVIDUALLY_LIMIT = 20


def escape_pattern(path: str) -> str:
    """A literal archive path as a `--pattern` that matches exactly that path."""
    return "".join(f"\\{ch}" if ch in _PATTERN_SPECIAL else ch for ch in path)


def _restore_selection_command(
    binary_path: str,
    repository: str,
    snapshot: str,
    archive_name: str,
    target_dir: str,
    paths: list[str],
    namespace: str,
) -> list[str]:
    """`proxmox-backup-client restore <snapshot> <archive-name> <target> [OPTIONS]` -- options as
    documented in the PBS docs' command syntax (pbs.proxmox.com/docs/command-syntax.html, checked
    2026-10-08): `--pattern` "Path or match pattern to limit files that get restored. Can be
    specified more than once."; `--ignore-ownership`/`--ignore-permissions` (needed unprivileged,
    ADR 0011); `--ignore-acls`/`--ignore-xattrs` (a ZIP carries neither, and setting e.g.
    `security.*` xattrs needs privileges the worker doesn't have); `--ignore-extract-device-errors`
    "ignore errors that occur during device node extraction" (an unprivileged process can't mknod
    a device -- it couldn't go into a ZIP anyway).

    A matching directory restores its whole subtree: pbs-client `pxar/extract.rs` pushes a
    directory's match result as the inherited state for its children (`current_match`). Entries
    land under `target_dir` at their full archive path (`/etc/nginx` -> `<target>/etc/nginx`),
    as `extract_file_to_bytes` already relies on.
    """
    command = [
        binary_path,
        "restore",
        snapshot,
        archive_name,
        target_dir,
        "--repository",
        repository,
        "--ignore-ownership",
        "true",
        "--ignore-permissions",
        "true",
        "--ignore-acls",
        "true",
        "--ignore-xattrs",
        "true",
        "--ignore-extract-device-errors",
        "true",
    ]
    if namespace:
        command += ["--ns", namespace]
    for path in paths:
        command += ["--pattern", escape_pattern(path)]
    return command


def _last_line(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return lines[-1] if lines else "(no error output)"


def restore_selection(
    repository: str,
    snapshot: str,
    archive_name: str,
    target_dir: str,
    paths: list[str],
    *,
    namespace: str = "",
    token: str | None = None,
    keyfile: str | None = None,
    fingerprint: str | None = None,
    timeout: float = LONG_COMMAND_TIMEOUT_SECONDS,
    guard: Guard | None = None,
    binary: str = _DEFAULT_BINARY,
) -> dict[str, str]:
    """Restores several files/directories of a CT/host snapshot into `target_dir` (which must
    exist and be empty) in ONE `restore` call -- one pass over the archive's chunk stream instead
    of one per entry.

    pbs-client's extractor aborts on the first entry it can't write (its default `on_error` is
    `Err`, pxar/extract.rs). So if the combined call fails, `target_dir` is cleared and each path
    is restored on its own: the ones that still fail are returned as `{path: reason}` (skipped,
    not fatal) and their partial output removed. Raises when every path failed, or when the
    selection is larger than `RETRY_INDIVIDUALLY_LIMIT` (`SelectionTooLargeError`). A single
    path that fails raises `PbsCommandFailedError` directly. `guard` -- see `run_guarded`.
    """
    if not target_dir.startswith("/"):
        raise InvalidPathError(target_dir, "target_dir must be an absolute path")
    if not paths:
        raise InvalidPathError("", "at least one path is required")
    archive_paths = [_normalize_archive_path(p) for p in paths]
    binary_path = _resolve_binary(binary)

    def run_once(selection: list[str]) -> tuple[list[str], int, str]:
        command = _restore_selection_command(
            binary_path, repository, snapshot, archive_name, target_dir, selection, namespace
        )
        with materialized_keyfile(keyfile) as keyfile_path:
            if keyfile_path is not None:
                command += ["--keyfile", keyfile_path]
            result = _run_guarded(command, token=token, fingerprint=fingerprint, timeout=timeout, guard=guard)
        return command, result.returncode, result.stderr

    command, returncode, stderr = run_once(archive_paths)
    if returncode == 0:
        return {}
    if len(archive_paths) == 1:
        raise PbsCommandFailedError(command, returncode, stderr)
    if len(archive_paths) > RETRY_INDIVIDUALLY_LIMIT:
        raise SelectionTooLargeError(len(archive_paths), RETRY_INDIVIDUALLY_LIMIT, stderr)

    _clear_directory(Path(target_dir))
    failed: dict[str, str] = {}
    for path in archive_paths:
        command, returncode, stderr = run_once([path])
        if returncode != 0:
            failed[path] = _last_line(stderr)
            partial = Path(target_dir) / path.lstrip("/")
            if partial.is_dir() and not partial.is_symlink():
                shutil.rmtree(partial, ignore_errors=True)
            elif partial.exists() or partial.is_symlink():
                partial.unlink(missing_ok=True)
    if len(failed) == len(archive_paths):
        raise PbsCommandFailedError(command, returncode, stderr)
    return failed


def _clear_directory(directory: Path) -> None:
    for child in directory.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def extract_file_to_path(
    repository: str,
    snapshot: str,
    archive_name: str,
    path: str,
    out_path: str,
    *,
    staging_dir: str,
    namespace: str = "",
    token: str | None = None,
    keyfile: str | None = None,
    fingerprint: str | None = None,
    timeout: float = LONG_COMMAND_TIMEOUT_SECONDS,
    guard: Guard | None = None,
    binary: str = _DEFAULT_BINARY,
) -> None:
    """Like `extract_file_to_bytes`, but the file never passes through memory: it is restored
    into `staging_dir` (created here, removed afterwards; put it on the same filesystem as
    `out_path`) and then renamed to `out_path`.
    """
    archive_path = _normalize_archive_path(path)
    staging = Path(staging_dir)
    staging.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        restore_selection(
            repository,
            snapshot,
            archive_name,
            str(staging),
            [archive_path],
            namespace=namespace,
            token=token,
            keyfile=keyfile,
            fingerprint=fingerprint,
            timeout=timeout,
            guard=guard,
            binary=binary,
        )
        local_path = staging / archive_path.lstrip("/")
        if local_path.is_symlink() or not local_path.is_file():
            raise InvalidPathError(path, "not a file in the restored archive")
        os.replace(local_path, out_path)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
