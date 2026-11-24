import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from resurgam_api.config import Settings
from resurgam_api.connectivity.models import RemoteDatastoreUsage, RemoteSnapshot, TrustConfig
from resurgam_api.connectivity.pbs_api import (
    get_group_notes,
    get_server_timezone,
    list_acl_entries,
    list_datastore_usage,
    list_datastores,
    list_snapshots,
    list_sync_jobs,
    list_tasks,
)
from resurgam_api.connectivity.pve_api import list_guests
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.datastore import Datastore
from resurgam_api.models.file_index_entry import FileIndexEntry, not_still_linked
from resurgam_api.models.inventory import (
    BackupGroup,
    BackupSnapshot,
    PbsAclEntry,
    TaskRun,
    TaskStatus,
    TaskType,
    VerifyStatus,
)
from resurgam_api.models.pbs import PbsInstance, TlsTrustMode
from resurgam_api.models.pve import PveCluster, PveClusterDatastore
from resurgam_api.models.secrets import SecretReference
from resurgam_api.models.sync import SyncJob
from resurgam_api.models.usage import UsageMeasurement
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

# PBS reports a different `worker_type` for a manually-triggered run versus
# its scheduled-job counterpart ("prune" vs "prunejob") — confirmed against
# a real PBS 4.2 instance, which had never surfaced in fixture-only testing.
_TASK_TYPE_MAP = {
    "backup": TaskType.BACKUP.value,
    "backupjob": TaskType.BACKUP.value,
    "verify": TaskType.VERIFY.value,
    "verificationjob": TaskType.VERIFY.value,
    "prune": TaskType.PRUNE.value,
    "prunejob": TaskType.PRUNE.value,
    "garbage_collection": TaskType.GARBAGE_COLLECTION.value,
    "sync": TaskType.SYNC.value,
    "syncjob": TaskType.SYNC.value,
    # A Sync Job's actual pull-from-remote task is PBS's own "reader"
    # worker type, distinct from "sync"/"syncjob" -- confirmed live
    # against real data (2026-09-28): every "reader" UPID in this
    # deployment's history was a Sync Job pull, and without this entry
    # they fell through to UNKNOWN, silently absent from both the
    # Overview's recent-activity panel and the Replication & Sync jobs
    # page (`JobsReplicationSyncPage.tsx` filters on `taskTypes: ["sync"]`
    # only, so a "reader" task never showed there either).
    "reader": TaskType.SYNC.value,
}


@dataclass
class InventorySyncResult:
    reachable: bool
    groups_created: int = 0
    groups_updated: int = 0
    groups_removed: int = 0
    snapshots_created: int = 0
    snapshots_updated: int = 0
    usage_captured: bool = False
    file_index_entries_purged: int = 0
    error: str | None = None


@dataclass
class TaskSyncResult:
    reachable: bool
    tasks_created: int = 0
    tasks_updated: int = 0
    error: str | None = None


@dataclass
class SyncJobSyncResult:
    reachable: bool
    sync_jobs_created: int = 0
    sync_jobs_updated: int = 0
    sync_jobs_removed: int = 0
    error: str | None = None


@dataclass
class DatastoreDiscoveryResult:
    """Outcome of `discover_datastores` -- datastore *names* per bucket, so
    callers (sync endpoint, instance-create flow, logs) can say exactly
    what changed rather than just how much."""

    reachable: bool
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class AclSyncResult:
    reachable: bool
    entries_synced: int = 0
    error: str | None = None


@dataclass
class DatastoreSyncOutcome:
    datastore_id: uuid.UUID
    datastore_name: str
    result: InventorySyncResult


@dataclass
class PbsInstanceSyncResult:
    tasks: TaskSyncResult
    sync_jobs: SyncJobSyncResult
    acl: AclSyncResult
    datastores: list[DatastoreSyncOutcome] = field(default_factory=list)
    discovery: DatastoreDiscoveryResult | None = None


def _verify_status_from_state(state: str | None) -> str:
    if state is None:
        return VerifyStatus.NONE.value
    if state.lower() == "ok":
        return VerifyStatus.OK.value
    if state.lower() in ("failed", "error"):
        return VerifyStatus.FAILED.value
    return VerifyStatus.UNKNOWN.value


