import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header
from sqlalchemy import exists
from sqlalchemy.orm import Session

from resurgam_api import encryption_keys
from resurgam_api.api.v1.restore_test_plans import _cluster_trust_and_token
from resurgam_api.auth.dependency import extract_bearer_token
from resurgam_api.auth.scope import effective_group_customer_id
from resurgam_api.collector.sync import sync_datastore_inventory
from resurgam_api.config import Settings, get_settings
from resurgam_api.connectivity.pve_api import (
    PveWriteError,
    clone_vm,
    configure_lab_appliance_network,
    get_task_status,
    start_guest,
)
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.lab_setup.uplink import (
    PURPOSE_LAB_RUN,
    allocate_uplink_ip,
    allocated_uplink_ip,
    release_uplink_ip,
    uplink_config_from_cluster,
    uplink_ipconfig,
    uplink_nameserver,
    uplink_nic,
)
from resurgam_api.models.application_group import ApplicationGroup, ApplicationGroupMember
from resurgam_api.models.customer_file_index_settings import CustomerFileIndexSettings
from resurgam_api.models.datastore import Datastore, DatastoreCustomerAssignment
from resurgam_api.models.file_index_entry import FileIndexEntry
from resurgam_api.models.file_index_job import FileIndexJob
from resurgam_api.models.inventory import BackupGroup, BackupSnapshot
from resurgam_api.models.pbs import PbsInstance
from resurgam_api.models.pve import PveCluster
from resurgam_api.models.restore_test import (
    RestoreTestPlan,
    RestoreTestPlanMember,
    RestoreTestPlanMemberStatus,
    RestoreTestPlanStatus,
)
from resurgam_api.restore_test.execution import (
    DEFAULT_POOL_NAME,
    aggregate_run_status,
    subnet_prefix_length,
    verify_appliance_run_token,
)
from resurgam_api.schemas.lab_agent import (
    LabAgentBackupCompletedRequest,
    LabAgentFileIndexClaimOut,
    LabAgentFileIndexEntriesRequest,
    LabAgentFileIndexJobOut,
    LabAgentFileIndexStatusUpdate,
    LabAgentJobOut,
    LabAgentStatusUpdate,
)
from resurgam_api.schemas.restore_test import ApplianceSnippetJobOut
from resurgam_api.worker.file_index_worker import (
    _compile_exclude_patterns,
    _insert_entries,
    _is_excluded,
    _link_unchanged_rows,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/lab-agent", tags=["recovery-lab"])

# The one fixed "archive" name for every guest-exec entry (ADR 0021) --
# unlike external_mount's per-disk archive names (the pxar archive name /
# a derived drive/disk segment), guest-exec walks the whole guest
# filesystem in one pass from inside the guest itself, so there's no
# separate archive concept to preserve.
_GUEST_EXEC_ARCHIVE_NAME = "guest-exec"

# A guest-exec job's own claim/entries/status endpoints below are shared
# by both opt-in methods -- kept as a tuple rather than importing
# FileIndexMethod's Literal (schemas/inventory.py), since this module
# only ever needs to check membership, not the full type.
_GUEST_EXEC_METHODS = ("guest_exec_live", "guest_exec_ephemeral")


def _require_lab_agent(db: Session, authorization: str | None) -> PveCluster:
    """`resurgam-lab-agent`'s own auth -- a long-lived, cluster-scoped
    bearer token (ADR 0019), never a user session. Reuses
    `verify_appliance_run_token`'s bcrypt compare (generically named
    despite the "appliance" name -- both are plain bearer-token
    hash/verify, no reason to duplicate it).
    """
    token = extract_bearer_token(authorization)
    if token is None:
        raise ResurgamApiError(code="unauthorized", message="Missing bearer token.", status_code=401)

    for cluster in db.query(PveCluster).filter(PveCluster.lab_agent_token_hash.isnot(None)).all():
        if verify_appliance_run_token(token, cluster.lab_agent_token_hash):
            _record_lab_agent_seen(db, cluster)
            return cluster
    raise ResurgamApiError(code="unauthorized", message="Invalid lab-agent token.", status_code=401)


# The agent polls every few seconds (`RESURGAM_LAB_AGENT_POLL_INTERVAL_SECONDS`,
# default 5) -- writing the timestamp on every request would turn the poll
# loop into a steady write stream for no benefit. The readiness check's
# "recently" window (`restore_test.lab_readiness.LAB_AGENT_RECENT_WINDOW`)
# is minutes, so a 30-second resolution is plenty.
LAB_AGENT_LAST_SEEN_WRITE_INTERVAL = timedelta(seconds=30)


def _record_lab_agent_seen(db: Session, cluster: PveCluster) -> None:
    """Stamps `PveCluster.lab_agent_last_seen_at` (the Lab Compute setup
    wizard's "agent has checked in" signal). Committed right away, before
    the endpoint does any work -- `_require_lab_agent` is always the first
    call in every `/lab-agent/*` endpoint, so there's nothing else pending
    in the session yet, and read-only endpoints (`GET /jobs`) never commit
    on their own.
    """
    now = datetime.now(UTC)
    last = cluster.lab_agent_last_seen_at
    if last is not None and now - last < LAB_AGENT_LAST_SEEN_WRITE_INTERVAL:
        return
    cluster.lab_agent_last_seen_at = now
    db.commit()


def _member_or_404(
    db: Session, member_id: uuid.UUID, cluster: PveCluster, *, for_update: bool = False
) -> RestoreTestPlanMember:
    query = db.query(RestoreTestPlanMember).filter_by(id=member_id)
    if for_update:
        query = query.with_for_update()
    member = query.one_or_none()
    if member is None:
        raise ResurgamApiError(code="not_found", message="Job not found.", status_code=404)
    plan = db.get(RestoreTestPlan, member.restore_test_plan_id)
    if plan is None or plan.pve_cluster_id != cluster.id:
        # This member's run isn't on the authenticated agent's own
        # cluster -- 404, not 403, mirroring the probe-results endpoint's
        # "a token only authenticates against what it's actually scoped
        # to" discipline.
        raise ResurgamApiError(code="not_found", message="Job not found.", status_code=404)
    return member


def _resolve_encryption_keyfile(db: Session, settings: Settings, snapshot: BackupSnapshot | None) -> str | None:
    """The key this job's backup was encrypted with (ADR 0031): exactly the one
    its fingerprint names, or None for an unencrypted backup. Pushed into the
    job payload rather than the agent's own config -- see
    `LabAgentJobOut.encryption_keyfile`. A key RESURGAM does not hold yields
    None here so one job cannot break the agent's whole job list; the job then
    fails with PBS's own "missing key ... <fingerprint>" message. Creating
    Instant Recovery, Disk Mounts and lab runs already refuses that case with
    a clear error (`encryption_keys.require_key_for_snapshot`)."""
    if snapshot is None:
        return None
    try:
        return encryption_keys.keyfile_for_snapshot(db, settings, snapshot)
    except encryption_keys.EncryptionKeyMissingError as exc:
        logger.warning("lab-agent job: %s", exc)
        return None


@router.get("/jobs", response_model=list[LabAgentJobOut])
def list_lab_agent_jobs(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
) -> list[LabAgentJobOut]:
    """Every `QUEUED` Application Group member on the authenticated
    agent's own cluster -- ready for `proxmox-backup-client map` +
    direct VM-config attach (ADR 0019). Nothing here is claimed yet;
    the agent must `POST /jobs/{member_id}/claim` before acting on one,
    so two agent instances (or one agent polling twice) can't both pick
    up the same member.

    `pool` is always RESURGAM's fixed Lab pool name -- the agent passes
    it straight through to `qm create --pool`, so member VMs join the
    same pool the probe appliance clone joins inline, without needing a
    separate PVE-API pool-membership write from the backend.
    """
    cluster = _require_lab_agent(db, authorization)
    members = (
        db.query(RestoreTestPlanMember)
        .join(RestoreTestPlan, RestoreTestPlanMember.restore_test_plan_id == RestoreTestPlan.id)
        .filter(
            RestoreTestPlan.pve_cluster_id == cluster.id,
            RestoreTestPlanMember.member_status == RestoreTestPlanMemberStatus.QUEUED.value,
        )
        .all()
    )

    jobs = []
    for member in members:
        group_member = db.get(ApplicationGroupMember, member.application_group_member_id)
        backup_group = db.get(BackupGroup, group_member.backup_group_id)
        datastore = db.get(Datastore, backup_group.datastore_id)
        pbs_instance = db.get(PbsInstance, datastore.pbs_instance_id)
        snapshot = db.get(BackupSnapshot, member.backup_snapshot_id)
        plan = db.get(RestoreTestPlan, member.restore_test_plan_id)

        jobs.append(
            LabAgentJobOut(
                member_id=member.id,
                pbs_host=pbs_instance.host,
                pbs_port=pbs_instance.port,
                pbs_datastore=datastore.name,
                pbs_namespace=backup_group.namespace,
                backup_type=backup_group.backup_type,
                backup_id=backup_group.backup_id,
                snapshot_time=snapshot.snapshot_time,
                target_vmid=member.target_vmid,
                target_node=member.target_node,
                target_storage=member.target_storage,
                vnet_name=plan.vnet_name,
                pool=DEFAULT_POOL_NAME,
                encryption_keyfile=_resolve_encryption_keyfile(db, settings, snapshot),
            )
        )
    return jobs


@router.post("/jobs/{member_id}/claim", status_code=204)
def claim_lab_agent_job(
    member_id: uuid.UUID, db: Session = Depends(get_db), authorization: str | None = Header(default=None)
) -> None:
    cluster = _require_lab_agent(db, authorization)
    # Locked read: two concurrent claims for the same member (two agent
    # instances, or one instance's retried poll after a slow response)
    # must not both pass the status check before either commits.
    member = _member_or_404(db, member_id, cluster, for_update=True)
    if member.member_status != RestoreTestPlanMemberStatus.QUEUED.value:
        raise ResurgamApiError(code="not_queued", message="This job is not currently queued.", status_code=409)

    member.member_status = RestoreTestPlanMemberStatus.MOUNTING.value
    member.started_at = datetime.now(UTC)
    db.commit()


@router.post("/jobs/{member_id}/status", status_code=204)
def report_lab_agent_job_status(
    member_id: uuid.UUID,
    body: LabAgentStatusUpdate,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> None:
    """The agent's own callback once it has (or has failed to) map,
    attach, and start a member's VM. `booted` moves the member straight
    to `PROBING` -- there's no separate confirmation step in this phase,
    the agent's own report *is* the confirmation that the guest was
    told to start. Does not itself advance to the next member in
    `startup_order` -- that's still `poll`'s job (queuing the next
    eligible member), kept in one place rather than duplicated here.
    """
    cluster = _require_lab_agent(db, authorization)
    member = _member_or_404(db, member_id, cluster)
    if member.member_status != RestoreTestPlanMemberStatus.MOUNTING.value:
        raise ResurgamApiError(code="not_mounting", message="This job is not currently in progress.", status_code=409)

    if body.status == "booted":
        member.member_status = RestoreTestPlanMemberStatus.PROBING.value
    else:
        member.member_status = RestoreTestPlanMemberStatus.FAILED.value
        member.error = body.error
        member.completed_at = datetime.now(UTC)

    plan = db.get(RestoreTestPlan, member.restore_test_plan_id)
    all_members = db.query(RestoreTestPlanMember).filter_by(restore_test_plan_id=plan.id).all()
    # A plan already FAILED at the appliance-provisioning level (clone/
    # network config, never member-driven) must stay failed -- a real
    # bug found live (2026-09-22): this unconditional recompute clobbered
    # that terminal state back to "running"/whatever the members' own
    # status implied, the moment the next member callback arrived.
    if plan.status != RestoreTestPlanStatus.FAILED.value:
        plan.status = aggregate_run_status([m.member_status for m in all_members])
    db.commit()


@router.get("/appliance-jobs", response_model=list[ApplianceSnippetJobOut])
def list_lab_agent_appliance_jobs(
    db: Session = Depends(get_db), authorization: str | None = Header(default=None)
) -> list[ApplianceSnippetJobOut]:
    """Every Recovery Lab run on the authenticated agent's own cluster
    still waiting on its probe appliance's cloud-init snippet to be
    written (Phase B: PVE has no REST endpoint to upload a snippet file,
    confirmed live 2026-09-21 -- `resurgam-lab-agent` writes it directly
    to `cluster.lab_snippet_storage_id`'s local path instead, e.g. via
    `pvesm path <storage>:snippets/<file>`).
    """
    cluster = _require_lab_agent(db, authorization)
    plans = (
        db.query(RestoreTestPlan)
        .filter(
            RestoreTestPlan.pve_cluster_id == cluster.id,
            RestoreTestPlan.status == RestoreTestPlanStatus.PROVISIONING_APPLIANCE.value,
            RestoreTestPlan.appliance_snippet_pending.isnot(None),
        )
        .all()
    )
    return [
        ApplianceSnippetJobOut(
            plan_id=plan.id,
            snippet_storage_id=cluster.lab_snippet_storage_id,
            filename=plan.appliance_snippet_pending["filename"],
            content=plan.appliance_snippet_pending["content"],
        )
        for plan in plans
    ]


@router.post("/appliance-jobs/{plan_id}/write-snippet", status_code=204)
def confirm_appliance_snippet_written(
    plan_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
) -> None:
    """The agent's confirmation that this run's snippet is written --
    the trigger for the deferred probe appliance clone (Phase B; see
    `execute_restore_test_plan`'s docstring). Clones the appliance
    dual-homed (net0 on the run's isolated Lab VNet, where it acts as
    its own gateway/DHCP/firewall/SNAT; net1 on the cluster's uplink
    bridge) with `cicustom` pointing at the snippet just written -- PVE
    itself never provisions anything beyond the zone/vnet it already
    created in `execute`.
    """
    cluster = _require_lab_agent(db, authorization)
    plan = db.query(RestoreTestPlan).filter_by(id=plan_id, pve_cluster_id=cluster.id).with_for_update().one_or_none()
    if plan is None or plan.appliance_snippet_pending is None:
        raise ResurgamApiError(
            code="not_found", message="No pending appliance snippet job for this run.", status_code=404
        )

    members = (
        db.query(RestoreTestPlanMember)
        .filter_by(restore_test_plan_id=plan.id)
        .order_by(RestoreTestPlanMember.target_vmid)
        .all()
    )
    group = db.get(ApplicationGroup, plan.application_group_id)
    snippet = plan.appliance_snippet_pending
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    timeout = settings.connectivity_timeout_seconds
    first_member = members[0]

    uplink = uplink_config_from_cluster(cluster)
    try:
        # Normally allocated at execute time; a run executed before its
        # cluster switched to a static range gets one now.
        uplink_address = allocated_uplink_ip(db, plan.id) or allocate_uplink_ip(
            db, cluster, purpose=PURPOSE_LAB_RUN, restore_test_plan_id=plan.id
        )
    except ResurgamApiError as exc:
        plan.status = RestoreTestPlanStatus.FAILED.value
        plan.error = f"Failed to clone/configure the probe appliance: {exc.message}"
        plan.completed_at = datetime.now(UTC)
        db.commit()
        return

    try:
        clone_upid = clone_vm(
            cluster.host,
            cluster.port,
            trust,
            token,
            timeout,
            node=first_member.target_node,
            source_vmid=cluster.lab_appliance_template_vmid,
            target_vmid=plan.appliance_vmid,
            name=f"lab-appliance-{plan.id.hex[:8]}",
            target_node=first_member.target_node,
            target_storage=first_member.target_storage,
            pool=DEFAULT_POOL_NAME,
        )
        # The clone holds a lock on the target VM until it actually
        # finishes -- confirmed live (2026-09-22): configuring net0/net1
        # immediately after firing the clone hit a real "VM is locked
        # (clone)" 500, since a full clone of a multi-GB template disk
        # is not instant. Poll the clone's own task status to completion
        # first.
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            clone_task = get_task_status(
                cluster.host, cluster.port, trust, token, timeout, node=first_member.target_node, upid=clone_upid
            )
            if not clone_task.reachable:
                raise PveWriteError(clone_task.error or "could not poll appliance clone task status")
            if clone_task.status == "stopped":
                if clone_task.exitstatus != "OK":
                    raise PveWriteError(f"appliance clone task failed: {clone_task.exitstatus}")
                break
            time.sleep(1)
        else:
            raise PveWriteError("appliance clone task did not finish within 180s")

        # Uplink (net1): the cluster's VLAN tag and DHCP or this run's
        # address from the static range (`lab_setup/uplink.py`).
        configure_lab_appliance_network(
            cluster.host,
            cluster.port,
            trust,
            token,
            timeout,
            node=first_member.target_node,
            vmid=plan.appliance_vmid,
            net0=f"model=virtio,bridge={plan.vnet_name}",
            net1=uplink_nic(cluster.lab_appliance_uplink_bridge, uplink.vlan_tag),
            ipconfig0=f"ip={group.lab_gateway_ip}/{subnet_prefix_length(group.lab_subnet_cidr)}",
            ipconfig1=uplink_ipconfig(uplink, uplink_address),
            cicustom=f"user={cluster.lab_snippet_storage_id}:snippets/{snippet['filename']}",
            nameserver=uplink_nameserver(uplink),
        )
        # A real gap found live (2026-09-22): the appliance was cloned
        # and correctly dual-homed but never actually started -- nothing
        # in this flow called it. Cloud-init only runs (and only then
        # enables/starts resurgam-lab-router/-prober) once the guest
        # actually boots.
        start_guest(
            cluster.host,
            cluster.port,
            trust,
            token,
            timeout,
            node=first_member.target_node,
            vmid=plan.appliance_vmid,
            guest_type="qemu",
        )
    except PveWriteError as exc:
        plan.status = RestoreTestPlanStatus.FAILED.value
        plan.error = f"Failed to clone/configure the probe appliance: {exc}"
        plan.completed_at = datetime.now(UTC)
        # The appliance never came up with this address, so it is free again.
        release_uplink_ip(db, restore_test_plan_id=plan.id)
        db.commit()
        return

    plan.appliance_snippet_pending = None
    plan.status = aggregate_run_status([m.member_status for m in members])
    db.commit()


# --- Guest-exec File Indexing (ADR 0021) ------------------------------------


def _ephemeral_job_cluster_id(db: Session, job: FileIndexJob) -> uuid.UUID | None:
    """`guest_exec_ephemeral` jobs don't store a cluster id directly --
    same as at job-creation time (`api/v1/file_index.py::
    _build_file_index_job`), the cluster is always the customer's own
    configured default (`CustomerFileIndexSettings.guest_exec_pve_
    cluster_id`), re-derived here rather than duplicated onto the job
    row.
    """
    if job.customer_id is None:
        return None
    settings = db.query(CustomerFileIndexSettings).filter_by(customer_id=job.customer_id).one_or_none()
    return settings.guest_exec_pve_cluster_id if settings is not None else None


def _owning_cluster_id(db: Session, job: FileIndexJob) -> uuid.UUID | None:
    if job.file_index_method == "guest_exec_live":
        group = db.get(BackupGroup, job.backup_group_id)
        return group.guest_exec_live_pve_cluster_id if group is not None else None
    if job.file_index_method == "guest_exec_ephemeral":
        return _ephemeral_job_cluster_id(db, job)
    return None


def _guest_exec_job_or_404(
    db: Session, job_id: uuid.UUID, cluster: PveCluster, *, for_update: bool = False
) -> FileIndexJob:
    query = db.query(FileIndexJob).filter_by(id=job_id)
    if for_update:
        query = query.with_for_update()
    job = query.one_or_none()
    if job is None or job.file_index_method not in _GUEST_EXEC_METHODS:
        raise ResurgamApiError(code="not_found", message="Job not found.", status_code=404)
    if _owning_cluster_id(db, job) != cluster.id:
        # Not this agent's job to see -- 404, not 403, matching
        # `_member_or_404`'s own "a token only authenticates against
        # what it's actually scoped to" discipline.
        raise ResurgamApiError(code="not_found", message="Job not found.", status_code=404)
    return job


@router.get("/file-index-jobs", response_model=list[LabAgentFileIndexJobOut])
def list_lab_agent_file_index_jobs(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
) -> list[LabAgentFileIndexJobOut]:
    """Every pending guest-exec index job on the authenticated agent's
    own cluster -- both opt-in methods (ADR 0021), the agent branches on
    `file_index_method`. `since`/`is_full_reindex` aren't decided here;
    they're decided once, at `POST .../claim` time, so a job re-listed
    across multiple poll cycles before being claimed can't see a stale
    cutoff.
    """
    cluster = _require_lab_agent(db, authorization)
    pending = (
        db.query(FileIndexJob)
        .filter(FileIndexJob.status == "pending", FileIndexJob.file_index_method.in_(_GUEST_EXEC_METHODS))
        .all()
    )

    jobs: list[LabAgentFileIndexJobOut] = []
    for job in pending:
        if _owning_cluster_id(db, job) != cluster.id:
            continue

        if job.file_index_method == "guest_exec_live":
            live_group = db.get(BackupGroup, job.backup_group_id)
            jobs.append(
                LabAgentFileIndexJobOut(
                    id=job.id,
                    file_index_method="guest_exec_live",
                    target_vmid=job.guest_exec_target_vmid,
                    target_node=job.guest_exec_target_node,
                    exclude_patterns=live_group.file_index_exclude_patterns if live_group is not None else [],
                    # Live guest exec indexes the running VM, never the backup: no key.
                    encryption_keyfile=None,
                )
            )
            continue

        group = db.get(BackupGroup, job.backup_group_id)
        snapshot = db.get(BackupSnapshot, job.backup_snapshot_id)
        datastore = db.get(Datastore, group.datastore_id)
        pbs_instance = db.get(PbsInstance, datastore.pbs_instance_id)
        index_settings = (
            db.query(CustomerFileIndexSettings).filter_by(customer_id=job.customer_id).one_or_none()
            if job.customer_id is not None
            else None
        )
        jobs.append(
            LabAgentFileIndexJobOut(
                id=job.id,
                file_index_method="guest_exec_ephemeral",
                target_vmid=job.guest_exec_target_vmid,
                target_node=job.guest_exec_target_node,
                target_storage=index_settings.guest_exec_target_storage if index_settings is not None else None,
                pbs_host=pbs_instance.host,
                pbs_port=pbs_instance.port,
                pbs_datastore=datastore.name,
                pbs_namespace=group.namespace,
                backup_type=group.backup_type,
                backup_id=group.backup_id,
                snapshot_time=snapshot.snapshot_time,
                pool=DEFAULT_POOL_NAME,
                exclude_patterns=group.file_index_exclude_patterns,
                encryption_keyfile=_resolve_encryption_keyfile(db, settings, snapshot),
            )
        )
    return jobs


def _decide_since_and_full(
    db: Session, backup_group_id: uuid.UUID, customer_id: uuid.UUID | None
) -> tuple[datetime | None, bool]:
    """A group's first-ever guest-exec run is always full (nothing to
    diff against); thereafter, every Nth completed run
    (`CustomerFileIndexSettings.full_reindex_every_n_runs`) forces
    another full walk -- the only thing that actually reconciles
    deletions/renames an incremental `-newermt`/`LastWriteTime` pass can
    never see (a full run's own fresh walk needs no carry-forward, see
    `_carry_forward_unchanged_entries`). Counted across *both* guest-exec
    methods for the group, not per-method -- switching methods mid-
    history shouldn't reset the cadence or lose the "since" baseline.
    """
    prior_job = (
        db.query(FileIndexJob)
        .filter(
            FileIndexJob.backup_group_id == backup_group_id,
            FileIndexJob.file_index_method.in_(_GUEST_EXEC_METHODS),
            FileIndexJob.status == "completed",
        )
        .order_by(FileIndexJob.started_at.desc())
        .first()
    )
    if prior_job is None:
        return None, True

    completed_count = (
        db.query(FileIndexJob)
        .filter(
            FileIndexJob.backup_group_id == backup_group_id,
            FileIndexJob.file_index_method.in_(_GUEST_EXEC_METHODS),
            FileIndexJob.status == "completed",
        )
        .count()
    )
    index_settings = (
        db.query(CustomerFileIndexSettings).filter_by(customer_id=customer_id).one_or_none()
        if customer_id is not None
        else None
    )
    n = index_settings.full_reindex_every_n_runs if index_settings is not None else None
    if n is not None and n > 0 and completed_count % n == 0:
        return None, True
    return prior_job.started_at, False


@router.post("/file-index-jobs/{job_id}/claim", response_model=LabAgentFileIndexClaimOut)
def claim_lab_agent_file_index_job(
    job_id: uuid.UUID, db: Session = Depends(get_db), authorization: str | None = Header(default=None)
) -> LabAgentFileIndexClaimOut:
    cluster = _require_lab_agent(db, authorization)
    job = _guest_exec_job_or_404(db, job_id, cluster, for_update=True)
    if job.status != "pending":
        raise ResurgamApiError(code="not_pending", message="This job is not currently pending.", status_code=409)

    since, is_full = _decide_since_and_full(db, job.backup_group_id, job.customer_id)
    job.status = "running"
    job.started_at = datetime.now(UTC)
    job.last_heartbeat_at = job.started_at
    job.is_full_reindex = is_full
    db.commit()
    return LabAgentFileIndexClaimOut(since=since, is_full_reindex=is_full)


@router.post("/file-index-jobs/{job_id}/entries", status_code=204)
def submit_lab_agent_file_index_entries(
    job_id: uuid.UUID,
    body: LabAgentFileIndexEntriesRequest,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> None:
    """Reuses `worker.file_index_worker`'s own `_link_unchanged_rows`/
    `_insert_entries` (ADR 0016) -- the identical write path
    `external_mount` jobs use, just called from here instead of that
    worker's own claim loop.

    A real gap found live (2026-09-24): this endpoint never applied the
    group's own `file_index_exclude_patterns` at all -- every entry a
    guest-exec run submitted was inserted unconditionally, including
    paths already excluded by default for `external_mount` (`Windows/
    WinSxS` etc.). The primary fix is the agent pruning excluded
    subtrees during enumeration itself (`LabAgentFileIndexJobOut.
    exclude_patterns`, `lab_agent.file_index.build_enumeration_command`)
    -- filtered again here as defense-in-depth, matching
    `worker.file_index_worker`'s own belt-and-suspenders precedent for
    `external_mount`, in case a pattern change lands mid-run or a guest-
    side translation gap lets something through.
    """
    cluster = _require_lab_agent(db, authorization)
    job = _guest_exec_job_or_404(db, job_id, cluster)
    if job.status != "running":
        raise ResurgamApiError(code="not_running", message="This job is not currently running.", status_code=409)

    group = db.get(BackupGroup, job.backup_group_id)
    compiled_excludes = _compile_exclude_patterns(group.file_index_exclude_patterns) if group is not None else []

    rows = [
        {
            "file_index_job_id": job.id,
            "backup_snapshot_id": job.backup_snapshot_id,
            "backup_group_id": job.backup_group_id,
            "customer_id": job.customer_id,
            "archive": _GUEST_EXEC_ARCHIVE_NAME,
            "path": entry.path,
            "filename": entry.filename,
            "type": entry.type,
            "size_bytes": entry.size_bytes,
            "mtime": entry.mtime,
            "linked_from_entry_id": None,
        }
        for entry in body.entries
        if not _is_excluded(entry.path, compiled_excludes)
    ]
    _link_unchanged_rows(db, job.backup_group_id, rows)
    _insert_entries(db, rows)
    job.entries_indexed += len(rows)
    job.last_heartbeat_at = datetime.now(UTC)
    db.commit()


def _carry_forward_unchanged_entries(db: Session, job: FileIndexJob) -> None:
    """The other half of incremental guest-exec indexing (ADR 0021): a
    `-newermt`/`LastWriteTime`-filtered run only reports *changed* files,
    so without this, a snapshot's own browsable tree (`GET /file-index/
    browse`, which filters by `backup_snapshot_id`) would show just that
    run's delta instead of the guest's whole tree at that point in time.
    Copies forward every prior completed guest-exec run's entry for a
    path this run didn't touch, linked to that entry's own root (never
    re-verified this run, so `size_bytes`/`mtime` stay null, same as any
    other "reuse and link" row). Skipped entirely for a full-reindex job
    (`job.is_full_reindex`) -- a full run's fresh walk already reports
    every path that currently exists, so carrying anything forward would
    just reintroduce the deleted/renamed paths a full run exists to
    reconcile away.
    """
    prior_job = (
        db.query(FileIndexJob)
        .filter(
            FileIndexJob.backup_group_id == job.backup_group_id,
            FileIndexJob.file_index_method.in_(_GUEST_EXEC_METHODS),
            FileIndexJob.status == "completed",
            FileIndexJob.id != job.id,
        )
        .order_by(FileIndexJob.started_at.desc())
        .first()
    )
    if prior_job is None:
        return

    touched_paths = {
        row[0] for row in db.query(FileIndexEntry.path).filter(FileIndexEntry.file_index_job_id == job.id).all()
    }
    prior_entries = db.query(FileIndexEntry).filter(FileIndexEntry.file_index_job_id == prior_job.id).all()
    carried = [
        {
            "file_index_job_id": job.id,
            "backup_snapshot_id": job.backup_snapshot_id,
            "backup_group_id": job.backup_group_id,
            "customer_id": job.customer_id,
            "archive": entry.archive,
            "path": entry.path,
            "filename": entry.filename,
            "type": entry.type,
            "size_bytes": None,
            "mtime": None,
            "linked_from_entry_id": entry.linked_from_entry_id or entry.id,
        }
        for entry in prior_entries
        if entry.path not in touched_paths
    ]
    if not carried:
        return
    _insert_entries(db, carried)
    job.entries_indexed += len(carried)


@router.post("/file-index-jobs/{job_id}/status", status_code=204)
def report_lab_agent_file_index_job_status(
    job_id: uuid.UUID,
    body: LabAgentFileIndexStatusUpdate,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> None:
    cluster = _require_lab_agent(db, authorization)
    job = _guest_exec_job_or_404(db, job_id, cluster)
    if job.status != "running":
        raise ResurgamApiError(code="not_running", message="This job is not currently running.", status_code=409)

    if body.status == "completed":
        if not job.is_full_reindex:
            _carry_forward_unchanged_entries(db, job)
        job.status = "completed"
        job.completed_at = datetime.now(UTC)
    else:
        job.status = "failed"
        job.error_code = "guest_exec_failed"
        job.error_message = body.error
        job.completed_at = datetime.now(UTC)
    db.commit()


def _resolve_customer_id_for_group(db: Session, group: BackupGroup) -> uuid.UUID | None:
    assignment = db.query(DatastoreCustomerAssignment).filter_by(datastore_id=group.datastore_id).one_or_none()
    return effective_group_customer_id(group, assignment)


@router.post("/backup-completed", status_code=204)
def report_backup_completed(
    body: LabAgentBackupCompletedRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
) -> None:
    """Relayed by the agent from its local trigger-file poll loop (ADR
    0021 Sec.4's hookscript -- `backup-end`, fired by vzdump's own
    node-level `script:` hook, confirmed live 2026-09-24 against a real
    backup, once this VM's own real data is already safely on PBS).
    Deliberately node-wide, not per-VM: PVE's per-VM `hookscript`
    property only fires on VM start/stop, never on backup (confirmed
    live against PVE's own `guest-example-hookscript.pl` vs.
    `vzdump-hook-script.pl` -- an earlier version of this feature wired
    the wrong one). The shipped script therefore fires unattended off
    every real backup of every VM on the node, the overwhelming majority
    of which have nothing to do with guest-exec indexing at all --
    intentionally lenient here, every "nothing to do" case is a silent
    no-op (204), never an error. Indexing-enabled is re-checked
    independently, not trusted from the trigger payload alone.
    """
    cluster = _require_lab_agent(db, authorization)
    group = (
        db.query(BackupGroup)
        .filter(
            BackupGroup.guest_exec_live_pve_cluster_id == cluster.id,
            BackupGroup.file_index_method == "guest_exec_live",
            BackupGroup.backup_id == str(body.vmid),
            BackupGroup.superseded_at.is_(None),
        )
        .one_or_none()
    )
    if group is None:
        return

    customer_id = _resolve_customer_id_for_group(db, group)
    index_settings = (
        db.query(CustomerFileIndexSettings).filter_by(customer_id=customer_id).one_or_none()
        if customer_id is not None
        else None
    )
    if index_settings is None or not index_settings.enabled or not group.file_index_enabled:
        return

    datastore = db.get(Datastore, group.datastore_id)
    sync_datastore_inventory(db, settings, datastore)

    snapshot = (
        db.query(BackupSnapshot)
        .filter_by(backup_group_id=group.id)
        .order_by(BackupSnapshot.snapshot_time.desc())
        .first()
    )
    if snapshot is None:
        return
    already_in_flight = db.query(
        exists().where(
            FileIndexJob.backup_snapshot_id == snapshot.id,
            FileIndexJob.status.in_(["pending", "running", "paused"]),
        )
    ).scalar()
    if already_in_flight:
        return

    db.add(
        FileIndexJob(
            status="pending",
            trigger="guest_exec_hook",
            file_index_method="guest_exec_live",
            backup_group_id=group.id,
            backup_snapshot_id=snapshot.id,
            customer_id=customer_id,
            actor_user_id=None,
            guest_exec_target_vmid=body.vmid,
            guest_exec_target_node=body.node,
        )
    )
    db.commit()
