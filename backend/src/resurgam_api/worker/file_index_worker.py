"""The out-of-process consumer for `FileIndexJob` -- manifest §13's
reopened persistent File Indexing feature (ADR 0016).

A *second*, separate privileged daemon from `file_restore_worker.py` (dev:
`sudo uv run resurgam-file-index-worker`, run from `backend/`), not a
third job type folded into that worker's own dispatch. `file_restore_
worker.run_forever()` is fully sequential -- it claims one job, runs it to
completion, and only then loops back -- which is fine for list/download
jobs (seconds), but a full-tree index job can legitimately run for hours
(VM disks: one `proxmox-file-restore list` subprocess call per directory,
no bulk mode). Sharing that loop would block every interactive file-
restore request for the duration of an index job. Both workers need root
for the same reason (VM file-restore beyond top-level listing, see
ADR 0011/0013) -- two privileged daemons is the accepted operational cost
of not building cooperative multitasking into the shipped worker.

Unlike file-restore jobs, an index job has no fixed TTL: it can be
legitimately `paused` for hours (its customer's time window closed for
most of the day) or `pending` again after a self-imposed per-run
directory-visit checkpoint (VM path). Staleness is instead a heartbeat
timeout (`_sweep_stale_index_jobs`) -- only a heartbeat that stops
advancing means the worker crashed mid-job.

`run_forever`'s periodic sweep also purges expired `ReportHistory` rows
(`_sweep_expired_report_history`) on the same cadence as file-index
retention -- unrelated to file indexing itself, but piggybacked here
for the same "no general-purpose scheduler exists" reason (ADR 0006)
rather than standing up a third privileged daemon just for it.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta, tzinfo
from datetime import time as time_of_day
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from resurgam_worker.pbs_client import catalog, file_restore
from resurgam_worker.pbs_client.errors import PathNotMountableError, PbsClientError, PbsOutputParseError
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, aliased

from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_session_factory, wait_for_database
from resurgam_api.filerestore.browse import pbs_fingerprint, repository_token_and_keyfile, snapshot_identifier
from resurgam_api.filerestore.index_entries import (
    browsable_paths,
    catalog_entries_to_rows,
    file_restore_entries_to_rows,
)
from resurgam_api.instance_settings import effective_report_retention_days
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.customer_file_index_settings import CustomerFileIndexSettings
from resurgam_api.models.datastore import Datastore, DatastoreCustomerAssignment
from resurgam_api.models.file_index_entry import FileIndexEntry, not_still_linked
from resurgam_api.models.file_index_job import FileIndexJob
from resurgam_api.models.inventory import BackupGroup, BackupSnapshot, BackupType
from resurgam_api.models.pbs import PbsInstance
from resurgam_api.models.report_history import ReportHistory
from resurgam_api.models.tenancy import Customer

logger = logging.getLogger("resurgam_api.worker.file_index")

_NON_TERMINAL_JOB_STATUSES = ("pending", "running", "paused")
_DB_RETRY_INITIAL_SECONDS = 2.0
_DB_RETRY_MAX_SECONDS = 30.0

# Scheduled snapshot choice (ADR 0016, "short-lived snapshots"): how far
# back to look at a group's backup cadence, how many of those days need
# more than one backup before the group counts as "several per day", and
# how far behind the group's newest Collector pass a snapshot may have
# been seen and still count as present on PBS.
_CADENCE_LOOKBACK_DAYS = 7
_MULTI_BACKUP_DAYS_THRESHOLD = 2
_LIVE_SNAPSHOT_SYNC_TOLERANCE = timedelta(minutes=10)


def _claim_next_index_job(db: Session) -> uuid.UUID | None:
    """`paused` is claimable exactly like `pending` -- resuming a
    window-paused or budget-checkpointed job is the same claim as
    starting a fresh one; `_run_index_job` branches on whether progress
    (`pending_paths`/`current_path`) is already present.

    Excludes `file_index_method != "external_mount"` (ADR 0021) --
    guest-exec jobs are claimed by `resurgam-lab-agent` instead, via
    `GET /lab-agent/file-index-jobs` / `POST .../claim`
    (`api/v1/lab_agent.py`), since only the agent has the host-local PVE
    access (or, for `guest_exec_ephemeral`, the map/boot machinery) they
    need -- this worker never processes them directly.
    """
    job = db.execute(
        select(FileIndexJob)
        .where(FileIndexJob.status.in_(["pending", "paused"]), FileIndexJob.file_index_method == "external_mount")
        .order_by(FileIndexJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if job is None:
        return None
    job.status = "running"
    if job.started_at is None:
        job.started_at = datetime.now(UTC)
    job.paused_at = None
    job.last_heartbeat_at = datetime.now(UTC)
    db.commit()
    return job.id


def _sweep_stale_index_jobs(db: Session, settings: Settings) -> None:
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=settings.file_index_worker_heartbeat_timeout_seconds)
    stale_jobs = (
        db.execute(
            select(FileIndexJob).where(
                FileIndexJob.status.in_(["running", "paused"]),
                FileIndexJob.last_heartbeat_at < cutoff,
            )
        )
        .scalars()
        .all()
    )
    for job in stale_jobs:
        job.status = "failed"
        job.error_code = "worker_heartbeat_timeout"
        job.error_message = "Worker heartbeat timed out -- the process likely crashed mid-run."
        job.completed_at = now
        db.add(
            AuditEvent(
                actor_user_id=job.actor_user_id,
                action="file_index_job_failed",
                target_type="file_index_job",
                target_id=str(job.id),
                correlation_id=job.correlation_id,
            )
        )
    if stale_jobs:
        db.commit()


def _snapshot_has_in_flight_job(db: Session, snapshot_id: uuid.UUID) -> bool:
    return db.query(
        exists().where(
            FileIndexJob.backup_snapshot_id == snapshot_id,
            FileIndexJob.status.in_(_NON_TERMINAL_JOB_STATUSES),
        )
    ).scalar()


def _snapshot_has_live_index(db: Session, snapshot_id: uuid.UUID) -> bool:
    return db.query(exists().where(FileIndexEntry.backup_snapshot_id == snapshot_id)).scalar()


def _snapshot_has_completed_job(db: Session, snapshot_id: uuid.UUID) -> bool:
    """A completed job that stored nothing (e.g. everything excluded)
    still means "done" -- without this the scheduler would re-queue the
    same snapshot on every run."""
    return db.query(
        exists().where(FileIndexJob.backup_snapshot_id == snapshot_id, FileIndexJob.status == "completed")
    ).scalar()


def _groups_for_customer(db: Session, customer_id: uuid.UUID) -> list[BackupGroup]:
    """The reverse of `auth.scope.effective_group_customer_id`: every
    group whose *effective* customer is this one -- either an explicit
    per-group override, or (absent an override) a group on a datastore
    assigned to this customer.
    """
    assigned_datastore_ids = [
        row.datastore_id
        for row in db.query(DatastoreCustomerAssignment.datastore_id).filter_by(customer_id=customer_id).all()
    ]
    conditions = [BackupGroup.customer_id == customer_id]
    if assigned_datastore_ids:
        conditions.append(and_(BackupGroup.customer_id.is_(None), BackupGroup.datastore_id.in_(assigned_datastore_ids)))
    # A superseded generation (a reused VMID's frozen prior identity)
    # keeps whatever file_index_enabled it had before it closed --
    # without this filter it would get endlessly re-enqueued for
    # indexing data that's no longer this workload's current state.
    return db.query(BackupGroup).filter(or_(*conditions), BackupGroup.superseded_at.is_(None)).all()


def _pbs_server_zone(db: Session, group: BackupGroup) -> tzinfo:
    """The timezone PBS prune counts days in for this group's datastore
    (`PbsInstance.server_timezone`, read by the Collector). UTC until the
    first sync has stored it, or if this host has no tz database entry
    for it -- the choice then may be off by the UTC offset, never worse
    than the pre-0.2.12 "always newest" behaviour for daily groups."""
    name = (
        db.query(PbsInstance.server_timezone)
        .join(Datastore, Datastore.pbs_instance_id == PbsInstance.id)
        .filter(Datastore.id == group.datastore_id)
        .scalar()
    )
    if not name:
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown PBS server timezone %r, using UTC for the File Indexing schedule", name)
        return UTC


def _scheduled_snapshot_for(db: Session, group: BackupGroup, now: datetime) -> BackupSnapshot | None:
    """Which snapshot of `group` a scheduled run should index.

    A group backed up once a day: its newest snapshot -- it is that day's
    only one, so PBS `keep-daily` keeps it.

    A group backed up several times a day (confirmed live 2026-11-14: a
    2-hourly VM's 03:00 snapshot was indexed at 05:00 and pruned by PBS
    before noon, taking its index with it): the newest snapshot of the
    last *finished* day, as the PBS server counts days. PBS prune keeps
    the newest snapshot of each local day (`strftime_local("%Y/%m/%d")`
    in `pbs-datastore/src/prune.rs`), so that one survives as long as
    `keep-daily` reaches back; today's snapshots can still be replaced
    as the day's last. The index is then up to one day older than the
    newest backup -- the price of it still existing tomorrow. Only
    snapshots the group's newest Collector pass still saw count, so an
    already-pruned row (RESURGAM keeps `backup_snapshots` rows, ADR 0006)
    is never picked.

    "Several per day" means at least `_MULTI_BACKUP_DAYS_THRESHOLD` of the
    last `_CADENCE_LOOKBACK_DAYS` local days hold more than one snapshot
    row, so a single extra manual backup doesn't switch a nightly group
    over. Rows of snapshots PBS has since pruned still count here --
    they record the cadence, which is exactly what's being measured.
    """
    newest = (
        db.query(BackupSnapshot)
        .filter_by(backup_group_id=group.id)
        .order_by(BackupSnapshot.snapshot_time.desc())
        .first()
    )
    if newest is None:
        return None

    zone = _pbs_server_zone(db, group)
    recent = (
        db.query(BackupSnapshot)
        .filter(
            BackupSnapshot.backup_group_id == group.id,
            BackupSnapshot.snapshot_time >= now - timedelta(days=_CADENCE_LOOKBACK_DAYS + 1),
        )
        .order_by(BackupSnapshot.snapshot_time.desc())
        .all()
    )
    snapshots_per_day = Counter(s.snapshot_time.astimezone(zone).date() for s in recent)
    busy_days = sum(1 for count in snapshots_per_day.values() if count > 1)
    if busy_days < _MULTI_BACKUP_DAYS_THRESHOLD:
        return newest

    today = now.astimezone(zone).date()
    newest_sync = max(s.last_synced_at for s in recent)
    for snapshot in recent:
        if (
            snapshot.snapshot_time.astimezone(zone).date() < today
            and snapshot.last_synced_at >= newest_sync - _LIVE_SNAPSHOT_SYNC_TOLERANCE
        ):
            return snapshot
    # Nothing from a finished day is still on PBS (e.g. a new group, or a
    # prune policy without keep-daily): nothing longer-lived to prefer.
    return newest


def _enqueue_due_scheduled_work(db: Session) -> None:
    now = datetime.now(UTC)
    due_settings = (
        db.execute(
            select(CustomerFileIndexSettings).where(
                CustomerFileIndexSettings.enabled.is_(True),
                CustomerFileIndexSettings.schedule_enabled.is_(True),
                CustomerFileIndexSettings.next_scheduled_run_at <= now,
            )
        )
        .scalars()
        .all()
    )
    if not due_settings:
        return

    for settings_row in due_settings:
        # Only groups individually opted in (manifest §13's per-VM opt-in,
        # ADR 0016) -- the customer-level settings row being due/enabled
        # is necessary but not sufficient, same two-level AND gate the API
        # enforces for manual/bulk triggers.
        due_groups = [g for g in _groups_for_customer(db, settings_row.customer_id) if g.file_index_enabled]
        for group in due_groups:
            # Only one snapshot per group, never its whole history:
            # confirmed live on a real appliance (2026-11-12) -- switching
            # indexing on for a VM with 395 existing snapshots would
            # otherwise queue 395 jobs of up to hours each, and the
            # schedule would never catch up. Older snapshots stay
            # indexable on demand (manual / bulk).
            snapshot = _scheduled_snapshot_for(db, group, now)
            if snapshot is not None:
                if (
                    _snapshot_has_live_index(db, snapshot.id)
                    or _snapshot_has_in_flight_job(db, snapshot.id)
                    or _snapshot_has_completed_job(db, snapshot.id)
                ):
                    continue
                db.add(
                    FileIndexJob(
                        status="pending",
                        trigger="scheduled",
                        backup_group_id=group.id,
                        backup_snapshot_id=snapshot.id,
                        customer_id=settings_row.customer_id,
                        actor_user_id=None,
                    )
                )
        settings_row.next_scheduled_run_at = now + timedelta(hours=settings_row.schedule_interval_hours)
    db.commit()


def _sweep_expired_retention(db: Session) -> int:
    """Manifest §13's "customer-specific retention": purges
    `file_index_entries` older than each customer's own configured
    `retention_days`, keyed off `created_at` ("indexed at"), not the
    file's own `mtime` -- a file's filesystem timestamp has nothing to do
    with how long RESURGAM should keep its index of it. This is a
    time-based heuristic independent of whether the snapshot itself is
    still on PBS -- see `collector.sync._purge_orphaned_file_index_entries`
    for the separate, sync-triggered cleanup that handles a snapshot PBS
    has actually pruned before `retention_days` would have caught it.
    """
    now = datetime.now(UTC)
    purged = 0
    for settings_row in db.query(CustomerFileIndexSettings).all():
        cutoff = now - timedelta(days=settings_row.retention_days)
        purged += (
            db.query(FileIndexEntry)
            .filter(
                FileIndexEntry.customer_id == settings_row.customer_id,
                FileIndexEntry.created_at < cutoff,
                not_still_linked(),
            )
            .delete(synchronize_session="fetch")
        )
    if purged:
        db.commit()
    return purged


def _sweep_expired_report_history(db: Session) -> int:
    """Report History library (raised directly by the user): purges
    `ReportHistory` rows (and, via `ondelete=CASCADE`, their
    `ReportHistoryContent` blob) older than each customer's own
    *effective* retention -- their own `Customer.report_retention_days`
    override, or the platform-wide floor (`InstanceSettings.
    min_report_retention_days`) if they've never set one. Piggybacks on
    this worker's existing periodic sweep cadence rather than standing
    up a third privileged daemon -- no general-purpose scheduler exists
    in this codebase (ADR 0006), the same reasoning
    `_sweep_expired_retention` above already established for file-index
    retention.
    """
    now = datetime.now(UTC)
    purged = 0
    for customer in db.query(Customer).all():
        retention_days = effective_report_retention_days(db, customer.report_retention_days)
        cutoff = now - timedelta(days=retention_days)
        purged += (
            db.query(ReportHistory)
            .filter(ReportHistory.customer_id == customer.id, ReportHistory.created_at < cutoff)
            .delete(synchronize_session="fetch")
        )
    if purged:
        db.commit()
    return purged


def _within_window(now_time: time_of_day, start: time_of_day | None, end: time_of_day | None) -> bool:
    if start is None or end is None:
        return True
    if start <= end:
        return start <= now_time <= end
    # Overnight window (e.g. 22:00-04:00): wraps past midnight rather
    # than needing two separate rows.
    return now_time >= start or now_time <= end


def _should_stop_for_cancel(db: Session, job: FileIndexJob) -> bool:
    """Best-effort cancellation, matching ADR 0013's own accepted no-
    lease/no-interrupt precedent for file-restore jobs: `cancel_requested`
    is set by the API in a separate transaction, so it's only noticed
    here, at the next checkpoint.
    """
    db.refresh(job, attribute_names=["cancel_requested"])
    if not job.cancel_requested:
        return False
    job.status = "cancelled"
    job.completed_at = datetime.now(UTC)
    db.add(
        AuditEvent(
            actor_user_id=job.actor_user_id,
            action="file_index_job_cancelled",
            target_type="file_index_job",
            target_id=str(job.id),
            correlation_id=job.correlation_id,
        )
    )
    db.commit()
    return True


_ERROR_MESSAGE_MAX_LENGTH = 4000


def _format_error_message(exc: Exception) -> str:
    """A driver-level error (e.g. a failed bulk INSERT) can stringify to
    megabytes -- its whole statement and every bound parameter -- which
    doesn't belong in a job row read back by an API response or a plain
    `SELECT *`. Truncated defensively for *any* exception, not just the
    one that first exposed this (confirmed live, ADR 0016: a single
    directory's oversized INSERT produced a 2.95MB `error_message`).
    """
    text = str(exc)
    if len(text) <= _ERROR_MESSAGE_MAX_LENGTH:
        return text
    return text[:_ERROR_MESSAGE_MAX_LENGTH] + f"... (truncated, {len(text)} chars total)"


def _compile_exclude_patterns(patterns: list[str]) -> list[re.Pattern]:
    """Invalid regex is a config-time mistake, not a reason to fail every
    index job for the VM -- skipped with a log line rather than raised.
    Case-insensitive since Windows path casing (NTFS) isn't reliable.
    """
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            logger.warning("file_index: skipping invalid exclude pattern %r", pattern)
    return compiled


def _is_excluded(path: str, compiled_patterns: list[re.Pattern]) -> bool:
    return any(pattern.search(path) for pattern in compiled_patterns)


def _link_unchanged_rows(db: Session, backup_group_id: uuid.UUID, rows: list[dict]) -> None:
    """ "Reuse and link" (manifest §13, ADR 0016), raised directly by the
    user after watching a real VM re-index 250k+ largely unchanged
    entries on every snapshot. Mutates `rows` in place: any row whose
    (type, size_bytes, mtime) exactly matches the entry at the same path
    belonging to this group's chronologically most recent *other*
    snapshot gets `linked_from_entry_id` set (always to a *root* entry --
    never chained through another link) and its own `size_bytes`/`mtime`
    dropped, instead of duplicating them.

    Ordered by `BackupSnapshot.snapshot_time`, not `FileIndexEntry.
    created_at` -- the latter is *when a row was indexed*, not which
    snapshot it belongs to, and a scheduled/paused/backfilled job can
    easily index an older snapshot later than a newer one. Confirmed live
    in this codebase's own tests: `created_at` uses Postgres's
    transaction-time `now()`, so entries written in the same transaction
    can tie, making an ordering that relies on it non-deterministic on
    top of being conceptually the wrong key.
    """
    if not rows:
        return
    paths = [row["path"] for row in rows]
    root_alias = aliased(FileIndexEntry)
    # A candidate that's itself a linked row has NULL size_bytes/mtime by
    # design -- comparing against those raw columns would always mismatch
    # a genuinely-unchanged file, silently defeating linking for every
    # generation past the second. The outer join resolves each
    # candidate's *effective* values through its own link (one hop, same
    # as everywhere else this invariant holds) for the comparison below.
    candidates = (
        db.query(FileIndexEntry, root_alias)
        .join(BackupSnapshot, FileIndexEntry.backup_snapshot_id == BackupSnapshot.id)
        .outerjoin(root_alias, FileIndexEntry.linked_from_entry_id == root_alias.id)
        .filter(FileIndexEntry.backup_group_id == backup_group_id, FileIndexEntry.path.in_(paths))
        .order_by(FileIndexEntry.path, BackupSnapshot.snapshot_time.desc(), FileIndexEntry.created_at.desc())
        .all()
    )
    latest_by_path: dict[str, tuple[FileIndexEntry, FileIndexEntry | None]] = {}
    for entry, root in candidates:
        if entry.path not in latest_by_path:
            latest_by_path[entry.path] = (entry, root)  # first row per path wins -- already ordered newest-first

    for row in rows:
        found = latest_by_path.get(row["path"])
        if found is None:
            continue
        existing, root = found
        effective_size = root.size_bytes if root is not None else existing.size_bytes
        effective_mtime = root.mtime if root is not None else existing.mtime
        if (existing.type, effective_size, effective_mtime) != (row["type"], row["size_bytes"], row["mtime"]):
            continue
        row["linked_from_entry_id"] = existing.linked_from_entry_id or existing.id
        row["size_bytes"] = None
        row["mtime"] = None


def _insert_entries(db: Session, rows: list[dict]) -> None:
    if not rows:
        return
    # `ON CONFLICT DO NOTHING` on (file_index_job_id, path) is what makes
    # CT/host's "resume by re-fetching the whole cheap dump_catalog()
    # call" safe: rows already written before a previous pause are
    # silently skipped rather than duplicated or erroring.
    stmt = pg_insert(FileIndexEntry).values(rows).on_conflict_do_nothing(index_elements=["file_index_job_id", "path"])
    db.execute(stmt)


def _complete_job(db: Session, job: FileIndexJob) -> None:
    job.status = "completed"
    job.completed_at = datetime.now(UTC)
    job.current_path = None
    job.pending_paths = None
    db.add(
        AuditEvent(
            actor_user_id=job.actor_user_id,
            action="file_index_job_completed",
            target_type="file_index_job",
            target_id=str(job.id),
            correlation_id=job.correlation_id,
        )
    )
    db.commit()


def _run_ct_host_index_job(
    db: Session,
    settings: Settings,
    job: FileIndexJob,
    group: BackupGroup,
    snapshot: BackupSnapshot,
    datastore: Datastore,
    index_settings: CustomerFileIndexSettings | None,
) -> None:
    """One `catalog.dump_catalog()` call (confirmed live: ~63k entries,
    0.55s against a real PBS 4.2 instance) -- cheap enough that the call
    itself isn't checkpointed, only the batched insert afterward.
    """
    repository, token, keyfile = repository_token_and_keyfile(db, settings, datastore)
    snapshot_id = snapshot_identifier(group, snapshot)
    entries = catalog.dump_catalog(
        repository,
        snapshot_id,
        namespace=group.namespace,
        token=token,
        keyfile=keyfile,
        fingerprint=pbs_fingerprint(db, datastore),
    )
    if group.file_index_exclude_patterns:
        compiled = _compile_exclude_patterns(group.file_index_exclude_patterns)
        entries = [e for e in entries if not _is_excluded(e.path, compiled)]
    rows = catalog_entries_to_rows(
        entries,
        file_index_job_id=job.id,
        backup_snapshot_id=snapshot.id,
        backup_group_id=group.id,
        customer_id=job.customer_id,
    )

    batch_size = settings.file_index_entries_insert_batch_size
    for offset in range(0, len(rows), batch_size):
        if _should_stop_for_cancel(db, job):
            return
        now = datetime.now(UTC)
        if index_settings is not None and not _within_window(
            now.time(), index_settings.window_start_time, index_settings.window_end_time
        ):
            job.status = "paused"
            job.paused_at = now
            job.last_heartbeat_at = now
            db.commit()
            return

        batch = rows[offset : offset + batch_size]
        _link_unchanged_rows(db, group.id, batch)
        _insert_entries(db, batch)
        job.entries_indexed += len(batch)
        job.last_heartbeat_at = datetime.now(UTC)
        db.commit()

    _complete_job(db, job)


def _run_vm_index_job(
    db: Session,
    settings: Settings,
    job: FileIndexJob,
    group: BackupGroup,
    snapshot: BackupSnapshot,
    datastore: Datastore,
    index_settings: CustomerFileIndexSettings | None,
) -> None:
    """BFS over directories via `file_restore.list_path()` -- one
    subprocess call per directory, unlike CT/host's single bulk call.
    `pending_paths` is the not-yet-visited frontier, persisted after every
    directory so a pause/budget-checkpoint can resume exactly where it
    left off rather than re-walking from "/".
    """
    repository, token, keyfile = repository_token_and_keyfile(db, settings, datastore)
    fingerprint = pbs_fingerprint(db, datastore)
    snapshot_id = snapshot_identifier(group, snapshot)
    compiled_excludes = _compile_exclude_patterns(group.file_index_exclude_patterns)

    pending_paths: list[str] = list(job.pending_paths) if job.pending_paths else ["/"]
    visited_this_run = 0
    budget = settings.file_index_worker_vm_max_directories_per_job_run

    while pending_paths:
        if _should_stop_for_cancel(db, job):
            return

        now = datetime.now(UTC)
        if index_settings is not None and not _within_window(
            now.time(), index_settings.window_start_time, index_settings.window_end_time
        ):
            job.pending_paths = pending_paths
            job.status = "paused"
            job.paused_at = now
            job.last_heartbeat_at = now
            db.commit()
            return

        if visited_this_run >= budget:
            # Self-imposed checkpoint, not a window pause -- immediately
            # re-claimable, not waiting on anything external.
            job.pending_paths = pending_paths
            job.status = "pending"
            job.last_heartbeat_at = now
            db.commit()
            return

        path = pending_paths.pop(0)
        job.current_path = path
        try:
            entries = file_restore.list_path(
                repository,
                snapshot_id,
                path,
                namespace=group.namespace,
                token=token,
                keyfile=keyfile,
                fingerprint=fingerprint,
            )
        except PathNotMountableError as exc:
            # A partition PBS can't mount (e.g. an EFI/recovery partition
            # with no supported filesystem) is a normal, expected outcome
            # for a real Windows VM disk, not a job failure -- skip this
            # one path and keep walking the rest of the frontier.
            logger.info("file_index_job %s: skipping unmountable path %s (%s)", job.id, path, exc.pbs_message)
            job.pending_paths = pending_paths
            job.last_heartbeat_at = datetime.now(UTC)
            db.commit()
            visited_this_run += 1
            time.sleep(settings.file_index_worker_vm_call_delay_seconds)
            continue
        except PbsOutputParseError as exc:
            # Confirmed live (2026-09-20) against a real Windows VM: some
            # single path made `proxmox-file-restore list` return a JSON
            # shape neither `list_path` nor `PathNotMountableError`
            # expects (a bare string, not an entry array or the
            # documented `{"code",...}` not-mountable shape) -- a real
            # PBS/path-specific oddity, not a sign the whole job is
            # unusable. Previously this propagated up to `_run_index_job`
            # and failed the entire job outright, discarding tens of
            # thousands of already-indexed entries' worth of remaining
            # progress over one bad path. Logged at warning (louder than
            # the routine unmountable-partition skip above, since a
            # genuinely malformed response is worth an operator noticing)
            # but still skip-and-continue, not job-fatal.
            logger.warning("file_index_job %s: skipping unparseable path %s (%s)", job.id, path, exc)
            job.pending_paths = pending_paths
            job.last_heartbeat_at = datetime.now(UTC)
            db.commit()
            visited_this_run += 1
            time.sleep(settings.file_index_worker_vm_call_delay_seconds)
            continue
        if compiled_excludes:
            entries = [e for e in entries if not _is_excluded(e.path, compiled_excludes)]
        rows = file_restore_entries_to_rows(
            entries,
            file_index_job_id=job.id,
            backup_snapshot_id=snapshot.id,
            backup_group_id=group.id,
            customer_id=job.customer_id,
        )
        # A single real directory can hold thousands of files (confirmed
        # live: a VM's flat package-doc directory alone exceeded 5900+
        # entries) -- chunked the same way as the CT/host bulk path,
        # since one un-chunked INSERT ... VALUES (...), (...), ... can
        # blow past Postgres's 65535-bound-parameters-per-statement limit.
        batch_size = settings.file_index_entries_insert_batch_size
        for offset in range(0, len(rows), batch_size):
            batch = rows[offset : offset + batch_size]
            _link_unchanged_rows(db, group.id, batch)
            _insert_entries(db, batch)
            job.entries_indexed += len(batch)
        pending_paths.extend(browsable_paths(entries))
        job.pending_paths = pending_paths
        job.last_heartbeat_at = datetime.now(UTC)
        db.commit()

        visited_this_run += 1
        # The concrete I/O-limit throttle manifest §13 asks for on the
        # expensive (one-directory-per-call) VM path.
        time.sleep(settings.file_index_worker_vm_call_delay_seconds)

    _complete_job(db, job)


def _run_index_job(db: Session, settings: Settings, job: FileIndexJob) -> None:
    group = db.get(BackupGroup, job.backup_group_id)
    snapshot = db.get(BackupSnapshot, job.backup_snapshot_id)
    datastore = db.get(Datastore, group.datastore_id)
    index_settings = (
        db.query(CustomerFileIndexSettings).filter_by(customer_id=job.customer_id).one_or_none()
        if job.customer_id is not None
        else None
    )

    # Checked once up front (in addition to every in-loop checkpoint)
    # so the window applies uniformly to *every* trigger type: even a
    # fresh manual/bulk job never gets past this if the window is
    # currently closed.
    now = datetime.now(UTC)
    if index_settings is not None and not _within_window(
        now.time(), index_settings.window_start_time, index_settings.window_end_time
    ):
        job.status = "paused"
        job.paused_at = now
        job.last_heartbeat_at = now
        db.commit()
        return

    try:
        if group.backup_type == BackupType.VM.value:
            _run_vm_index_job(db, settings, job, group, snapshot, datastore, index_settings)
        else:
            _run_ct_host_index_job(db, settings, job, group, snapshot, datastore, index_settings)
    except PbsClientError as exc:
        job.status = "failed"
        job.error_code = "pbs_error"
        job.error_message = _format_error_message(exc)
        job.completed_at = datetime.now(UTC)
        db.add(
            AuditEvent(
                actor_user_id=job.actor_user_id,
                action="file_index_job_failed",
                target_type="file_index_job",
                target_id=str(job.id),
                correlation_id=job.correlation_id,
            )
        )
        db.commit()
    except Exception as exc:  # a job must never be left stuck "running" on an unexpected crash
        job.status = "failed"
        job.error_code = "internal_error"
        job.error_message = _format_error_message(exc)
        job.completed_at = datetime.now(UTC)
        logger.exception("file_index_job %s failed unexpectedly", job.id)
        db.add(
            AuditEvent(
                actor_user_id=job.actor_user_id,
                action="file_index_job_failed",
                target_type="file_index_job",
                target_id=str(job.id),
                correlation_id=job.correlation_id,
            )
        )
        db.commit()


def run_forever() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    session_factory = get_session_factory()
    logger.info(
        "file-index worker starting (poll interval %.1fs, schedule check every %.0fs)",
        settings.file_index_worker_poll_interval_seconds,
        settings.file_index_worker_schedule_check_interval_seconds,
    )

    wait_for_database(session_factory, logger)
    last_schedule_check = 0.0
    last_retention_check = 0.0

    db_retry_delay = _DB_RETRY_INITIAL_SECONDS
    while True:
        try:
            with session_factory() as db:
                _sweep_stale_index_jobs(db, settings)
                now_monotonic = time.monotonic()
                if now_monotonic - last_schedule_check >= settings.file_index_worker_schedule_check_interval_seconds:
                    _enqueue_due_scheduled_work(db)
                    last_schedule_check = now_monotonic
                if now_monotonic - last_retention_check >= settings.file_index_worker_retention_check_interval_seconds:
                    _sweep_expired_retention(db)
                    _sweep_expired_report_history(db)
                    last_retention_check = now_monotonic
                job_id = _claim_next_index_job(db)
        except OperationalError as exc:
            # On the appliance the worker is native but Postgres runs in
            # Docker, so at boot the worker can be up before the database
            # (confirmed live: one crash + systemd restart per boot).
            # Wait and retry instead of exiting.
            logger.warning("file-index worker: database not reachable, retrying in %.0fs (%s)", db_retry_delay, exc)
            time.sleep(db_retry_delay)
            db_retry_delay = min(db_retry_delay * 2, _DB_RETRY_MAX_SECONDS)
            continue
        db_retry_delay = _DB_RETRY_INITIAL_SECONDS

        if job_id is None:
            time.sleep(settings.file_index_worker_poll_interval_seconds)
            continue

        with session_factory() as db:
            job = db.get(FileIndexJob, job_id)
            _run_index_job(db, settings, job)


if __name__ == "__main__":
    run_forever()
