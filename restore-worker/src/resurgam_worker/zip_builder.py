"""Builds the single ZIP a folder / multi-selection file-restore download produces.

Fed from two kinds of source, one per selected snapshot entry:
- a local file or directory tree (CT/host: what `proxmox-backup-client restore --pattern ...`
  wrote into a staging directory; VM single files: what `proxmox-file-restore extract ... -`
  streamed into a file), via `add_tree`;
- a ZIP the PBS tool already built (VM directories: `proxmox-file-restore extract --format zip`,
  see `pbs_client.file_restore.extract_to_file`), merged member by member via `add_zip`.

Either way every entry goes through the same rules, so both backup types produce the same kind
of archive:
- Paths are relative to the selection's common parent (the caller passes each source's archive
  name components); directories get explicit entries, so empty ones survive.
- Names are written as UTF-8 with the ZIP "language encoding" flag (general purpose bit 11,
  APPNOTE 6.3.x appendix D) -- Python's `zipfile` sets it for any name that isn't pure ASCII.
  Bytes that aren't valid UTF-8 (possible on Linux file systems) become U+FFFD.
- Names that Windows can't create on extraction -- `< > : " / \\ | ? *`, control characters,
  trailing dots/spaces, reserved device names like `CON` or `LPT1` -- are made safe with `_`,
  and two names that only differ in case get a ` (n)` suffix (NTFS is case-insensitive). Every
  such change is reported in `ZipReport.renamed`, never silent.
- Symlinks are stored as symlinks (Unix mode S_IFLNK, target as content), never followed --
  following one could pull a file from the *worker host* into the archive. Device nodes, FIFOs
  and sockets can't be stored and are reported as skipped, as is anything unreadable; one bad
  entry never fails the whole archive.
- ZIP64 kicks in automatically for members over ~4 GiB (`zipfile` decides from the known size).

Reading and writing fail differently: an unreadable *source* entry is skipped and reported,
but a failed *write* to the ZIP itself (ENOSPC, I/O error) raises `ZipWriteError` -- the archive
is corrupt from that point on, so the caller must abort and delete the partial file.

`add_tree(..., remove_after=True)` deletes each local file right after it is in the archive, so
the disk holds roughly "selection + finished part of the ZIP", not twice the selection.
"""

from __future__ import annotations

import os
import shutil
import stat
import time
import unicodedata
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

_COPY_CHUNK = 1024 * 1024
# Windows: characters not allowed in a file name, plus everything below 0x20
# (learn.microsoft.com "Naming Files, Paths, and Namespaces").
_WINDOWS_FORBIDDEN = set('<>:"/\\|?*') | {chr(c) for c in range(32)}
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
# ZIP's DOS timestamps can't go below 1980.
_MIN_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
# Reports are stored with the job and shown in the UI; beyond this only the counts grow.
MAX_REPORTED_ENTRIES = 200


class ZipWriteError(OSError):
    """Writing the ZIP itself failed (e.g. no space left on the output file system). The partial
    archive is unusable; the caller deletes it and fails the job with this message."""

    def __init__(self, cause: OSError) -> None:
        self.cause = cause
        reason = cause.strerror or str(cause)
        super().__init__(cause.errno, f"writing the ZIP failed: {reason}")


@dataclass
class SkippedEntry:
    path: str
    reason: str


@dataclass
class RenamedEntry:
    path: str
    archive_name: str


@dataclass
class ZipReport:
    file_count: int = 0
    directory_count: int = 0
    symlink_count: int = 0
    total_bytes: int = 0
    skipped_count: int = 0
    renamed_count: int = 0
    skipped: list[SkippedEntry] = field(default_factory=list)
    renamed: list[RenamedEntry] = field(default_factory=list)
    # Size of the member about to be written while `check` runs before it (0 between members),
    # so a size limit can refuse a member *before* it pushes the archive over the limit.
    pending_bytes: int = 0

    def as_dict(self) -> dict:
        return {
            "file_count": self.file_count,
            "directory_count": self.directory_count,
            "symlink_count": self.symlink_count,
            "total_bytes": self.total_bytes,
            "skipped_count": self.skipped_count,
            "renamed_count": self.renamed_count,
            "skipped": [{"path": s.path, "reason": s.reason} for s in self.skipped],
            "renamed": [{"path": r.path, "archive_name": r.archive_name} for r in self.renamed],
        }


