"""The out-of-process consumer for `FileRestoreJob` (ADR 0002, ADR 0013).

Launched as a *separate* OS process from the uvicorn API server (dev:
`sudo uv run resurgam-file-restore-worker`, run from `backend/` so `.env`
still resolves under `sudo`'s reset environment) -- not folded into the
API process, because the VM file-restore path (`resurgam_worker.pbs_client
.file_restore`) requires the calling process to be root beyond its
top-level disk listing, and the API process must not run as root.

Calls `filerestore.browse.list_directory`/`download_file` unchanged --
per ADR 0011's own stated design, moving these behind a queue is "a
relocation of the call site, not a rewrite of the logic".

Two lanes, one thread each, concurrency 1 per lane:
- **browse**: `list` jobs -- interactive, seconds;
- **transfer**: `download`/`archive` jobs -- can take hours.
So a multi-hour ZIP never blocks someone browsing a snapshot. The main
thread runs the expiry/orphan sweep and a watchdog.

Deadlines: claiming a job sets its `expires_at` to the end of its run
budget (`_run_deadline`); download/archive jobs also stop themselves
through the guard once the budget is used up (`deadline_exceeded`). A
job's run budget plus a grace period is when the sweep marks it expired
-- and when the watchdog exits the process (systemd/docker restart it),
because a thread hung inside a call can't be stopped any other way. On
start, `running` jobs a previous process left behind are failed.

A pending job's claim deadline only runs while its lane is idle: each lane
expires its own stale pending jobs (right before claiming, in the same
thread, so never one it was about to claim) and pushes the deadline of
everything still waiting forward after every job it finishes -- waiting
behind a long ZIP is not "unclaimed".

Cancel: the API marks a pending *or running* job `cancelled` (row-locked);
a running download/archive notices within seconds through the guard and
stops, and the worker never overwrites a cancelled job when it finishes.

Single-worker design: one worker process per deployment (this codebase's
current target), not a general fix for multiple concurrent workers.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_session_factory, wait_for_database
from resurgam_api.filerestore import browse, storage
from resurgam_api.filerestore.audit_detail import download_audit_detail
from resurgam_api.filerestore.browse import FileRestoreError
from resurgam_api.filerestore.sessions import hold_session, stop_session_best_effort
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.datastore import Datastore
from resurgam_api.models.filerestore_job import FileRestoreJob
from resurgam_api.models.inventory import BackupGroup, BackupSnapshot, BackupType

logger = logging.getLogger("resurgam_api.worker.file_restore")

BROWSE_JOB_TYPES: tuple[str, ...] = ("list",)
TRANSFER_JOB_TYPES: tuple[str, ...] = ("download", "archive")
ALL_JOB_TYPES = BROWSE_JOB_TYPES + TRANSFER_JOB_TYPES
# Past its run budget a job gets this much longer before the sweep expires it and the watchdog
# restarts the worker: normally the guard's own `deadline_exceeded` ends it cleanly first.
RUN_DEADLINE_GRACE_SECONDS = 15 * 60
# How often a running download/archive looks for a cancel (one indexed primary-key read).
CANCEL_CHECK_INTERVAL_SECONDS = 3.0
_CANCELLED = "cancelled"


class JobCancelled(Exception):
    """Raised by the guard when the job was cancelled while running."""


def _run_budget_seconds(settings: Settings, job_type: str) -> int:
    """How long one job may run. Listing: the job TTL (a directory listing is seconds, a cold
    restore-VM boot about a minute). Download/archive: `file_restore_extract_timeout_seconds`
    for the *whole* job -- an archive of many entries can't run for that long per entry."""
    if job_type in TRANSFER_JOB_TYPES:
        return settings.file_restore_extract_timeout_seconds
    return settings.file_restore_job_ttl_seconds


def _run_deadline(settings: Settings, job_type: str, now: datetime) -> datetime:
    return now + timedelta(seconds=_run_budget_seconds(settings, job_type) + RUN_DEADLINE_GRACE_SECONDS)


