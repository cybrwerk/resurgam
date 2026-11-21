"""Manifest §10.3 "Session-Lifecycle" -- proactive duration enforcement and
best-effort cleanup for the underlying `proxmox-file-restore` VM-boot
sessions (distinct from the RESURGAM-level `FileRestoreJob` queue, ADR
0013). `file_restore.status()`/`stop()` already existed but were never
called anywhere in runtime code before this -- confirmed dead code, only
exercised by their own unit tests.

`status()` returns *every* active session PBS-wide (not scoped to a
RESURGAM job or even a single repository/token), keyed by a session name
that embeds the snapshot identifier but isn't documented as a stable
format -- matched here by substring against the snapshot identifier
already computed by `browse.snapshot_identifier()`, not by parsing or
reconstructing the exact name.

Two distinct call shapes, same two primitives:
- `stop_stale_session_if_needed`: proactive, called *before* a new list/
  extract call -- if an existing session for this snapshot has been alive
  longer than the configured max, stop it first so PBS boots a fresh one.
  This is the actual "max session duration" enforcement.
- `stop_session_best_effort`: reactive, called after a job finishes
  (success, failure, or expiry-sweep) to tidy up. Deliberately swallows
  `PbsClientError` and logs rather than raising -- cleanup failing must
  never fail an already-completed/expired job, and a session PBS itself
  eventually reaps on its own internal timeout regardless.

The file-restore worker runs browse (list) jobs and downloads in two
lanes at once, so two jobs may use the same snapshot's session: each job
holds it (`hold_session`) while it runs, and neither primitive stops a
session another job of this process still holds.
"""

import logging
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager

from resurgam_worker.pbs_client import file_restore
from resurgam_worker.pbs_client.errors import PbsClientError

logger = logging.getLogger("resurgam_api.filerestore.sessions")

_holders: Counter[str] = Counter()
_holders_lock = threading.Lock()


@contextmanager
def hold_session(snapshot_id: str) -> Iterator[None]:
    """Marks `snapshot_id`'s restore session as in use by the calling job for the block."""
    with _holders_lock:
        _holders[snapshot_id] += 1
    try:
        yield
    finally:
        with _holders_lock:
            _holders[snapshot_id] -= 1
            if _holders[snapshot_id] <= 0:
                del _holders[snapshot_id]


def _holder_count(snapshot_id: str) -> int:
    with _holders_lock:
        return _holders.get(snapshot_id, 0)


def _find_session_for_snapshot(snapshot_id: str, *, token: str | None) -> file_restore.RestoreSessionStatus | None:
    sessions = file_restore.status(token=token)
    for session in sessions:
        if snapshot_id in session.name:
            return session
    return None


def stop_stale_session_if_needed(snapshot_id: str, *, token: str | None, max_age_seconds: int) -> None:
    if _holder_count(snapshot_id) > 1:
        # Another running job (besides the caller) is using this session -- stopping it would
        # kill that job's extraction. The age limit is enforced once it's done.
        return
    try:
        session = _find_session_for_snapshot(snapshot_id, token=token)
        if session is not None and session.uptime > max_age_seconds:
            logger.info(
                "file_restore: stopping session %s for %s -- uptime %ss exceeds max %ss",
                session.name,
                snapshot_id,
                session.uptime,
                max_age_seconds,
            )
            file_restore.stop(session.name, token=token)
    except PbsClientError:
        # Best-effort: a failed duration check/stop must never block the
        # actual list/extract call about to happen -- PBS's own internal
        # session timeout is the real backstop either way.
        logger.warning("file_restore: could not check/stop a stale session for %s", snapshot_id, exc_info=True)


def stop_session_best_effort(snapshot_id: str, *, token: str | None) -> None:
    if _holder_count(snapshot_id) > 0:
        return  # still in use by another running job of this worker
    try:
        session = _find_session_for_snapshot(snapshot_id, token=token)
        if session is not None:
            file_restore.stop(session.name, token=token)
    except PbsClientError:
        logger.warning("file_restore: best-effort session cleanup failed for %s", snapshot_id, exc_info=True)
