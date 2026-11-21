"""Guest-exec File Indexing (ADR 0021): the OS-aware enumeration-command
builder and output parsers shared by both opt-in methods
(`guest_exec_live`/`guest_exec_ephemeral`) -- a snapshot's file mtimes
reflect the guest's own history regardless of when/how it's booted, so
both benefit from the same `since`-based incremental filtering and the
same row shape. Pure functions, no PVE/network I/O -- `lab_agent/daemon.py`
calls `pve_client.qm.guest_exec`/`guest_file_read_all` and hands the
result here.

Output is written by the guest as many small chunk files plus one tiny
manifest, not one big file -- a real, decisive gap found live
(2026-09-25): retrieving a single large listing via PVE's own
`agent/file-read` API failed deterministically (`"got timeout"`,
QGA-level) on a real production VM. Originally believed to be a hard
size ceiling; further live testing showed it's not about size at all
(a synthetic file of equal or larger size read back fine every time) --
the real cause was never fully pinned down (a copy of the failing
content under a new filename also failed, pointing at content-triggered
AV scanning, but a separate bug in how completion was detected --
`lab_agent.daemon._wait_for_manifest`'s docstring -- means some of that
evidence may itself have been re-reading stale data rather than a fresh
run; not yet conclusively resolved). Chunking the output is the right
mitigation regardless of the exact cause: any single all-or-nothing
transfer is fragile against exactly this class of failure, and
`daemon.py` reads/parses/submits each small chunk as soon as it's
retrieved rather than holding the full listing in memory either way.

Chunk/manifest cleanup from a *previous* run is deliberately **not**
part of this script -- see `build_cleanup_command`. An earlier version
folded a `Remove-Item`/`rm -f` into this same script's own first
statement, which raced against the caller polling for the manifest to
appear: a fire-and-forget-launched enumeration hasn't necessarily
reached its own cleanup line yet by the time the very first poll runs,
so a *stale* manifest from the previous run could be found readable and
mistaken for this run's fresh result (live-confirmed: a chunk file's
`LastWriteTime` was hours older than the run that supposedly just
produced it). Cleanup is now the caller's own separate, confirmed-
complete step (run via `pve_client.qm.guest_exec`, not
`guest_exec_launch`) before this script is ever dispatched.
"""

from __future__ import annotations

from datetime import UTC, datetime

_LINUX_CHUNK_PREFIX = "/tmp/resurgam-index-chunk-"
_LINUX_MANIFEST_PATH = "/tmp/resurgam-index-manifest.txt"
_WINDOWS_CHUNK_PREFIX = "C:\\Windows\\Temp\\resurgam-index-chunk-"
_WINDOWS_MANIFEST_PATH = "C:\\Windows\\Temp\\resurgam-index-manifest.txt"
# Created atomically (mkdir) as the scan's first step; a second start of the
# same scan finds it and exits at once. Lets the agent retry a start whose
# guest-agent call timed out without ever running two scans side by side
# (live finding 2026-11-21: on a guest booting slowly from its backup the
# first `guest-exec` timed out). Removed by `build_cleanup_command`.
_LINUX_LOCK_PATH = "/tmp/resurgam-index.lock"
_WINDOWS_LOCK_PATH = "C:\\Windows\\Temp\\resurgam-index.lock"

# Rows per chunk file. Conservative by a wide margin against the one
# ceiling actually measured live (44MB fails, a few hundred bytes
# succeeds) -- a real path+metadata row is on the order of 100 bytes, so
# 5000 rows is well under 1MB, independent of whichever exact threshold
# PVE's undocumented timeout actually trips on.
_CHUNK_ROWS = 5000

# PVE's own `ostype` vocabulary (confirmed against every value this
# project's own reconstructed configs have used: win10/win11/l26/...)
# -- Windows-family values always start with "w", Linux-family with "l".
_WINDOWS_OSTYPE_PREFIX = "w"
_LINUX_OSTYPE_PREFIX = "l"

_TYPE_CHAR_TO_ENTRY_TYPE = {"f": "file", "d": "directory", "l": "symlink"}