def _task_status_from_remote(status: str | None, end_time: int | None) -> str:
    if end_time is None:
        return TaskStatus.RUNNING.value
    if status is None:
        return TaskStatus.UNKNOWN.value
    return TaskStatus.OK.value if status.upper() == "OK" else TaskStatus.ERROR.value


def _get_token(db: Session, settings: Settings, secret_ref_id: uuid.UUID) -> str:
    secret_ref = db.get(SecretReference, secret_ref_id)
    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    return provider.retrieve(secret_ref.reference_key)


def _trust_for(instance: PbsInstance) -> TrustConfig:
    return TrustConfig(
        trust_mode=TlsTrustMode(instance.tls_trust_mode), fingerprint_sha256=instance.tls_fingerprint_sha256
    )


# PBS's own `backup_type` ("vm"/"ct") versus PVE's `guest_type` ("qemu"/
# "lxc") for the same guest -- confirmed against a real PBS 4.2 + PVE 9.2
# pair (2026-09-16/20).
_PVE_GUEST_TYPE_TO_BACKUP_TYPE = {"qemu": "vm", "lxc": "ct"}


def _pve_guest_name_lookup(db: Session, settings: Settings, datastore: Datastore) -> dict[tuple[str, str], str]:
    """Best-effort `{(backup_type, backup_id): guest_name}` lookup, built
    from every PVE cluster explicitly linked to *this datastore*
    (`PveClusterDatastore`) for name-resolution provenance -- used only as
    a fallback when a backup group's PBS notes are empty (see
    `sync_datastore_inventory`).

    Deliberately scoped to the datastore's own linked cluster(s), not "any
    cluster paired with this PBS instance" (`PveClusterPbsInstance`, a
    restore-capability link) -- a VMID is only unique *within* one PVE
    cluster (ADR 0009), and one PBS instance can back datastores for many
    different tenants, so resolving via the instance-level link risks
    matching the wrong guest across tenants.
    """
    cluster_ids = [
        row[0] for row in db.query(PveClusterDatastore.pve_cluster_id).filter_by(datastore_id=datastore.id).all()
    ]
    if not cluster_ids:
        return {}

    lookup: dict[tuple[str, str], str] = {}
    clusters = db.query(PveCluster).filter(PveCluster.id.in_(cluster_ids), PveCluster.disabled.is_(False)).all()
    for cluster in clusters:
        token = _get_token(db, settings, cluster.api_token_secret_ref_id)
        trust = TrustConfig(
            trust_mode=TlsTrustMode(cluster.tls_trust_mode), fingerprint_sha256=cluster.tls_fingerprint_sha256
        )
        result = list_guests(cluster.host, cluster.port, trust, token, timeout=settings.connectivity_timeout_seconds)
        if not result.reachable or not result.guests:
            continue
        for guest in result.guests:
            if not guest.name:
                continue
            backup_type = _PVE_GUEST_TYPE_TO_BACKUP_TYPE.get(guest.guest_type)
            if backup_type is None:
                continue
            lookup[(backup_type, str(guest.vmid))] = guest.name
    return lookup