def sanitize_component(raw: str) -> str:
    """One path component -> a name every common unzip tool (Windows Explorer included) accepts."""
    # surrogateescape round-trip: os.scandir hands undecodable bytes back as lone surrogates.
    name = raw.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    name = unicodedata.normalize("NFC", name)
    name = "".join("_" if ch in _WINDOWS_FORBIDDEN else ch for ch in name)
    stripped = name.rstrip(" .")
    if stripped != name:
        name = stripped + "_"
    if name in ("", ".", ".."):
        name = "_"
    if name.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        name = "_" + name
    return name


def _zip_time(epoch: float) -> tuple[int, int, int, int, int, int]:
    t = time.gmtime(max(epoch, 0))[:6]
    return max(t, _MIN_ZIP_TIME)


class ZipBuilder:
    """See the module docstring. `check(report)` runs before every member (the caller's size
    limit / free-disk-space guard: abort by raising); `on_progress(report)` at most once a second.
    Use as a context manager, or call `close()`; an exception leaves a partial file the caller
    deletes.
    """

    def __init__(
        self,
        out_path: str | Path,
        *,
        check: Callable[[ZipReport], None] | None = None,
        on_progress: Callable[[ZipReport], None] | None = None,
        compresslevel: int = 1,
    ) -> None:
        # Deflate level 1: most of the size win at a fraction of the CPU of the default 6 -- this
        # runs over up to tens of GB of backup data, much of it already compressed.
        self._zip = zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=compresslevel)
        self._check = check
        self._on_progress = on_progress
        self._last_progress = 0.0
        self.report = ZipReport()
        # (sanitized parent, raw name) -> sanitized name; sanitized parent -> used names (casefolded)
        self._names: dict[tuple[str, str], str] = {}
        self._used: dict[str, set[str]] = {}
        self._written_dirs: set[str] = set()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.close()
            return
        # Already failing: closing may fail again (e.g. still no space) -- keep the first error.
        try:
            self._zip.close()
        except OSError:
            pass

    def close(self) -> ZipReport:
        try:
            self._zip.close()
        except OSError as exc:
            raise ZipWriteError(exc) from exc
        return self.report

    # --- reporting ----------------------------------------------------------------------------

    def skip(self, path: str, reason: str) -> None:
        self.report.skipped_count += 1
        if len(self.report.skipped) < MAX_REPORTED_ENTRIES:
            self.report.skipped.append(SkippedEntry(path=path, reason=reason))

    def _renamed(self, path: str, archive_name: str) -> None:
        self.report.renamed_count += 1
        if len(self.report.renamed) < MAX_REPORTED_ENTRIES:
            self.report.renamed.append(RenamedEntry(path=path, archive_name=archive_name))

    def _tick(self, pending_bytes: int = 0) -> None:
        if self._check is not None:
            self.report.pending_bytes = pending_bytes
            try:
                self._check(self.report)
            finally:
                self.report.pending_bytes = 0
        now = time.monotonic()
        if self._on_progress is not None and now - self._last_progress >= 1.0:
            self._last_progress = now
            self._on_progress(self.report)

    # --- names --------------------------------------------------------------------------------

    def _arcname(self, components: list[str], source_path: str) -> str:
        """Sanitized, collision-free ZIP name for a raw component list (stable per raw path)."""
        parent = ""
        newly_changed = False
        for raw in components:
            key = (parent, raw)
            name = self._names.get(key)
            if name is None:
                name = sanitize_component(raw)
                used = self._used.setdefault(parent, set())
                base, n = name, 1
                while name.casefold() in used:
                    stem, dot, ext = base.partition(".") if not base.startswith(".") else (base, "", "")
                    name = f"{stem} ({n}){dot}{ext}"
                    n += 1
                used.add(name.casefold())
                self._names[key] = name
                # Reported once, where the change happens -- not again for every child of a
                # renamed directory.
                newly_changed = newly_changed or name != raw
            parent = f"{parent}/{name}" if parent else name
        if newly_changed:
            self._renamed(source_path, parent)
        return parent

    def _ensure_dirs(self, arcname: str, date_time: tuple | None = None) -> None:
        """Explicit entries for every ancestor directory of `arcname` (and `arcname` itself when
        `date_time` is given, i.e. it *is* a directory)."""
        parts = arcname.split("/")
        upto = parts if date_time is not None else parts[:-1]
        for i in range(1, len(upto) + 1):
            dir_name = "/".join(upto[:i])
            if dir_name in self._written_dirs:
                continue
            own = date_time if i == len(upto) and date_time is not None else _zip_time(time.time())
            info = zipfile.ZipInfo(dir_name + "/", date_time=own)
            info.external_attr = ((stat.S_IFDIR | 0o755) << 16) | 0x10  # 0x10: MS-DOS directory attribute
            self._writestr(info, b"")
            self._written_dirs.add(dir_name)
            self.report.directory_count += 1

    # --- local trees --------------------------------------------------------------------------

    def add_tree(
        self, local_path: Path, components: list[str], archive_path: str, *, remove_after: bool = True
    ) -> None:
        """Adds a local file or directory tree as `components` (its name inside the ZIP).
        `archive_path` is the entry's path inside the snapshot, for reports."""
        try:
            st = os.lstat(local_path)
        except OSError as exc:
            self.skip(archive_path, f"not restored: {exc.strerror or exc}")
            return
        self._add_entry(local_path, st, components, archive_path, remove_after)

    def _add_entry(
        self, local: Path, st: os.stat_result, components: list[str], archive_path: str, remove: bool
    ) -> None:
        self._tick()
        if stat.S_ISDIR(st.st_mode):
            arcname = self._arcname(components, archive_path)
            self._ensure_dirs(arcname, date_time=_zip_time(st.st_mtime))
            try:
                children = sorted(os.scandir(local), key=lambda e: e.name)
            except OSError as exc:
                self.skip(archive_path, f"directory not readable: {exc.strerror or exc}")
                return
            for child in children:
                try:
                    child_st = child.stat(follow_symlinks=False)
                except OSError as exc:
                    self.skip(f"{archive_path.rstrip('/')}/{child.name}", f"not readable: {exc.strerror or exc}")
                    continue
                self._add_entry(
                    Path(child.path),
                    child_st,
                    [*components, child.name],
                    f"{archive_path.rstrip('/')}/{child.name}",
                    remove,
                )
            if remove:
                shutil.rmtree(local, ignore_errors=True)
        elif stat.S_ISLNK(st.st_mode):
            try:
                target = os.readlink(local)
            except OSError as exc:
                self.skip(archive_path, f"symlink not readable: {exc.strerror or exc}")
                return
            arcname = self._arcname(components, archive_path)
            self._ensure_dirs(arcname)
            self._write_symlink(arcname, os.fsencode(target), _zip_time(st.st_mtime))
            if remove:
                local.unlink(missing_ok=True)
        elif stat.S_ISREG(st.st_mode):
            try:
                # O_NOFOLLOW: the lstat above said "regular file"; never let a swap to a symlink
                # in between redirect the read elsewhere.
                fd = os.open(local, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError as exc:
                self.skip(archive_path, f"not readable: {exc.strerror or exc}")
                return
            arcname = self._arcname(components, archive_path)
            self._ensure_dirs(arcname)
            self._tick(st.st_size)
            with os.fdopen(fd, "rb") as src:
                self._write_file(arcname, src, st.st_size, _zip_time(st.st_mtime), st.st_mode, archive_path)
            if remove:
                local.unlink(missing_ok=True)
        else:
            self.skip(archive_path, "special file (device, FIFO or socket) -- cannot be stored in a ZIP")
            if remove:
                local.unlink(missing_ok=True)

    def _write_file(self, arcname: str, src, size: int, date_time: tuple, mode: int, archive_path: str) -> None:
        info = zipfile.ZipInfo(arcname, date_time=date_time)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = (stat.S_IFREG | (stat.S_IMODE(mode) or 0o644)) << 16
        # Known size up front: zipfile switches this member to ZIP64 when it may exceed 4 GiB.
        info.file_size = size
        written = 0
        read_error: Exception | None = None
        try:
            with self._zip.open(info, "w") as dst:
                while True:
                    try:
                        chunk = src.read(_COPY_CHUNK)
                    except (OSError, zipfile.BadZipFile, EOFError) as exc:
                        # Source trouble: stop this member, keep the archive consistent (the
                        # member is closed below with what was read so far).
                        read_error = exc
                        break
                    if not chunk:
                        break
                    dst.write(chunk)
                    written += len(chunk)
        except OSError as exc:
            # Writing the archive failed (ENOSPC, I/O error): it is corrupt from here on.
            raise ZipWriteError(exc) from exc
        if read_error is not None:
            # The member is already (partly) in the archive -- a ZIP can't take it back out.
            self.skip(archive_path, f"read failed after {written} bytes, the archive copy is incomplete: {read_error}")
            return
        self.report.file_count += 1
        self.report.total_bytes += written

    def _writestr(self, info: zipfile.ZipInfo, data: bytes) -> None:
        try:
            self._zip.writestr(info, data)
        except OSError as exc:
            raise ZipWriteError(exc) from exc

    def _write_symlink(self, arcname: str, target: bytes, date_time: tuple) -> None:
        info = zipfile.ZipInfo(arcname, date_time=date_time)
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        info.compress_type = zipfile.ZIP_STORED
        self._writestr(info, target)
        self.report.symlink_count += 1

    # --- merging a tool-built ZIP -------------------------------------------------------------

    def add_zip(self, zip_path: Path, prefix: list[str], archive_parent: str) -> None:
        """Merges the members of a ZIP `proxmox-file-restore extract --format zip` produced.
        Its member names are relative to the extracted directory's parent (they start with the
        directory's own name); `prefix` is that parent's position inside this archive and
        `archive_parent` its snapshot path, for reports."""
        try:
            source = zipfile.ZipFile(zip_path)
        except (zipfile.BadZipFile, OSError) as exc:
            detail = _peek_text(zip_path)
            self.skip(archive_parent, f"the PBS tool returned no valid ZIP ({exc}){': ' + detail if detail else ''}")
            return
        with source:
            for member in source.infolist():
                self._tick()
                parts = [p for p in member.filename.split("/") if p]
                safe_parts = [p for p in parts if p not in (".", "..")]
                member_path = f"{archive_parent.rstrip('/')}/{'/'.join(parts)}"
                if not safe_parts:
                    continue
                if safe_parts != parts:
                    self._renamed(member_path, "/".join(safe_parts))
                mode = member.external_attr >> 16
                date_time = max(tuple(member.date_time), _MIN_ZIP_TIME)
                if member.is_dir():
                    self._ensure_dirs(self._arcname([*prefix, *safe_parts], member_path), date_time=date_time)
                    continue
                if mode and stat.S_ISLNK(mode):
                    arcname = self._arcname([*prefix, *safe_parts], member_path)
                    self._ensure_dirs(arcname)
                    self._write_symlink(arcname, source.read(member), date_time)
                    continue
                arcname = self._arcname([*prefix, *safe_parts], member_path)
                self._ensure_dirs(arcname)
                try:
                    src = source.open(member)
                except (zipfile.BadZipFile, OSError, NotImplementedError) as exc:
                    self.skip(member_path, f"not readable in the PBS tool's ZIP: {exc}")
                    continue
                with src:
                    self._tick(member.file_size)
                    self._write_file(arcname, src, member.file_size, date_time, mode or 0o644, member_path)


def _peek_text(path: Path, limit: int = 300) -> str:
    """The start of a non-ZIP output file as text -- the tool reports some failures as a JSON
    `{"message": ...}` on stdout with exit code 0 (see `file_restore.list_path`)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(limit)
    except OSError:
        return ""
    return head.decode("utf-8", "replace").strip()
