from __future__ import annotations

import contextlib
import json
import os
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass

from resurgam_worker._subprocess import run_subprocess
from resurgam_worker.pbs_client.errors import PbsCommandFailedError, PbsOutputParseError

# Confirmed live (2026-09-15) against a real PVE 9.2 node with matched
# package versions (proxmox-backup-file-restore/proxmox-backup-restore-image/
# pve-qemu-kvm, all from the same PVE apt repo — mixing repos or Debian
# releases is what caused this to hang indefinitely in earlier testing, see
# ADR 0011): a cold VM boot + directory listing took ~4s, a warm extract
# under 1s. 120s leaves real margin for a slower cold boot under load
# without masking a genuine hang for many minutes. Shared by every PBS
# client wrapper in this package (`file_restore.py`, `catalog.py`,
# `map.py`) -- not duplicated per module.
COMMAND_TIMEOUT_SECONDS = 120


def run(
    command: list[str],
    *,
    capture_text: bool = True,
    token: str | None = None,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
    fingerprint: str | None = None,
) -> subprocess.CompletedProcess:
    """`token`, when given, is the PBS API token secret — passed via the
    `PBS_PASSWORD` env var every PBS client binary here reads for
    API-token auth, scoped to this one subprocess call only (a per-call
    `env` dict, never `os.environ[...] = ...`), so it can never leak into
    this process's own environment or a concurrent, unrelated call.

    `timeout` defaults to `COMMAND_TIMEOUT_SECONDS` but is overridable per
    call -- `catalog.restore_archive`/`extract_file_to_bytes` need a much
    longer budget (confirmed live: extracting one file near the end of a
    ~63k-entry catalog took over 120s) than a typical metadata-only call.

    `fingerprint` -- the PBS server certificate's SHA-256 fingerprint the operator pinned for this
    instance (RESURGAM's `fingerprint` TLS trust mode, needed for PBS's default self-signed
    certificate) -- is passed as `PBS_FINGERPRINT`. Without it the CLI tools only accept a
    CA-trusted certificate or one confirmed interactively into the caller's
    ~/.config/proxmox-backup/fingerprints, which the sandboxed, non-interactive workers can't do
    ("certificate validation failed - Certificate fingerprint was not confirmed", found running
    the appliance's workers against a self-signed PBS, 2026-10-07).
    """
    return run_subprocess(
        command,
        timeout=timeout,
        on_timeout=PbsCommandFailedError,
        env=_pbs_env(token, fingerprint),
        capture_text=capture_text,
    )


def _pbs_env(token: str | None, fingerprint: str | None) -> dict[str, str] | None:
    extra = {}
    if token is not None:
        extra["PBS_PASSWORD"] = token
    if fingerprint:
        extra["PBS_FINGERPRINT"] = format_fingerprint(fingerprint)
    return {**os.environ, **extra} if extra else None


# Archive (folder/multi-selection ZIP) extraction can legitimately run for hours on a large
# selection; the caller passes its own configured budget. This is only the fallback.
LONG_COMMAND_TIMEOUT_SECONDS = 4 * 60 * 60

Guard = Callable[[], None]


@dataclass
class GuardedResult:
    returncode: int
    stderr: str


def run_guarded(
    command: list[str],
    *,
    token: str | None = None,
    fingerprint: str | None = None,
    timeout: float = LONG_COMMAND_TIMEOUT_SECONDS,
    stdout_path: str | None = None,
    guard: Guard | None = None,
    guard_interval: float = 2.0,
) -> GuardedResult:
    """Runs a long, data-producing PBS CLI call (`extract ... -` to a file, `restore` into a
    directory) without holding its output in memory, while watching it.

    - stdout goes straight to `stdout_path` (created 0640) or is discarded; stderr is spooled to an
      anonymous temp file, never a pipe (a pipe nobody reads blocks the tool once its buffer fills).
    - Every `guard_interval` seconds `guard()` runs; it aborts the call by *raising* (e.g. the
      caller's "disk is filling up" / "selection exceeds the size limit" error). The process is
      then killed and the guard's exception propagates unchanged.
    - Past `timeout` the process is killed and `PbsCommandFailedError(command, -1, ...)` is
      raised, the same contract as `run()`.
    Same env handling as `run()` (`PBS_PASSWORD`/`PBS_FINGERPRINT` per call only).
    """
    env = _pbs_env(token, fingerprint)
    out_fd = (
        os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        if stdout_path
        else os.open(os.devnull, os.O_WRONLY)
    )
    try:
        with tempfile.TemporaryFile() as err_fh:
            proc = subprocess.Popen(command, stdout=out_fd, stderr=err_fh, env=env)  # noqa: S603 - fixed binary + argument list, no shell
            deadline = time.monotonic() + timeout
            try:
                while True:
                    try:
                        returncode = proc.wait(timeout=guard_interval)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if time.monotonic() > deadline:
                        raise PbsCommandFailedError(command, -1, f"timed out after {timeout:.0f}s")
                    if guard is not None:
                        guard()
            except BaseException:
                proc.kill()
                proc.wait()
                raise
            err_fh.seek(0)
            stderr = err_fh.read().decode(errors="replace")
    finally:
        os.close(out_fd)
    return GuardedResult(returncode=returncode, stderr=stderr)


def format_fingerprint(fingerprint: str) -> str:
    """`F3A3560D...` / `f3:a3:56:...` -> `f3:a3:56:...`, the form the PBS CLI tools expect."""
    hex_digits = fingerprint.replace(":", "").strip().lower()
    return ":".join(hex_digits[i : i + 2] for i in range(0, len(hex_digits), 2))


def run_json(command: list[str], *, token: str | None = None, fingerprint: str | None = None) -> object:
    result = run(command, token=token, fingerprint=fingerprint)
    if result.returncode != 0:
        raise PbsCommandFailedError(command, result.returncode, result.stderr)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PbsOutputParseError(command, result.stdout, exc) from exc


@contextlib.contextmanager
def materialized_keyfile(content: str | None):
    """Yields a filesystem path to `content` (a PBS keyfile's raw text,
    as produced by `proxmox-backup-client key create`/`key show`) for the
    duration of the `with` block, or `None` straight through when there's
    no key -- every `pbs_client` function that reads snapshot data needs
    `--keyfile <path>` (confirmed live: `map`/`restore`/`file-restore` all
    accept it directly), but the key's plaintext only ever exists as a
    RESURGAM secret or an in-flight job field, never a standing file on
    disk. Written with mode 0600 before any content touches it (`open`
    combined with `os.open`'s own restrictive flags, not a
    world-readable-by-default temp file followed by a chmod) and unlinked
    in `finally` -- including when the caller's own command raises,
    matching this project's existing "never outlive the one call that
    needed it" discipline for other secrets (e.g. `PBS_PASSWORD`'s
    per-call-only env var, above).
    """
    if content is None:
        yield None
        return
    fd, path = tempfile.mkstemp(prefix="resurgam-pbs-keyfile-")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        yield path
    finally:
        os.unlink(path)