def _capture_usage_measurement(
    db: Session, instance: PbsInstance, datastore: Datastore, remote_usage: RemoteDatastoreUsage | None
) -> bool:
    """Insert one append-only `usage_measurements` row (manifest §9.3),
    or do nothing if PBS's usage listing didn't include this datastore
    (unreachable, or renamed since import — non-fatal, just skipped).
    Group/snapshot counts, `logical_bytes`, and the last GC time come from
    RESURGAM's own already-synced tables, not a second PBS call —
    `logical_bytes` inherits the same pruning caveat as everything else
    computed from `backup_snapshots`: a snapshot pruned on PBS since the
    last sync still counts here until the next sync notices it's gone
    (see the Collector's own module docstring / ADR 0006).
    """
    if remote_usage is None:
        return False

    type_counts = dict(
        db.query(BackupGroup.backup_type, func.count(BackupGroup.id))
        .filter_by(datastore_id=datastore.id)
        .group_by(BackupGroup.backup_type)
        .all()
    )
    snapshot_count, oldest, newest, logical_bytes = (
        db.query(
            func.count(BackupSnapshot.id),
            func.min(BackupSnapshot.snapshot_time),
            func.max(BackupSnapshot.snapshot_time),
            func.coalesce(func.sum(BackupSnapshot.size_bytes), 0),
        )
        .join(BackupGroup, BackupSnapshot.backup_group_id == BackupGroup.id)
        .filter(BackupGroup.datastore_id == datastore.id)
        .one()
    )
    last_gc = (
        db.query(func.max(TaskRun.ended_at))
        .filter(
            TaskRun.pbs_instance_id == instance.id,
            TaskRun.task_type == TaskType.GARBAGE_COLLECTION.value,
            TaskRun.status == TaskStatus.OK.value,
            TaskRun.worker_id == datastore.name,
        )
        .scalar()
    )

    db.add(
        UsageMeasurement(
            datastore_id=datastore.id,
            used_bytes=remote_usage.used_bytes,
            available_bytes=remote_usage.available_bytes,
            total_bytes=remote_usage.total_bytes,
            logical_bytes=logical_bytes,
            group_count_vm=type_counts.get("vm", 0),
            group_count_ct=type_counts.get("ct", 0),
            group_count_host=type_counts.get("host", 0),
            snapshot_count=snapshot_count or 0,
            oldest_snapshot_at=oldest,
            newest_snapshot_at=newest,
            last_successful_gc_at=last_gc,
        )
    )
    db.flush()
    return True


def _purge_orphaned_file_index_entries(
    db: Session, datastore: Datastore, grouped: dict[tuple[str, str, str], list[RemoteSnapshot]]
) -> int:
    """File Indexing correctness fix (manifest §13), not general prune-
    detection -- `sync_datastore_inventory`'s own "no prune-detection in
    this slice" gap for `backup_snapshots` itself is deliberately left
    exactly as-is (ADR 0006 territory). A `file_index_entries` row
    pointing at a snapshot PBS no longer reports is a sharper problem
    than an ordinary stale inventory row, though: it actively invites a
    restore attempt against data that no longer exists, rather than just
    showing outdated metadata. `grouped` is this sync's already-fetched,
    authoritative PBS snapshot list for the *whole* datastore, so any
    `backup_snapshots` row under it that isn't in `grouped` -- including
    every snapshot of a backup group PBS no longer reports at all -- is
    safely known to be gone from PBS right now, whether or not RESURGAM's
    own `backup_snapshots` row for it still exists.

    `not_still_linked()` (manifest §13's "reuse and link", ADR 0016) can
    keep a handful of this snapshot's own rows alive past this purge --
    the ones a *newer, still-live* snapshot's entries depend on for their
    `size_bytes`/`mtime`. Accepted tradeoff: browsing/searching this now-
    gone snapshot could still surface one of those rows, but attempting
    to actually download it goes through the existing on-demand
    file-restore queue, which fails cleanly against a snapshot PBS no
    longer has -- not a silent restore-from-nothing risk, just a stale
    search result until the newer snapshot's own retention eventually
    lets this row go too.
    """
    live_snapshot_times: dict[tuple[str, str, str], set[datetime]] = {
        key: {datetime.fromtimestamp(snap.backup_time, tz=UTC) for snap in snaps} for key, snaps in grouped.items()
    }

    purged = 0
    for group in db.query(BackupGroup).filter_by(datastore_id=datastore.id).all():
        live_times = live_snapshot_times.get((group.namespace, group.backup_type, group.backup_id), set())
        for snapshot in db.query(BackupSnapshot).filter_by(backup_group_id=group.id).all():
            if snapshot.snapshot_time in live_times:
                continue
            purged += (
                db.query(FileIndexEntry)
                .filter(FileIndexEntry.backup_snapshot_id == snapshot.id, not_still_linked())
                .delete(synchronize_session="fetch")
            )
    return purged


