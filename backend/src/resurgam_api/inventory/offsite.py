"""ADR 0032, part 2: is there an offsite copy of each backup group, and how far
behind is it? Computed on read from already-synced data (sync jobs, backup
groups, snapshots), never stored -- same pattern as protection status and the
capacity forecast.

A PBS sync job becomes a *copy edge*: source datastore + namespace -> target
datastore + namespace. Pull jobs copy remote -> local, push jobs (PBS 3.3+)
local -> remote. A remote is matched to a connected PBS instance by its
certificate fingerprint, else by host and port. A synced snapshot keeps its
`backup-time`, so "not copied yet" means: newer than the newest snapshot in
the copy.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from resurgam_api.instance_settings import get_instance_settings
from resurgam_api.models.datastore import Datastore, DatastoreCustomerAssignment, DatastoreRole
from resurgam_api.models.inventory import BackupGroup, BackupSnapshot, ProtectionPolicy
from resurgam_api.models.pbs import PbsInstance
from resurgam_api.models.sync import SyncJob

DEFAULT_LAG_WARNING_HOURS = 36
DEFAULT_LAG_CRITICAL_HOURS = 72
PBS_DEFAULT_PORT = 8007

# PBS group filter types -> backup_type as stored on BackupGroup.
_FILTER_TYPES = {"vm", "ct", "host"}


class OffsiteStatus(str, Enum):
    OK = "ok"
    LAGGING = "lagging"
    MISSING = "missing"
    LOCAL_COPY_ONLY = "local_copy_only"
    TARGET_NOT_CONNECTED = "target_not_connected"
    NO_COPY_JOB = "no_copy_job"
    EXCEPTED = "excepted"
    NOT_REQUIRED = "not_required"


def lag_thresholds(db: Session) -> tuple[int, int]:
    """(warning, critical) hours -- the platform setting, else the defaults."""
    settings = get_instance_settings(db)
    warning = (settings.offsite_lag_warning_hours if settings else None) or DEFAULT_LAG_WARNING_HOURS
    critical = (settings.offsite_lag_critical_hours if settings else None) or DEFAULT_LAG_CRITICAL_HOURS
    return warning, max(critical, warning)


# --- group filter -------------------------------------------------------------


def _filter_matches(spec: str, backup_type: str, backup_id: str) -> bool:
    kind, _, value = spec.partition(":")
    group_id = f"{backup_type}/{backup_id}"
    if kind == "type":
        return value == backup_type
    if kind == "group":
        return value == group_id
    if kind == "regex":
        try:
            # Rust's `Regex::is_match` searches anywhere, like `re.search`.
            return re.search(value, group_id) is not None
        except re.error:
            # A pattern Python can't read: assume it matches, so a missing
            # copy shows as a gap instead of silently counting as filtered out.
            return True
    return False


def group_filter_allows(filters: list[str] | tuple[str, ...] | None, backup_type: str, backup_id: str) -> bool:
    """PBS's documented rule (managing-remotes, "group-filter"): no filters ->
    every group; only include filters -> groups matching one of them; only
    exclude filters -> every group but those; both -> included minus excluded."""
    if not filters:
        return True
    includes, excludes = [], []
    for raw in filters:
        if raw.startswith("exclude:"):
            excludes.append(raw.removeprefix("exclude:"))
        else:
            includes.append(raw.removeprefix("include:"))
    if any(_filter_matches(spec, backup_type, backup_id) for spec in excludes):
        return False
    if includes:
        return any(_filter_matches(spec, backup_type, backup_id) for spec in includes)
    return True


# --- namespaces ----------------------------------------------------------------


def _ns_parts(ns: str | None) -> list[str]:
    return [part for part in (ns or "").split("/") if part]


def map_namespace(group_ns: str, source_ns: str | None, target_ns: str | None, max_depth: int | None) -> str | None:
    """Where a group in `group_ns` lands, or None if the job doesn't reach it.
    `max_depth` counts levels below `source_ns` (0 = only that namespace,
    None = all levels)."""
    group, source = _ns_parts(group_ns), _ns_parts(source_ns)
    if group[: len(source)] != source:
        return None
    relative = group[len(source) :]
    if max_depth is not None and len(relative) > max_depth:
        return None
    return "/".join(_ns_parts(target_ns) + relative)


# --- copy edges ----------------------------------------------------------------


@dataclass(frozen=True)
class CopyEdge:
    sync_job_id: uuid.UUID
    job_label: str
    source_datastore_id: uuid.UUID | None
    source_ns: str | None
    target_datastore_id: uuid.UUID | None
    target_instance_id: uuid.UUID | None
    target_ns: str | None
    max_depth: int | None
    group_filter: tuple[str, ...]
    # Where the copy goes, readable, also when the target isn't connected.
    target_label: str


def _normalize_fingerprint(value: str | None) -> str | None:
    return value.replace(":", "").lower() if value else None


def _match_instance(job: SyncJob, instances: list[PbsInstance]) -> PbsInstance | None:
    fingerprint = _normalize_fingerprint(job.remote_fingerprint)
    if fingerprint:
        for instance in instances:
            if _normalize_fingerprint(instance.tls_fingerprint_sha256) == fingerprint:
                return instance
    if job.remote_host:
        port = job.remote_port or PBS_DEFAULT_PORT
        for instance in instances:
            if instance.host.lower() == job.remote_host.lower() and instance.port == port:
                return instance
    return None


def build_copy_edges(db: Session) -> list[CopyEdge]:
    instances = db.query(PbsInstance).all()
    instances_by_id = {i.id: i for i in instances}
    datastore_ids = {
        (d.pbs_instance_id, d.name): d.id
        for d in db.query(Datastore).filter(Datastore.removed_from_pbs_at.is_(None)).all()
    }
    edges: list[CopyEdge] = []
    for job in db.query(SyncJob).all():
        local = instances_by_id.get(job.pbs_instance_id)
        if local is None:
            continue
        remote = local if not job.remote_name else _match_instance(job, instances)
        remote_side = f"{job.remote_name} / {job.remote_store}" if job.remote_name else job.remote_store
        local_side = f"{local.display_name} / {job.local_store_name}"
        if job.sync_direction == "push":
            source_instance, source_store, source_ns = local, job.local_store_name, job.local_ns
            target_instance, target_store, target_ns = remote, job.remote_store, job.remote_ns
            target_label = (
                f"{target_instance.display_name} / {target_store}" if target_instance else f"remote {remote_side}"
            )
        else:
            source_instance, source_store, source_ns = remote, job.remote_store, job.remote_ns
            target_instance, target_store, target_ns = local, job.local_store_name, job.local_ns
            target_label = local_side
        edges.append(
            CopyEdge(
                sync_job_id=job.id,
                job_label=f"{local.display_name}: sync job {job.job_id}",
                source_datastore_id=datastore_ids.get((source_instance.id, source_store)) if source_instance else None,
                source_ns=source_ns,
                target_datastore_id=datastore_ids.get((target_instance.id, target_store)) if target_instance else None,
                target_instance_id=target_instance.id if target_instance else None,
                target_ns=target_ns,
                max_depth=job.max_depth,
                group_filter=tuple(job.group_filter or ()),
                target_label=target_label,
            )
        )
    return edges


# --- per-group evaluation ---------------------------------------------------------


@dataclass
class OffsiteEvaluation:
    group: BackupGroup
    status: OffsiteStatus
    copy_label: str | None = None
    copy_backup_group_id: uuid.UUID | None = None
    latest_backup_at: datetime | None = None
    latest_copy_at: datetime | None = None
    oldest_uncopied_at: datetime | None = None
    uncopied_count: int = 0
    lag_hours: float | None = None
    # Sync jobs covering the group, readable.
    jobs: list[str] = field(default_factory=list)
    # The group's protection policy requires Sync/DR (`sync_dr_required`):
    # only then is "no sync job at all" an alarm, not just a fact.
    dr_required: bool = False


def _copy_key(datastore_id: uuid.UUID, namespace: str, backup_type: str, backup_id: str):
    return (datastore_id, namespace, backup_type, backup_id)


def evaluate_offsite(
    db: Session, groups: list[BackupGroup], now: datetime, edges: list[CopyEdge] | None = None
) -> tuple[list[OffsiteEvaluation], set[uuid.UUID]]:
    """Evaluate every *source* group in `groups`. Groups that are themselves a
    copy (on an offsite-role datastore, or the target of a copy edge) are not
    evaluated; their ids come back as the second value so callers can tell
    them apart. Exceptions and leg applicability are applied by the caller
    (`inventory/backup_gaps.py`)."""
    if edges is None:
        edges = build_copy_edges(db)
    warning_hours, _critical_hours = lag_thresholds(db)

    datastores = {d.id: d for d in db.query(Datastore).all()}
    offsite_role = {
        a.datastore_id for a in db.query(DatastoreCustomerAssignment).filter_by(role=DatastoreRole.OFFSITE.value).all()
    }
    target_datastores = {e.target_datastore_id for e in edges if e.target_datastore_id}

    # Every current group on a copy target, by identity.
    copies: dict[tuple, BackupGroup] = {}
    if target_datastores:
        for g in (
            db.query(BackupGroup)
            .filter(
                BackupGroup.datastore_id.in_(target_datastores),
                BackupGroup.superseded_at.is_(None),
                BackupGroup.removed_from_pbs_at.is_(None),
            )
            .all()
        ):
            copies[_copy_key(g.datastore_id, g.namespace or "", g.backup_type, g.backup_id)] = g

    copy_group_ids: set[uuid.UUID] = set()
    candidates: list[tuple[BackupGroup, list[tuple[CopyEdge, str]]]] = []
    for group in groups:
        if group.datastore_id in offsite_role or _is_copy(group, edges):
            copy_group_ids.add(group.id)
            continue
        covering = []
        for edge in edges:
            if edge.source_datastore_id != group.datastore_id:
                continue
            if not group_filter_allows(edge.group_filter, group.backup_type, group.backup_id):
                continue
            target_ns = map_namespace(group.namespace or "", edge.source_ns, edge.target_ns, edge.max_depth)
            if target_ns is None:
                continue
            covering.append((edge, target_ns))
        candidates.append((group, covering))

    snapshot_times = _snapshot_times(db, [g for g, _ in candidates] + [copies[k] for k in copies])
    dr_policies = set(db.scalars(select(ProtectionPolicy.id).where(ProtectionPolicy.sync_dr_required.is_(True))))

    results: list[OffsiteEvaluation] = []
    for group, covering in candidates:
        own_times = snapshot_times.get(group.id, [])
        evaluation = OffsiteEvaluation(
            group=group,
            status=OffsiteStatus.NO_COPY_JOB,
            latest_backup_at=own_times[-1] if own_times else group.last_backup_at,
            jobs=[edge.job_label for edge, _ in covering],
            dr_required=group.protection_policy_id in dr_policies,
        )
        results.append(evaluation)
        if not covering:
            continue

        best: tuple[BackupGroup, CopyEdge] | None = None
        local_only: tuple[BackupGroup, CopyEdge] | None = None
        unconnected: CopyEdge | None = None
        for edge, target_ns in covering:
            if edge.target_datastore_id is None:
                unconnected = unconnected or edge
                continue
            copy = copies.get(_copy_key(edge.target_datastore_id, target_ns, group.backup_type, group.backup_id))
            if copy is None:
                continue
            if not _edge_is_offsite(edge, group, datastores, offsite_role):
                local_only = local_only or (copy, edge)
            elif best is None or _latest(snapshot_times, copy.id) > _latest(snapshot_times, best[0].id):
                best = (copy, edge)

        offsite_edges = [
            (edge, ns)
            for edge, ns in covering
            if edge.target_datastore_id is not None and _edge_is_offsite(edge, group, datastores, offsite_role)
        ]
        if best is None and offsite_edges:
            # A job copies the group offsite, but nothing has arrived yet.
            evaluation.copy_label = offsite_edges[0][0].target_label
            _apply_lag(evaluation, own_times, None, now, warning_hours, missing_if_late=True)
        elif best is None and unconnected is not None:
            evaluation.status = OffsiteStatus.TARGET_NOT_CONNECTED
            evaluation.copy_label = unconnected.target_label
        elif best is None and local_only is not None:
            evaluation.status = OffsiteStatus.LOCAL_COPY_ONLY
            evaluation.copy_label = local_only[1].target_label
            evaluation.copy_backup_group_id = local_only[0].id
        elif best is None:
            # Only same-PBS jobs, and their copy doesn't exist either.
            evaluation.status = OffsiteStatus.LOCAL_COPY_ONLY
            evaluation.copy_label = covering[0][0].target_label
        else:
            copy, edge = best
            copy_times = snapshot_times.get(copy.id, [])
            evaluation.copy_label = edge.target_label
            evaluation.copy_backup_group_id = copy.id
            evaluation.latest_copy_at = copy_times[-1] if copy_times else None
            _apply_lag(evaluation, own_times, evaluation.latest_copy_at, now, warning_hours, missing_if_late=False)
    return results, copy_group_ids


def _latest(snapshot_times: dict[uuid.UUID, list[datetime]], group_id: uuid.UUID) -> datetime:
    times = snapshot_times.get(group_id)
    return times[-1] if times else datetime.min.replace(tzinfo=UTC)


def _edge_is_offsite(
    edge: CopyEdge, group: BackupGroup, datastores: dict[uuid.UUID, Datastore], offsite_role: set[uuid.UUID]
) -> bool:
    if edge.target_datastore_id in offsite_role:
        return True
    target, source = datastores.get(edge.target_datastore_id), datastores.get(group.datastore_id)
    return target is not None and source is not None and target.pbs_instance_id != source.pbs_instance_id


def _is_copy(group: BackupGroup, edges: list[CopyEdge]) -> bool:
    """On a sync job's target datastore, inside the namespace it writes to."""
    return any(
        edge.target_datastore_id == group.datastore_id
        and map_namespace(group.namespace or "", edge.target_ns, edge.target_ns, edge.max_depth) is not None
        for edge in edges
    )


