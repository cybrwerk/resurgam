"""Backup Jobs -> "Run now" with a live status viewer.

PVE's GUI "Run now" is `POST /nodes/{node}/vzdump` with the job's settings on
every node that holds guests of the job (there is no run-job endpoint, see
`connectivity.pve_api.run_vzdump`); RESURGAM does the same and returns the
task ids, which the UI follows through `GET .../tasks/{node}/{upid}` (status
plus new log lines on every poll).
"""

import uuid
from collections import defaultdict
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.orm import Session

from resurgam_api.api.v1._connection_helpers import upstream_error
from resurgam_api.api.v1.pve_clusters import _cluster_trust_and_token, _get_cluster_or_404
from resurgam_api.auth.dependency import require_role
from resurgam_api.config import Settings, get_settings
from resurgam_api.connectivity.pve_api import (
    TASK_LOG_PAGE_SIZE,
    PveWriteError,
    VzdumpJob,
    _encode_prune_backups,
    get_task_log,
    get_task_status,
    get_vzdump_job,
    list_guests,
    run_vzdump,
)
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.identity import RoleKey
from resurgam_api.schemas.lab_setup import PveNodeName
from resurgam_api.schemas.pve_backup_run import (
    BackupRunOut,
    BackupRunSkippedOut,
    BackupRunTaskOut,
    PveTaskLogLine,
    PveTaskProgressOut,
)

router = APIRouter(prefix="/pve-clusters", tags=["pve-clusters"])
_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)

_UPID_PATTERN = r"^UPID:[A-Za-z0-9_.\-]+:[0-9A-Fa-f]+:[0-9A-Fa-f]+:[0-9A-Fa-f]+:[A-Za-z0-9_\-]+:[^:/]*:[^:/]+:$"