def sync_datastore_inventory(
    db: Session, settings: Settings, datastore: Datastore, remote_usage: RemoteDatastoreUsage | None = None
) -> InventorySyncResult:
    """Sync backup groups and snapshots for one imported datastore.

    Idempotent: re-running with identical PBS data creates no duplicate
    rows, only refreshing `last_synced_at` and any changed fields. Does not
    delete `backup_snapshots` rows for snapshots no longer present on PBS
    — no general prune-detection in this slice (see docs/adr/0006). It
    *does* purge any `file_index_entries` (manifest §13) belonging to a
    snapshot PBS no longer reports (`_purge_orphaned_file_index_entries`)
    — a narrower, File-Indexing-specific fix, since stale search results
    pointing at data PBS has already pruned are a sharper problem than an
    ordinary stale inventory row. `remote_usage`, when given, is used to
    also capture one `usage_measurements` row after the inventory sync
    above so its group/snapshot counts reflect the just-synced data.

    A reused VMID (a new snapshot's own comment differs from the group's
    already-established `resolved_name`) splits into a new `BackupGroup`
    generation rather than mutating the existing one in place -- see
    `BackupGroup`'s own docstring for the full rationale. Snapshots
    already recorded under a closed generation are never reclassified.
    """
    instance = db.get(PbsInstance, datastore.pbs_instance_id)
    token = _get_token(db, settings, instance.api_token_secret_ref_id)
    trust = _trust_for(instance)

    result = list_snapshots(
        instance.host, instance.port, trust, token, datastore.name, timeout=settings.connectivity_timeout_seconds
    )
    if not result.reachable:
        return InventorySyncResult(reachable=False, error=result.error)

    groups_created = groups_updated = snapshots_created = snapshots_updated = 0
    pve_guest_names = _pve_guest_name_lookup(db, settings, datastore)

    grouped: dict[tuple[str, str, str], list[RemoteSnapshot]] = {}
    for snap in result.snapshots:
        grouped.setdefault((snap.namespace, snap.backup_type, snap.backup_id), []).append(snap)

    now = datetime.now(UTC)

    for (namespace, backup_type, backup_id), snaps in grouped.items():
        group = (
            db.query(BackupGroup)
            .filter_by(
                datastore_id=datastore.id,
                namespace=namespace,
                backup_type=backup_type,
                backup_id=backup_id,
                superseded_at=None,
            )
            .one_or_none()
        )

        original_group = group

        if group is not None and group.removed_from_pbs_at is not None:
            # PBS is reporting this identity tuple again -- undeleted, or
            # a fresh backup landed at the same slot. Nothing about its
            # identity changed (that's the rename-detection branch
            # below's job), so this just clears the flag rather than
            # opening a new generation.
            group.removed_from_pbs_at = None

        # Every snapshot already recorded under *any* generation of this
        # identity tuple -- a closed generation's own rows must never be
        # touched or reclassified by this sync pass, only read to know
        # they're already accounted for.
        sibling_group_ids = [
            row[0]
            for row in db.query(BackupGroup.id)
            .filter_by(datastore_id=datastore.id, namespace=namespace, backup_type=backup_type, backup_id=backup_id)
            .all()
        ]
        existing_by_time = (
            {
                s.snapshot_time: s
                for s in db.query(BackupSnapshot).filter(BackupSnapshot.backup_group_id.in_(sibling_group_ids)).all()
            }
            if sibling_group_ids
            else {}
        )

        is_new_group = group is None
        if is_new_group:
            groups_created += 1

        # Chronological order is what makes "detect a rename, open a new
        # generation, keep going" correct within a single sync pass --
        # including multiple renames in one batch -- without needing to
        # reclassify anything after the fact.
        for snap in sorted(snaps, key=lambda s: s.backup_time):
            snapshot_time = datetime.fromtimestamp(snap.backup_time, tz=UTC)
            verify_status = _verify_status_from_state(snap.verify_state)
            existing = existing_by_time.get(snapshot_time)

            if existing is not None:
                if group is not None and existing.backup_group_id == group.id:
                    existing.verify_status = verify_status
                    existing.size_bytes = snap.size
                    existing.crypt_mode = snap.crypt_mode
                    existing.key_fingerprint = snap.key_fingerprint
                    existing.last_synced_at = now
                    group.last_backup_at = snapshot_time
                    group.last_verify_status = verify_status
                    group.owner = snap.owner
                    group.last_synced_at = now
                    snapshots_updated += 1
                # else: belongs to an already-closed generation -- leave
                # it exactly as it is, no reclassification.
                continue

            if group is None:
                # First-ever generation for this identity tuple -- a
                # first name assignment is never treated as a rename.
                group = BackupGroup(
                    datastore_id=datastore.id,
                    namespace=namespace,
                    backup_type=backup_type,
                    backup_id=backup_id,
                )
                db.add(group)
                db.flush()
            elif snap.comment and group.resolved_name and snap.comment != group.resolved_name:
                # Rename detected: this VMID slot's guest identity
                # changed between snapshots -- close this generation and
                # open a fresh one rather than blending two unrelated
                # VMs' history. The new generation starts blank (no
                # policy, no customer override, file indexing off) --
                # never inherits the old generation's settings.
                group.superseded_at = now
                groups_updated += 1
                group = BackupGroup(
                    datastore_id=datastore.id,
                    namespace=namespace,
                    backup_type=backup_type,
                    backup_id=backup_id,
                    resolved_name=snap.comment,
                )
                db.add(group)
                db.flush()
                groups_created += 1

            if snap.comment:
                # Primary source, and by far the most reliable in
                # practice (confirmed live: 100% of a real customer's
                # snapshots carry this, vs. group-notes below, which is
                # almost always empty). This is where a PVE vzdump job's
                # `{{guestname}}` notes-template result actually lands --
                # *not* group-notes, a separate, rarely-used field.
                group.resolved_name = snap.comment
            elif not group.resolved_name:
                notes_result = get_group_notes(
                    instance.host,
                    instance.port,
                    trust,
                    token,
                    datastore.name,
                    backup_type,
                    backup_id,
                    namespace=namespace,
                    timeout=settings.connectivity_timeout_seconds,
                )
                if notes_result.reachable and notes_result.notes:
                    group.resolved_name = notes_result.notes
                else:
                    # Neither snapshot comment nor group notes gave a
                    # name -- fall back to a real guest name from a PVE
                    # cluster explicitly linked to this datastore, if one
                    # resolves. Never overwrites a name already provided
                    # by either source above.
                    fallback_name = pve_guest_names.get((backup_type, backup_id))
                    if fallback_name:
                        group.resolved_name = fallback_name

            db.add(
                BackupSnapshot(
                    backup_group_id=group.id,
                    snapshot_time=snapshot_time,
                    verify_status=verify_status,
                    size_bytes=snap.size,
                    crypt_mode=snap.crypt_mode,
                    key_fingerprint=snap.key_fingerprint,
                )
            )
            group.last_backup_at = snapshot_time
            group.last_verify_status = verify_status
            group.owner = snap.owner
            group.last_synced_at = now
            snapshots_created += 1

        if not is_new_group and group is original_group:
            # An existing group had snapshot fields refreshed this pass,
            # but never split -- counted once here, matching the old
            # per-identity-tuple counting. A split already counted its
            # own +1 created / +1 updated at the point it happened, so
            # this must not fire again for the generation left open by a
            # split (`group is not original_group` in that case).
            groups_updated += 1

    # Anything still `superseded_at IS NULL` for this datastore that PBS
    # didn't report at all this pass is gone -- raised directly by the
    # user after a whole namespace deleted on PBS kept showing as live in
    # RESURGAM for hours (the Collector was purely additive, never
    # noticing removal). History stays intact; this only stops it from
    # appearing as current.
    groups_removed = 0
    still_live_groups = (
        db.query(BackupGroup).filter_by(datastore_id=datastore.id, superseded_at=None, removed_from_pbs_at=None).all()
    )
    for candidate in still_live_groups:
        if (candidate.namespace, candidate.backup_type, candidate.backup_id) not in grouped:
            candidate.removed_from_pbs_at = now
            groups_removed += 1

    file_index_entries_purged = _purge_orphaned_file_index_entries(db, datastore, grouped)

    db.flush()
    usage_captured = _capture_usage_measurement(db, instance, datastore, remote_usage)
    return InventorySyncResult(
        reachable=True,
        groups_created=groups_created,
        groups_updated=groups_updated,
        groups_removed=groups_removed,
        snapshots_created=snapshots_created,
        snapshots_updated=snapshots_updated,
        usage_captured=usage_captured,
        file_index_entries_purged=file_index_entries_purged,
    )