def _claim_next_job(
    db: Session, job_types: tuple[str, ...] = ALL_JOB_TYPES, settings: Settings | None = None
) -> uuid.UUID | None:
    """Claims and commits in one short transaction, releasing the row
    lock immediately -- the slow work (the actual PBS call) happens in a
    separate transaction/session, never while holding this lock. Only
    jobs of `job_types` (one lane). With `settings`, `expires_at` becomes
    the job's run deadline (see the module docstring).
    """
    job = db.execute(
        select(FileRestoreJob)
        .where(FileRestoreJob.status == "pending", FileRestoreJob.job_type.in_(job_types))
        .order_by(FileRestoreJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if job is None:
        return None
    now = datetime.now(UTC)
    job.status = "running"
    job.started_at = now
    if settings is not None:
        job.expires_at = _run_deadline(settings, job.job_type, now)
    db.commit()
    return job.id


def _expire_stale_pending(db: Session, settings: Settings, job_types: tuple[str, ...]) -> None:
    """A lane's own sweep of its pending jobs, run by the lane thread right before it claims:
    only jobs nobody could claim in time (the worker was down or the lane was hung)."""
    _expire_stale_jobs(db, settings, pending_types=job_types, sweep_running=False, sweep_files=False)


def _extend_pending(db: Session, settings: Settings, job_types: tuple[str, ...]) -> None:
    """After a lane finishes a job: whatever waited behind it gets a fresh claim deadline."""
    fresh = datetime.now(UTC) + timedelta(seconds=settings.file_restore_job_ttl_seconds)
    for job in (
        db.execute(
            select(FileRestoreJob).where(
                FileRestoreJob.status == "pending",
                FileRestoreJob.job_type.in_(job_types),
                FileRestoreJob.expires_at < fresh,
            )
        )
        .scalars()
        .all()
    ):
        job.expires_at = fresh
    db.commit()


def _fail_orphaned_running_jobs(db: Session) -> int:
    """At start-up: every `running` job belongs to a previous worker process (single-worker
    design) that died or was restarted mid-job -- fail it now instead of leaving the UI waiting
    for its deadline."""
    jobs = db.execute(select(FileRestoreJob).where(FileRestoreJob.status == "running")).scalars().all()
    now = datetime.now(UTC)
    for job in jobs:
        job.status = "failed"
        job.error_code = "worker_restarted"
        job.error_message = "The file-restore worker restarted while this job was running. Please start it again."
        job.progress = None
        job.completed_at = now
        job.result_storage_name = None
        db.add(
            AuditEvent(
                actor_user_id=job.actor_user_id,
                action="file_restore_job_failed",
                target_type="file_restore_job",
                target_id=str(job.id),
                customer_id=job.customer_id,
                correlation_id=job.correlation_id,
                detail={"reason": "worker_restarted"},
            )
        )
    db.commit()
    return len(jobs)


def _cleanup_session_for_job(db: Session, settings: Settings, job: FileRestoreJob) -> None:
    """Manifest §10.3: best-effort `stop()` of whatever PBS-side VM
    session this job's snapshot might still have open -- a no-op for
    CT/host jobs (that path never opens a session at all) and safe to
    call even when nothing is actually running. Never raises: cleanup
    failing must not fail an already-completed/expired job.
    """
    group = db.get(BackupGroup, job.backup_group_id)
    if group is None or group.backup_type != BackupType.VM.value:
        return
    snapshot = db.get(BackupSnapshot, job.backup_snapshot_id)
    datastore = db.get(Datastore, group.datastore_id)
    if snapshot is None or datastore is None:
        return
    _, token, _ = browse.repository_token_and_keyfile(db, settings, datastore)
    snapshot_id = browse.snapshot_identifier(group, snapshot)
    stop_session_best_effort(snapshot_id, token=token)


def _expire_stale_jobs(
    db: Session,
    settings: Settings,
    *,
    pending_types: tuple[str, ...] = ALL_JOB_TYPES,
    sweep_running: bool = True,
    sweep_files: bool = True,
    active_job_ids: frozenset[uuid.UUID] = frozenset(),
) -> None:
    """Sweeps `pending` (of `pending_types`) and `running` jobs past
    `expires_at` -- covering `running` is what makes the no-heartbeat
    design safe: without it, a job whose thread hung would stay `running`
    forever, and any frontend still polling it would hang indefinitely.
    The main thread passes `pending_types=()` -- pending jobs are each
    lane's own business (`_expire_stale_pending`).

    With `sweep_files`, also deletes the content file of every completed download/archive job
    past `expires_at` (its download window -- see `FileRestoreJob.expires_at`), and then anything
    in the output directory no live job owns (what a crash left behind: `.part` files, staging
    directories). `active_job_ids`: jobs a lane is working on right now (maybe already
    cancelled) -- their scratch files are not orphans.
    """
    now = datetime.now(UTC)
    conditions = []
    if pending_types:
        conditions.append((FileRestoreJob.status == "pending") & FileRestoreJob.job_type.in_(pending_types))
    if sweep_running:
        conditions.append(FileRestoreJob.status == "running")
    stale_jobs = []
    if conditions:
        stale_jobs = (
            db.execute(select(FileRestoreJob).where(or_(*conditions), FileRestoreJob.expires_at < now)).scalars().all()
        )
    for job in stale_jobs:
        job.status = "expired"
        job.progress = None
        storage.remove_job_files(settings, job.id)
        job.result_storage_name = None
        db.add(
            AuditEvent(
                actor_user_id=job.actor_user_id,
                action="file_restore_job_expired",
                target_type="file_restore_job",
                target_id=str(job.id),
                customer_id=job.customer_id,
                correlation_id=job.correlation_id,
            )
        )
        _cleanup_session_for_job(db, settings, job)
    if not sweep_files:
        if stale_jobs:
            db.commit()
        return

    expired_content = (
        db.execute(
            select(FileRestoreJob).where(
                FileRestoreJob.status == "completed",
                FileRestoreJob.result_storage_name.is_not(None),
                FileRestoreJob.expires_at < now,
            )
        )
        .scalars()
        .all()
    )
    for job in expired_content:
        storage.remove_content(settings, job.result_storage_name)
        job.result_storage_name = None
        db.add(
            AuditEvent(
                actor_user_id=job.actor_user_id,
                action="file_restore_content_deleted",
                target_type="file_restore_job",
                target_id=str(job.id),
                customer_id=job.customer_id,
                correlation_id=job.correlation_id,
                detail={"reason": "download window expired", "size_bytes": job.result_size_bytes},
            )
        )
    if stale_jobs or expired_content:
        db.commit()

    keep = set(
        db.execute(
            select(FileRestoreJob.result_storage_name).where(
                FileRestoreJob.status == "completed", FileRestoreJob.result_storage_name.is_not(None)
            )
        ).scalars()
    )
    running_ids = set(db.execute(select(FileRestoreJob.id).where(FileRestoreJob.status == "running")).scalars())
    for running_id in running_ids | set(active_job_ids):
        keep |= {f"{running_id}.part", f"{running_id}.staging"}
    removed = storage.sweep_orphans(settings, keep)
    if removed:
        logger.info("file_restore: removed %d orphaned file(s) from %s", removed, storage.output_dir(settings))


def _progress_writer(db: Session, job: FileRestoreJob):
    """Commits the job's `progress` at most every 2s -- the job row isn't locked (the claim
    committed already, ADR 0013), so intermediate commits are safe and the UI's 1.5s poll sees
    them."""
    last = [0.0]

    def write(progress: dict) -> None:
        now = time.monotonic()
        if now - last[0] < 2.0 and progress.get("entries_done") != progress.get("entries_total"):
            return
        last[0] = now
        job.progress = progress
        db.commit()

    return write


def _cancel_probe(db: Session, job_id: uuid.UUID) -> Callable[[], None]:
    """For the guard: raises `JobCancelled` once the job's row says `cancelled` (checked at most
    every few seconds, one primary-key read; the read's transaction ends right away)."""
    last = [0.0]

    def check() -> None:
        now = time.monotonic()
        if now - last[0] < CANCEL_CHECK_INTERVAL_SECONDS:
            return
        last[0] = now
        status = db.execute(select(FileRestoreJob.status).where(FileRestoreJob.id == job_id)).scalar_one_or_none()
        db.commit()
        if status == _CANCELLED:
            raise JobCancelled()

    return check


@dataclass
class _Outcome:
    status: str
    action: str
    fields: dict = field(default_factory=dict)


def _execute(db: Session, settings: Settings, job: FileRestoreJob, group, snapshot, datastore) -> _Outcome:
    """Runs the job; returns what to write -- nothing is assigned to `job` here, so the final
    status check in `_run_job` (row lock) can still refuse it when the job was cancelled."""
    deadline = time.monotonic() + _run_budget_seconds(settings, job.job_type)
    abort_check = _cancel_probe(db, job.id)
    try:
        if job.job_type == "list":
            entries = browse.list_directory(db, settings, group, snapshot, datastore, job.path)
            return _Outcome("completed", "file_restore_job_completed", {"result": [e.model_dump() for e in entries]})
        if job.job_type == "archive":
            outcome = browse.build_archive(
                db,
                settings,
                group,
                snapshot,
                datastore,
                list(job.paths or []),
                job.path,
                job_id=job.id,
                on_progress=_progress_writer(db, job),
                deadline=deadline,
                abort_check=abort_check,
            )
        else:
            outcome = browse.download_file(
                db,
                settings,
                group,
                snapshot,
                datastore,
                job.path,
                job_id=job.id,
                deadline=deadline,
                abort_check=abort_check,
            )
        return _Outcome(
            "completed",
            "file_restore_job_completed",
            {
                "result_filename": outcome.filename,
                "result_size_bytes": outcome.size_bytes,
                "result_storage_name": outcome.storage_name,
                "archive_report": outcome.report,
                # The download window starts now, not at creation: a long extraction must not
                # use it up (see FileRestoreJob.expires_at).
                "expires_at": datetime.now(UTC) + timedelta(seconds=settings.file_restore_job_ttl_seconds),
            },
        )
    except JobCancelled:
        return _Outcome(_CANCELLED, "")
    except FileRestoreError as exc:
        return _Outcome("failed", "file_restore_job_failed", {"error_code": exc.code, "error_message": exc.message})
    except Exception as exc:  # a job must never be left stuck "running" on an unexpected crash
        logger.exception("file_restore_job %s failed unexpectedly", job.id)
        db.rollback()
        return _Outcome(
            "failed", "file_restore_job_failed", {"error_code": "internal_error", "error_message": str(exc)}
        )


def _session_hold(group, snapshot):
    """VM jobs hold their snapshot's restore session while they run (see `filerestore.sessions`)."""
    if group is None or snapshot is None or group.backup_type != BackupType.VM.value:
        return nullcontext()
    return hold_session(browse.snapshot_identifier(group, snapshot))


def _run_job(db: Session, settings: Settings, job: FileRestoreJob) -> None:
    job_id = job.id
    group = db.get(BackupGroup, job.backup_group_id)
    snapshot = db.get(BackupSnapshot, job.backup_snapshot_id)
    datastore = db.get(Datastore, group.datastore_id) if group is not None else None

    try:
        with _session_hold(group, snapshot):
            outcome = _execute(db, settings, job, group, snapshot, datastore)
    finally:
        # Scratch space never outlives the job; on failure neither does partial content.
        storage.remove_path(storage.staging_path(settings, job_id))
        storage.remove_path(storage.part_path(settings, job_id))

    try:
        # Row lock: a cancel that raced the job's end wins, and is never overwritten.
        current = db.execute(
            select(FileRestoreJob.status).where(FileRestoreJob.id == job_id).with_for_update()
        ).scalar_one_or_none()
        job = db.get(FileRestoreJob, job_id)
        if current != "running" or job is None or outcome.status == _CANCELLED:
            # Cancelled (or expired by the sweep) meanwhile: drop what was produced.
            storage.remove_content(settings, outcome.fields.get("result_storage_name"))
            if job is not None:
                job.progress = None
            db.commit()
            if job is not None:
                _cleanup_session_for_job(db, settings, job)
            return
        job.status = outcome.status
        for name, value in outcome.fields.items():
            setattr(job, name, value)
        job.progress = None
        job.completed_at = datetime.now(UTC)
        db.add(
            AuditEvent(
                actor_user_id=job.actor_user_id,
                action=outcome.action,
                target_type="file_restore_job",
                target_id=str(job.id),
                customer_id=job.customer_id,
                correlation_id=job.correlation_id,
                detail=download_audit_detail(job) if job.status == "completed" else None,
            )
        )
        db.commit()
    except Exception as exc:
        # E.g. a value the column can't hold: record a plain failure instead of crashing the lane.
        logger.exception("file_restore_job %s: could not record its result", job_id)
        db.rollback()
        storage.remove_content(settings, outcome.fields.get("result_storage_name"))
        _mark_failed(db, job_id, "internal_error", f"Could not record the job's result: {exc}")
    job = db.get(FileRestoreJob, job_id)
    if job is not None:
        _cleanup_session_for_job(db, settings, job)


def _mark_failed(db: Session, job_id: uuid.UUID, code: str, message: str) -> None:
    """Last resort -- never raises."""
    try:
        job = db.get(FileRestoreJob, job_id)
        if job is not None and job.status == "running":
            job.status = "failed"
            job.error_code = code
            job.error_message = message[:2000]
            job.progress = None
            job.completed_at = datetime.now(UTC)
            db.commit()
    except Exception:
        logger.exception("file_restore_job %s: could not even mark it failed", job_id)
        db.rollback()


@dataclass
class _Lane:
    name: str
    job_types: tuple[str, ...]
    job_id: uuid.UUID | None = None
    job_type: str | None = None
    started: float = 0.0  # time.monotonic()
    lock: threading.Lock = field(default_factory=threading.Lock)

    def current(self) -> tuple[uuid.UUID | None, str | None, float]:
        with self.lock:
            return self.job_id, self.job_type, self.started

    def set(self, job_id: uuid.UUID | None, job_type: str | None) -> None:
        with self.lock:
            self.job_id, self.job_type, self.started = job_id, job_type, time.monotonic()


def _lane_step(session_factory, settings: Settings, lane: _Lane) -> bool:
    """One claim-and-run round of a lane; True when it ran a job. Never raises -- one bad job
    must never take the lane (and with it the worker) down."""
    job_id = None
    try:
        with session_factory() as db:
            _expire_stale_pending(db, settings, lane.job_types)
            job_id = _claim_next_job(db, lane.job_types, settings)
        if job_id is None:
            return False
        with session_factory() as db:
            job = db.get(FileRestoreJob, job_id)
            lane.set(job_id, job.job_type)
            try:
                _run_job(db, settings, job)
            finally:
                lane.set(None, None)
            _extend_pending(db, settings, lane.job_types)
        return True
    except Exception:
        logger.exception("file-restore %s lane: unexpected error (job %s)", lane.name, job_id)
        if job_id is not None:
            try:
                with session_factory() as db:
                    _mark_failed(db, job_id, "internal_error", "The file-restore worker hit an unexpected error.")
            except Exception:
                logger.exception("file-restore %s lane: could not mark job %s failed", lane.name, job_id)
        return job_id is not None


def _lane_loop(session_factory, settings: Settings, lane: _Lane) -> None:
    while True:
        if not _lane_step(session_factory, settings, lane):
            time.sleep(settings.file_restore_worker_poll_interval_seconds)


def _hung_lane(settings: Settings, lanes: list[_Lane]) -> _Lane | None:
    """A lane whose job ran past its budget plus the grace period -- the guard didn't stop it, so
    its thread is stuck inside a call."""
    now = time.monotonic()
    for lane in lanes:
        job_id, job_type, started = lane.current()
        budget = _run_budget_seconds(settings, job_type) if job_type else 0
        if job_id is not None and job_type is not None and now - started > budget + RUN_DEADLINE_GRACE_SECONDS:
            return lane
    return None


def run_forever() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    session_factory = get_session_factory()
    directory = storage.ensure_output_dir(settings)
    logger.info(
        "file-restore worker starting (poll interval %.1fs, job ttl %ds, output dir %s)",
        settings.file_restore_worker_poll_interval_seconds,
        settings.file_restore_job_ttl_seconds,
        directory,
    )
    wait_for_database(session_factory, logger)
    with session_factory() as db:
        failed = _fail_orphaned_running_jobs(db)
    if failed:
        logger.warning("file-restore worker: failed %d job(s) a previous worker process left running", failed)

    lanes = [_Lane("browse", BROWSE_JOB_TYPES), _Lane("transfer", TRANSFER_JOB_TYPES)]
    for lane in lanes:
        threading.Thread(
            target=_lane_loop, args=(session_factory, settings, lane), name=f"file-restore-{lane.name}", daemon=True
        ).start()

    while True:
        try:
            with session_factory() as db:
                active = frozenset(job_id for lane in lanes if (job_id := lane.current()[0]) is not None)
                _expire_stale_jobs(db, settings, pending_types=(), active_job_ids=active)
        except Exception:
            logger.exception("file-restore worker: expiry sweep failed")
        hung = _hung_lane(settings, lanes)
        if hung is not None:
            logger.critical(
                "file-restore %s lane is stuck on job %s past its deadline -- exiting so the service manager "
                "restarts the worker",
                hung.name,
                hung.current()[0],
            )
            os._exit(70)
        time.sleep(max(settings.file_restore_worker_poll_interval_seconds, 1.0))


if __name__ == "__main__":
    run_forever()