def _parse_vmids(value: str | None) -> list[int]:
    out = []
    for part in (value or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def _job_params(job: VzdumpJob) -> dict:
    """The job's settings as vzdump parameters -- what a scheduled run uses:
    storage, mode, compression, notes template, mail settings and the job's
    own retention (`prune-backups`, encoded like the job write path). Per
    PVE's API schema `prune-backups` needs `Datastore.Allocate` on the
    storage, which RESURGAM's required privileges include (on /storage); a
    job without its own retention leaves the key out, so the storage's
    retention applies, exactly as on a scheduled run. `VzdumpJob` carries no
    bwlimit/ionice/performance options, so none are sent (the node's
    vzdump.conf defaults apply, as they would for these jobs anyway).
    `job-id` is root@pam-only and therefore not sent."""
    params = {
        "storage": job.storage,
        "mode": job.mode,
        "compress": job.compress,
        "notes-template": job.notes_template,
        "mailto": job.mailto,
        "mailnotification": job.mailnotification,
        "prune-backups": _encode_prune_backups(job),
    }
    return {k: v for k, v in params.items() if v not in (None, "")}


@router.post("/{cluster_id}/backup-jobs/{job_id}/run", response_model=BackupRunOut, status_code=202)
def run_backup_job_now(
    cluster_id: uuid.UUID,
    job_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9_\-:.]{1,64}$", description="The vzdump job id.")],
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> BackupRunOut:
    """`platform_admin` only. Starts the backup job now, like PVE's own "Run
    now": one `vzdump` task per node holding guests of the job, with the job's
    storage, mode, compression, notes, mail settings and retention
    (`prune-backups`) -- so the run prunes like a scheduled one. Works for
    disabled jobs too. A job bound to a node backs up only that node's
    guests; VMIDs that are gone or on another node come back in `skipped`.
    Returns the task ids to follow with
    GET /pve-clusters/{id}/tasks/{node}/{upid}. 409
    `backup_job_selects_nothing` (with `details.skipped`) when nothing is
    left to back up. Audited."""
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    timeout = settings.connectivity_timeout_seconds
    job = get_vzdump_job(cluster.host, cluster.port, trust, token, timeout, job_id=job_id)
    if job is None:
        raise ResurgamApiError(code="not_found", message=f"Backup job '{job_id}' was not found.", status_code=404)
    guests = list_guests(cluster.host, cluster.port, trust, token, timeout)
    if not guests.reachable:
        raise upstream_error("pve", guests.error)
    node_of = {g.vmid: g.node for g in guests.guests}
    nodes_with_guests = sorted(set(node_of.values()))
    base = _job_params(job)

    plan: list[tuple[str, dict, list[int] | None]] = []
    skipped: list[BackupRunSkippedOut] = []
    if job.vmid and not job.all_guests:
        by_node: dict[str, list[int]] = defaultdict(list)
        for vmid in _parse_vmids(job.vmid):
            node = node_of.get(vmid)
            if node is None:
                skipped.append(BackupRunSkippedOut(vmid=vmid, reason="missing"))
            elif job.node and node != job.node:
                skipped.append(BackupRunSkippedOut(vmid=vmid, reason="other_node"))
            else:
                by_node[node].append(vmid)
        for node, vmids in sorted(by_node.items()):
            plan.append((node, {**base, "vmid": ",".join(str(v) for v in vmids)}, vmids))
    elif job.pool:
        for node in [job.node] if job.node else nodes_with_guests:
            plan.append((node, {**base, "pool": job.pool}, None))
    elif job.all_guests:
        selection = {"all": 1, **({"exclude": job.exclude} if job.exclude else {})}
        for node in [job.node] if job.node else nodes_with_guests:
            plan.append((node, {**base, **selection}, None))
    if not plan:
        raise ResurgamApiError(
            code="backup_job_selects_nothing",
            message="This job selects no existing guest on the cluster (removed VMs, guests on another node than "
            "the job's node, an empty pool or a node without guests), so there is nothing to back up.",
            status_code=409,
            details={"skipped": [entry.model_dump() for entry in skipped]},
        )

    tasks: list[BackupRunTaskOut] = []
    errors: list[str] = []
    for node, params, vmids in plan:
        try:
            upid = run_vzdump(cluster.host, cluster.port, trust, token, timeout, node=node, params=params)
        except PveWriteError as exc:
            errors.append(f"{node}: {exc}")
            continue
        tasks.append(BackupRunTaskOut(node=node, upid=upid, vmids=vmids))
    db.add(
        AuditEvent(
            actor_user_id=user.user_id,
            action="pve_backup_job_run_now",
            target_type="pve_cluster",
            target_id=str(cluster.id),
            correlation_id=getattr(request.state, "correlation_id", None),
            detail={"job_id": job_id, "tasks": [t.model_dump() for t in tasks], "errors": errors},
        )
    )
    db.commit()
    if not tasks:
        raise upstream_error("pve", "; ".join(errors))
    return BackupRunOut(job_id=job_id, tasks=tasks, skipped=skipped)


@router.get("/{cluster_id}/tasks/{node}/{upid}", response_model=PveTaskProgressOut)
def get_pve_task_progress(
    cluster_id: uuid.UUID,
    node: PveNodeName,
    upid: Annotated[str, Path(pattern=_UPID_PATTERN, description="The PVE task id (UPID).")],
    start: Annotated[
        int, Query(ge=0, description="Log lines to skip -- the previous call's `next_start` (0 on the first call).")
    ] = 0,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> PveTaskProgressOut:
    """`platform_admin` only. A PVE task's status and its log after the first
    `start` lines -- poll every few seconds while `status` is `running` (and
    once more right away while `more` is true) for a live view."""
    cluster = _get_cluster_or_404(db, cluster_id)
    if upid.split(":")[1] != node:
        raise ResurgamApiError(
            code="upid_node_mismatch", message="This task id belongs to another node.", status_code=400
        )
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    timeout = settings.connectivity_timeout_seconds
    status = get_task_status(cluster.host, cluster.port, trust, token, timeout, node=node, upid=upid)
    if not status.reachable:
        raise upstream_error("pve", status.error)
    lines = get_task_log(cluster.host, cluster.port, trust, token, timeout, node=node, upid=upid, start=start) or []
    # PVE's `n` is 1-based and `start` counts skipped lines, so the last `n`
    # is the next `start` (never move backwards).
    next_start = max((n for n, _ in lines), default=start)
    return PveTaskProgressOut(
        node=node,
        upid=upid,
        status=status.status,
        exitstatus=status.exitstatus,
        lines=[PveTaskLogLine(n=n, t=t) for n, t in lines],
        next_start=max(next_start, start),
        more=len(lines) >= TASK_LOG_PAGE_SIZE,
    )