class UnsupportedOsTypeError(Exception):
    def __init__(self, ostype: str) -> None:
        self.ostype = ostype
        super().__init__(f"guest-exec indexing has no enumeration command for ostype {ostype!r}")


def manifest_path_for_ostype(ostype: str) -> str:
    if ostype.startswith(_WINDOWS_OSTYPE_PREFIX):
        return _WINDOWS_MANIFEST_PATH
    if ostype.startswith(_LINUX_OSTYPE_PREFIX):
        return _LINUX_MANIFEST_PATH
    raise UnsupportedOsTypeError(ostype)


def chunk_path_for_ostype(ostype: str, index: int) -> str:
    """Matches the guest-side naming each script writes: GNU `split -d -a
    4` on Linux, a manually zero-padded `{0:D4}` StreamWriter path on
    Windows -- both four-digit zero-padded, starting at 0000.
    """
    if ostype.startswith(_WINDOWS_OSTYPE_PREFIX):
        return f"{_WINDOWS_CHUNK_PREFIX}{index:04d}"
    if ostype.startswith(_LINUX_OSTYPE_PREFIX):
        return f"{_LINUX_CHUNK_PREFIX}{index:04d}"
    raise UnsupportedOsTypeError(ostype)


def parse_chunk_count(manifest_text: str) -> int:
    """Both scripts write a bare integer (no newline on Windows,
    trailing newline on Linux's `echo`) -- `.strip()` handles either.
    """
    return int(manifest_text.strip())


def build_cleanup_command(ostype: str) -> list[str]:
    """Deletes any chunk/manifest files a *previous* run left behind --
    deliberately run by the caller as its own, separate, fast, reliably
    awaitable command (`pve_client.qm.guest_exec`, not
    `guest_exec_launch`) *before* dispatching the real enumeration, not
    folded into the enumeration script's own start (as an earlier
    version did).

    A real, decisive race found live (2026-09-25): with cleanup as the
    enumeration script's own first statement, `lab_agent.daemon`'s
    `_wait_for_manifest` could poll and find a *stale* manifest from the
    previous run still sitting there, readable, before the freshly
    launched (fire-and-forget) enumeration had even reached its own
    Remove-Item line yet -- silently treating old data as this run's
    result rather than failing loudly. Live-confirmed: a chunk file's
    `LastWriteTime` was hours older than the run that supposedly just
    produced it. Running cleanup as its own confirmed-complete step
    first closes the gap: by the time the real enumeration is launched,
    no manifest exists to be mistaken for a fresh one.
    """
    if ostype.startswith(_WINDOWS_OSTYPE_PREFIX):
        script = (
            f"Remove-Item -Path ({_powershell_single_quote(_WINDOWS_CHUNK_PREFIX)} + '*') -Force -ErrorAction SilentlyContinue; "
            f"Remove-Item -Path {_powershell_single_quote(_WINDOWS_MANIFEST_PATH)} -Force -ErrorAction SilentlyContinue; "
            f"Remove-Item -Path {_powershell_single_quote(_WINDOWS_LOCK_PATH)} -Recurse -Force -ErrorAction SilentlyContinue"
        )
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]
    if ostype.startswith(_LINUX_OSTYPE_PREFIX):
        script = f"rm -f {_LINUX_CHUNK_PREFIX}* {_LINUX_MANIFEST_PATH}; rmdir {_LINUX_LOCK_PATH} 2>/dev/null; true"
        return ["/bin/sh", "-c", script]
    raise UnsupportedOsTypeError(ostype)


def _powershell_single_quote(value: str) -> str:
    """PowerShell single-quoted strings do zero interpolation -- the
    only escape needed is doubling an embedded `'`. Used for every
    exclude pattern embedded into the generated script: these come from
    `BackupGroup.file_index_exclude_patterns`, an operator-editable
    field, not a trusted literal -- naive string formatting here would
    reopen exactly the "no arbitrary scripts" trust boundary this
    project has otherwise held everywhere else (Recovery Lab's check
    types, the enumeration command's own fixed shape).
    """
    return "'" + value.replace("'", "''") + "'"