def _apply_lag(
    evaluation: OffsiteEvaluation,
    own_times: list[datetime],
    latest_copy_at: datetime | None,
    now: datetime,
    warning_hours: int,
    *,
    missing_if_late: bool,
) -> None:
    """Snapshots newer than the copy's newest are not copied yet; the age of
    the oldest of them is the lag. Under the warning threshold that is just a
    copy in transit (`ok`)."""
    uncopied = [t for t in own_times if latest_copy_at is None or t > latest_copy_at]
    evaluation.uncopied_count = len(uncopied)
    evaluation.oldest_uncopied_at = uncopied[0] if uncopied else None
    evaluation.lag_hours = (now - uncopied[0]).total_seconds() / 3600 if uncopied else 0.0
    if evaluation.lag_hours < warning_hours:
        evaluation.status = OffsiteStatus.OK
    else:
        evaluation.status = OffsiteStatus.MISSING if missing_if_late else OffsiteStatus.LAGGING


def offsite_severity(
    status: OffsiteStatus, lag_hours: float | None, warning_hours: int, critical_hours: int, *, dr_required: bool
) -> str | None:
    """`critical`, `warning` or None -- ADR 0032's status table. Where a sync
    job exists, offsite was intended and every breakage is a gap. Without
    any sync job it is only a gap (an alarm) when the group's protection
    policy requires Sync/DR -- otherwise a single-site setup would get a
    permanent warning per workload."""
    if status == OffsiteStatus.MISSING:
        return "critical"
    if status == OffsiteStatus.LAGGING:
        return "critical" if (lag_hours or 0) >= critical_hours else "warning"
    if status in (OffsiteStatus.LOCAL_COPY_ONLY, OffsiteStatus.TARGET_NOT_CONNECTED):
        return "warning"
    if status == OffsiteStatus.NO_COPY_JOB:
        return "warning" if dr_required else None
    return None


def _snapshot_times(db: Session, groups: list[BackupGroup]) -> dict[uuid.UUID, list[datetime]]:
    """Snapshot times per group, oldest first, in one query."""
    ids = list({g.id for g in groups})
    if not ids:
        return {}
    rows = db.execute(
        select(BackupSnapshot.backup_group_id, BackupSnapshot.snapshot_time)
        .where(BackupSnapshot.backup_group_id.in_(ids))
        .order_by(BackupSnapshot.backup_group_id, BackupSnapshot.snapshot_time)
    ).all()
    times: dict[uuid.UUID, list[datetime]] = {}
    for group_id, snapshot_time in rows:
        times.setdefault(group_id, []).append(snapshot_time)
    return times