_COLLECTOR_ACTOR_LABEL = "Collector (automatic)"


def _record_system_audit(db: Session, *, action: str, datastore: Datastore, detail: dict) -> None:
    """Audit trail for a change the Collector made on its own -- no user
    is behind it, so `actor_label` names the Collector instead (the
    `before_insert` listener leaves an explicit label alone).
    """
    db.add(
        AuditEvent(
            actor_user_id=None,
            actor_label=_COLLECTOR_ACTOR_LABEL,
            action=action,
            target_type="datastore",
            target_id=str(datastore.id),
            detail=detail,
        )
    )


def discover_datastores(db: Session, settings: Settings, instance: PbsInstance) -> DatastoreDiscoveryResult:
    """Reconcile RESURGAM's `Datastore` rows for *instance* with the
    datastores PBS currently has configured (`GET /config/datastore`,
    `connectivity.pbs_api.list_datastores`).

    - A datastore PBS lists that RESURGAM doesn't know yet is imported,
      with exactly the fields the manual import endpoint
      (`POST /pbs-instances/{id}/datastores`) sets: name, path, comment.
      It starts unassigned (no customer) -- assignment stays a human
      decision.
    - An already-known datastore gets its `path`/`comment` refreshed
      when PBS reports different values.
    - A known datastore PBS no longer lists gets `removed_from_pbs_at`
      set (mirroring `BackupGroup.removed_from_pbs_at`); nothing is
      deleted -- customer assignment, inventory and usage history stay.
      If it reappears later, the mark is cleared again (`restored`).

    When PBS is unreachable (or returns an unexpected shape) nothing is
    touched at all -- an outage must never mark every datastore removed.
    Each creation/removal/restoration writes an audit event attributed to
    the Collector. Only flushes; the caller owns the transaction (commit).

    Called at the start of every `sync_pbs_instance` (scheduled pass and
    manual "Sync" button alike), and safe to call on its own right after
    a PBS instance has been created so its datastores show up at once.
    Idempotent: a second call with unchanged PBS data changes nothing.
    """
    token = _get_token(db, settings, instance.api_token_secret_ref_id)
    trust = _trust_for(instance)
    listing = list_datastores(instance.host, instance.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    if not listing.reachable:
        return DatastoreDiscoveryResult(reachable=False, error=listing.error)

    result = DatastoreDiscoveryResult(reachable=True)
    now = datetime.now(UTC)
    remote_by_name = {remote.name: remote for remote in listing.datastores or []}
    known_by_name = {d.name: d for d in db.query(Datastore).filter_by(pbs_instance_id=instance.id).all()}

    for name, remote in remote_by_name.items():
        datastore = known_by_name.get(name)
        if datastore is None:
            datastore = Datastore(pbs_instance_id=instance.id, name=name, path=remote.path, comment=remote.comment)
            db.add(datastore)
            db.flush()
            result.created.append(name)
            _record_system_audit(
                db,
                action="datastore_discovered",
                datastore=datastore,
                detail={"pbs_instance_id": str(instance.id), "name": name},
            )
            continue

        if datastore.removed_from_pbs_at is not None:
            datastore.removed_from_pbs_at = None
            result.restored.append(name)
            _record_system_audit(
                db,
                action="datastore_reappeared_on_pbs",
                datastore=datastore,
                detail={"pbs_instance_id": str(instance.id), "name": name},
            )
        if datastore.path != remote.path or datastore.comment != remote.comment:
            datastore.path = remote.path
            datastore.comment = remote.comment
            result.updated.append(name)

    for name, datastore in known_by_name.items():
        if name not in remote_by_name and datastore.removed_from_pbs_at is None:
            datastore.removed_from_pbs_at = now
            result.removed.append(name)
            _record_system_audit(
                db,
                action="datastore_removed_from_pbs",
                datastore=datastore,
                detail={"pbs_instance_id": str(instance.id), "name": name},
            )

    db.flush()
    return result


def sync_instance_tasks(db: Session, settings: Settings, instance: PbsInstance) -> TaskSyncResult:
    """Sync task history (backup/verify/prune/GC/sync runs) for a PBS instance."""
    token = _get_token(db, settings, instance.api_token_secret_ref_id)
    trust = _trust_for(instance)

    result = list_tasks(instance.host, instance.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    if not result.reachable:
        return TaskSyncResult(reachable=False, error=result.error)

    created = updated = 0
    now = datetime.now(UTC)

    for task in result.tasks:
        existing = db.query(TaskRun).filter_by(pbs_instance_id=instance.id, upid=task.upid).one_or_none()
        started_at = datetime.fromtimestamp(task.start_time, tz=UTC)
        ended_at = datetime.fromtimestamp(task.end_time, tz=UTC) if task.end_time else None
        status = _task_status_from_remote(task.status, task.end_time)
        task_type = _TASK_TYPE_MAP.get(task.task_type, TaskType.UNKNOWN.value)

        if existing is None:
            db.add(
                TaskRun(
                    pbs_instance_id=instance.id,
                    upid=task.upid,
                    task_type=task_type,
                    worker_id=task.worker_id,
                    status=status,
                    started_at=started_at,
                    ended_at=ended_at,
                    task_user=task.task_user,
                )
            )
            created += 1
        else:
            existing.status = status
            existing.ended_at = ended_at
            existing.last_synced_at = now
            updated += 1

    db.flush()
    return TaskSyncResult(reachable=True, tasks_created=created, tasks_updated=updated)


def sync_instance_sync_jobs(db: Session, settings: Settings, instance: PbsInstance) -> SyncJobSyncResult:
    """Sync configured Sync Jobs (manifest §8's `sync_jobs`) for a PBS
    instance — config data, upserted like `PbsInstance`/`BackupGroup`, not
    append-only like `UsageMeasurement`. Field shapes are unverified
    against a real PBS (see `connectivity.pbs_api.list_sync_jobs`'s
    docstring), so this is best-effort like the rest of the connectivity
    layer was before this milestone's live verification pass.

    A job PBS no longer lists is *deleted* here, not marked: `SyncJob` is
    a live config mirror like `PbsAclEntry`, nothing references it by FK,
    and every consumer (the per-instance sync-job inventory endpoint,
    Recovery Evidence's offsite status, which treats any row for a
    datastore as "has an offsite copy") reads all rows unfiltered -- a
    stale row would keep claiming replication that no longer exists.
    Skipped entirely when PBS is unreachable.
    """
    token = _get_token(db, settings, instance.api_token_secret_ref_id)
    trust = _trust_for(instance)

    result = list_sync_jobs(instance.host, instance.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    if not result.reachable:
        return SyncJobSyncResult(reachable=False, error=result.error)

    datastores_by_name = {d.name: d.id for d in db.query(Datastore).filter_by(pbs_instance_id=instance.id).all()}

    created = updated = 0
    now = datetime.now(UTC)

    for job in result.sync_jobs:
        existing = db.query(SyncJob).filter_by(pbs_instance_id=instance.id, job_id=job.job_id).one_or_none()
        local_datastore_id = datastores_by_name.get(job.local_store)

        if existing is None:
            db.add(
                SyncJob(
                    pbs_instance_id=instance.id,
                    job_id=job.job_id,
                    remote_name=job.remote_name,
                    remote_store=job.remote_store,
                    local_store_name=job.local_store,
                    local_datastore_id=local_datastore_id,
                    schedule=job.schedule,
                    remove_vanished=job.remove_vanished,
                    comment=job.comment,
                )
            )
            created += 1
        else:
            existing.remote_name = job.remote_name
            existing.remote_store = job.remote_store
            existing.local_store_name = job.local_store
            existing.local_datastore_id = local_datastore_id
            existing.schedule = job.schedule
            existing.remove_vanished = job.remove_vanished
            existing.comment = job.comment
            existing.last_synced_at = now
            updated += 1

    remote_job_ids = {job.job_id for job in result.sync_jobs}
    removed = 0
    for stale in db.query(SyncJob).filter_by(pbs_instance_id=instance.id).all():
        if stale.job_id not in remote_job_ids:
            db.delete(stale)
            removed += 1

    db.flush()
    return SyncJobSyncResult(
        reachable=True, sync_jobs_created=created, sync_jobs_updated=updated, sync_jobs_removed=removed
    )


def sync_instance_acl(db: Session, settings: Settings, instance: PbsInstance) -> AclSyncResult:
    """Mirror PBS's *current* ACL state (`GET /access/acl`) for this
    instance -- raised directly by the user after the Recovery Evidence
    report's "Access Control" section was shown to only ever list
    RESURGAM's own role assignments, missing real access granted
    natively in PBS. A live mirror, not history: every existing
    `PbsAclEntry` row for this instance is replaced wholesale each sync
    (delete-then-reinsert) rather than diffed field-by-field, since a
    revoked grant must actually disappear, and PBS's own ACL listing is
    already small enough that a full replace costs nothing meaningful.
    """
    token = _get_token(db, settings, instance.api_token_secret_ref_id)
    trust = _trust_for(instance)

    result = list_acl_entries(instance.host, instance.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    if not result.reachable:
        return AclSyncResult(reachable=False, error=result.error)

    db.query(PbsAclEntry).filter_by(pbs_instance_id=instance.id).delete()
    for entry in result.entries or []:
        db.add(
            PbsAclEntry(
                pbs_instance_id=instance.id,
                path=entry.path,
                role=entry.role,
                subject=entry.subject,
                subject_type=entry.subject_type,
                propagate=entry.propagate,
            )
        )
    db.flush()
    return AclSyncResult(reachable=True, entries_synced=len(result.entries or []))


def sync_pbs_instance(db: Session, settings: Settings, instance: PbsInstance) -> PbsInstanceSyncResult:
    """Run the full Collector pass for one PBS instance: datastore
    discovery (`discover_datastores` -- new datastores imported, path/
    comment refreshed, vanished ones marked), task history, configured
    sync jobs (manifest §8), ACL grants, then backup inventory (plus a
    usage measurement, manifest §9.3) for every datastore still present
    on PBS. Datastores marked `removed_from_pbs_at` are skipped (PBS
    would only answer "not found"); their groups keep their last known
    state. One datastore's failure doesn't abort the others — each is
    reported independently; a discovery failure doesn't abort the rest.

    Sets `instance.last_collector_sync_at` unconditionally on every call
    (manual "Sync" button and the scheduled pass alike, see
    `worker/collector_scheduler.py`) -- it marks that a sync was
    *attempted* just now, the staleness signal the frontend shows, not
    that every sub-sync actually reached PBS.
    """
    instance.last_collector_sync_at = datetime.now(UTC)
    try:
        discovery_result = discover_datastores(db, settings, instance)
    except Exception as exc:  # noqa: BLE001 - discovery failing must not abort the inventory sync
        discovery_result = DatastoreDiscoveryResult(reachable=False, error=str(exc))
    tasks_result = sync_instance_tasks(db, settings, instance)
    sync_jobs_result = sync_instance_sync_jobs(db, settings, instance)
    acl_result = sync_instance_acl(db, settings, instance)

    token = _get_token(db, settings, instance.api_token_secret_ref_id)
    trust = _trust_for(instance)
    usage_result = list_datastore_usage(
        instance.host, instance.port, trust, token, timeout=settings.connectivity_timeout_seconds
    )
    usage_by_name = {u.name: u for u in usage_result.usage} if usage_result.reachable and usage_result.usage else {}
    # Kept as-is when PBS can't be asked right now -- a timezone doesn't
    # change between syncs, and a stale value beats falling back to UTC.
    server_timezone = get_server_timezone(
        instance.host, instance.port, trust, token, timeout=settings.connectivity_timeout_seconds
    )
    if server_timezone is not None:
        instance.server_timezone = server_timezone

    outcomes: list[DatastoreSyncOutcome] = []
    datastores = db.query(Datastore).filter_by(pbs_instance_id=instance.id, removed_from_pbs_at=None).all()
    for datastore in datastores:
        try:
            result = sync_datastore_inventory(db, settings, datastore, usage_by_name.get(datastore.name))
        except Exception as exc:  # noqa: BLE001 - a single datastore's failure must not abort the others
            result = InventorySyncResult(reachable=False, error=str(exc))
        outcomes.append(DatastoreSyncOutcome(datastore_id=datastore.id, datastore_name=datastore.name, result=result))

    return PbsInstanceSyncResult(
        tasks=tasks_result,
        sync_jobs=sync_jobs_result,
        acl=acl_result,
        datastores=outcomes,
        discovery=discovery_result,
    )