def _posix_shell_single_quote(value: str) -> str:
    """POSIX `sh` single-quoted strings do zero interpolation -- the
    standard escape for an embedded `'` is closing the quote, an
    escaped literal quote, then reopening it. Same trust-boundary
    reasoning as `_powershell_single_quote`.
    """
    return "'" + value.replace("'", "'\\''") + "'"


def build_enumeration_command(
    ostype: str, *, since: datetime | None = None, exclude_patterns: list[str] | None = None
) -> list[str]:
    """The guest-side command argv for `pve_client.qm.guest_exec` --
    fixed and project-authored, never operator- or customer-suppliable
    (manifest §22.6's "no arbitrary scripts" discipline, already applied
    to Recovery Lab's own check types, extended here). `qm guest exec`
    runs a bare executable + argv, not a shell string, so redirection
    needs an explicit shell/interpreter as the executable itself.

    Both OS families emit the same four-column shape (type char, size,
    mtime as Unix epoch seconds, path) so a single row-to-dict step
    (`_row_to_entry`) serves both parsers -- Windows emits epoch
    seconds explicitly (`Get-Date -UFormat %s`) specifically to avoid
    any locale-dependent .NET DateTime string parsing.

    `exclude_patterns` (`BackupGroup.file_index_exclude_patterns`) prunes
    matching subtrees *during* the walk, not just filters the results
    afterward -- a real, decisive gap found live (2026-09-24): the
    original version walked (and transferred back) the entire drive
    unconditionally, including paths already excluded by default for
    exactly this reason (`Windows/WinSxS` etc., ADR 0016). Confirmed
    live against a real production VM: a full, unpruned walk produced a
    95MB listing and ran 9+ minutes unattended before being killed.
    Patterns are the same Python-`re` strings the backend already
    compiles (`worker.file_index_worker._compile_exclude_patterns`) --
    passed through as-is to PowerShell's .NET regex (`-imatch`, a
    superset of Python's own syntax for every pattern this project ships
    or has seen) and GNU `find`'s POSIX-extended regex (`-iregex`, close
    enough for these patterns' own simple constructs: anchors,
    alternation, escaped literals). A custom operator pattern using a
    Python-only regex feature simply won't prune on the guest side --
    the backend's own post-submit filtering (`api/v1/lab_agent.py`'s
    `submit_lab_agent_file_index_entries`) still catches it, so
    correctness never depends on this translation being perfect, only
    efficiency does.
    """
    exclude_patterns = exclude_patterns or []

    if ostype.startswith(_WINDOWS_OSTYPE_PREFIX):
        since_literal = f"{since.timestamp():.0f}" if since is not None else "$null"
        exclude_literal = ", ".join(_powershell_single_quote(p) for p in exclude_patterns)
        chunk_prefix_literal = _powershell_single_quote(_WINDOWS_CHUNK_PREFIX)
        script = (
            f"try {{ New-Item -ItemType Directory -Path {_powershell_single_quote(_WINDOWS_LOCK_PATH)} "
            "-ErrorAction Stop | Out-Null } catch { exit 0 }; "
            f"$exclude = @({exclude_literal}); $since = {since_literal}; $chunkSize = {_CHUNK_ROWS}; "
            f"$chunkPrefix = {chunk_prefix_literal}; "
            "$chunkIndex = 0; $rowCount = 0; "
            "$sw = New-Object System.IO.StreamWriter(($chunkPrefix + '0000'), $false, (New-Object System.Text.UTF8Encoding($false))); "
            "function Test-Excluded($p) { foreach ($pat in $exclude) { if ($p -imatch $pat) { return $true } }; return $false }; "
            "function NextChunk { "
            "$script:sw.Close(); $script:chunkIndex++; "
            "$script:sw = New-Object System.IO.StreamWriter(($chunkPrefix + ('{0:D4}' -f $script:chunkIndex)), $false, (New-Object System.Text.UTF8Encoding($false))); "
            "$script:rowCount = 0 "
            "}; "
            "function Emit($item) { "
            "if ($script:rowCount -ge $chunkSize) { NextChunk }; "
            "$type = if ($item.PSIsContainer) { 'd' } else { 'f' }; "
            "$size = if ($item.PSIsContainer) { 0 } else { $item.Length }; "
            "$mtime = [int64](Get-Date $item.LastWriteTimeUtc -UFormat %s); "
            "$script:sw.WriteLine(\"$type`t$size`t$mtime`t$($item.FullName -replace '\\\\','/')\"); "
            "$script:rowCount++ "
            "}; "
            "function Walk($dir) { "
            "Get-ChildItem -LiteralPath $dir -Force -ErrorAction SilentlyContinue | ForEach-Object { "
            "$rel = $_.FullName -replace '\\\\','/'; "
            "if (Test-Excluded $rel) { return }; "
            "if (-not $since -or ([int64](Get-Date $_.LastWriteTimeUtc -UFormat %s)) -gt $since) { Emit $_ }; "
            "if ($_.PSIsContainer) { Walk $_.FullName } "
            "} }; "
            "Walk 'C:\\'; "
            "$sw.Close(); "
            f"($chunkIndex + 1) | Out-File -Encoding ascii -NoNewline {_powershell_single_quote(_WINDOWS_MANIFEST_PATH)}"
        )
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]

    if ostype.startswith(_LINUX_OSTYPE_PREFIX):
        since_clause = ""
        if since is not None:
            since_clause = f"-newermt '@{since.timestamp():.0f}' "
        prune_clause = ""
        if exclude_patterns:
            regex_args = " -o ".join(f"-iregex {_posix_shell_single_quote(p)}" for p in exclude_patterns)
            prune_clause = f"-regextype posix-extended \\( {regex_args} \\) -prune -o "
        script = (
            f"mkdir {_LINUX_LOCK_PATH} 2>/dev/null || exit 0; "
            f"nice -n 19 ionice -c3 find / -xdev {prune_clause}{since_clause}"
            f"-printf '%y\\t%s\\t%T@\\t%p\\n' 2>/dev/null | "
            f"split -d -a 4 -l {_CHUNK_ROWS} - {_LINUX_CHUNK_PREFIX}; "
            f"ls {_LINUX_CHUNK_PREFIX}* 2>/dev/null | wc -l > {_LINUX_MANIFEST_PATH}"
        )
        return ["/bin/sh", "-c", script]

    raise UnsupportedOsTypeError(ostype)


def _row_to_entry(type_char: str, size_text: str, mtime_epoch_text: str, path: str) -> dict:
    entry_type = _TYPE_CHAR_TO_ENTRY_TYPE.get(type_char, "other")
    try:
        mtime = datetime.fromtimestamp(float(mtime_epoch_text), tz=UTC)
    except (ValueError, OverflowError, OSError):
        mtime = None
    size_bytes = None
    if entry_type == "file":
        try:
            size_bytes = int(float(size_text))
        except ValueError:
            size_bytes = None
    normalized_path = path.replace("\\", "/")
    filename = normalized_path.rsplit("/", 1)[-1] or normalized_path
    return {
        "path": normalized_path,
        "filename": filename,
        "type": entry_type,
        "size_bytes": size_bytes,
        "mtime": mtime,
    }


def parse_linux_find_output(text: str) -> list[dict]:
    """`type\\tsize\\tmtime_epoch\\tpath` per line (`find -printf`'s own
    format, see `build_enumeration_command`). Malformed lines are
    skipped, not fatal -- a single odd path (e.g. containing a literal
    newline, however rare) shouldn't lose an entire index run.
    """
    entries = []
    for line in text.splitlines():
        if not line:
            continue
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        entries.append(_row_to_entry(*parts))
    return entries


def parse_output_for_ostype(ostype: str, text: str) -> list[dict]:
    """Dispatches to the right parser for this `ostype` -- both families
    now emit the identical `type\\tsize\\tmtime_epoch\\tpath` row shape
    (see `build_enumeration_command`; Windows switched from
    `ConvertTo-Csv` to plain TSV in the 2026-09-25 chunking rewrite, so
    `lab_agent.daemon` doesn't need to re-derive the OS family a second
    time to know which parser applies -- only `UnsupportedOsTypeError`'s
    validation still depends on it).
    """
    if ostype.startswith((_WINDOWS_OSTYPE_PREFIX, _LINUX_OSTYPE_PREFIX)):
        return parse_linux_find_output(text)
    raise UnsupportedOsTypeError(ostype)
