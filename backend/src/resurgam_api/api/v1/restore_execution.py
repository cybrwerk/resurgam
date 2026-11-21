import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from resurgam_api.api.v1._connection_helpers import upstream_error
from resurgam_api.api.v1.pve_clusters import _cluster_trust_and_token
from resurgam_api.api.v1.restore_plans import _get_snapshot_group_and_datastore
from resurgam_api.auth.dependency import get_current_user
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.scope import require_restore_access
from resurgam_api.config import Settings, get_settings
from resurgam_api.connectivity.pve_api import (
    PveWriteError,
    get_ct_config,
    get_task_status,
    get_vm_config,
    list_storages,
    set_guest_network_config,
    start_ct_restore,
    start_guest,
    start_vm_restore,
    stop_task,
)
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.instant_recovery import checks as ir_checks
from resurgam_api.instant_recovery.original_vm import RUNNING, UNKNOWN, find_original_vm
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.inventory import BackupGroup
from resurgam_api.models.pve import PveCluster
from resurgam_api.models.restore import (
    RestorePlan,
    RestorePlanApprovalStatus,
    RestorePlanExecutionStatus,
    RestorePlanMethod,
    RestorePlanNetworkMode,
    RestorePlanVmidIntent,
)
from resurgam_api.restore.command import pbs_volid
from resurgam_api.restore.execution import (
    build_restore_params,
    is_execution_stale,
    needs_network_finalize,
    needs_start_call,
    network_updates,
    next_execution_status,
)
from resurgam_api.schemas.restore import RestorePlanOut, RestorePlanRejectRequest

router = APIRouter(prefix="/restore-plans", tags=["restore-plans"])


def _record_audit(
    db: Session, request: Request, *, actor_user_id, action: str, target_id, customer_id=None, detail=None
) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            target_type="restore_plan",
            target_id=str(target_id),
            customer_id=customer_id,
            correlation_id=getattr(request.state, "correlation_id", None),
            detail=detail,
        )
    )


def _get_plan_with_restore_access(
    db: Session, plan_id: uuid.UUID, user: AuthenticatedUser, *, for_update: bool = False
) -> RestorePlan:
    """Unlike `restore_plans.py`'s `_get_plan_or_404` (viewer-level
    visibility via `visible_customer_ids`), every endpoint here actually
    changes state or starts real infrastructure work, so it requires the
    same `restore_operator`/`platform_admin` scoped access as plan
    creation -- resolved via the plan's own group, not its snapshotted
    `customer_id`. `for_update=True` (used only by `execute`) takes a row
    lock so a double-click can't fire two restores from two concurrent
    requests racing past the same "not yet executed" check.
    """
    query = db.query(RestorePlan).filter_by(id=plan_id)
    if for_update:
        query = query.with_for_update()
    plan = query.one_or_none()
    if plan is None:
        raise ResurgamApiError(code="not_found", message="Restore plan not found.", status_code=404)
    group = db.get(BackupGroup, plan.backup_group_id)
    require_restore_access(db, user, group)
    return plan


def _require_not_self_approval(plan: RestorePlan, user: AuthenticatedUser) -> None:
    """Manifest §11.2's four-eyes requirement only means something if the
    approver isn't the plan's own creator. Deliberately a plain 403, not
    the codebase's usual 404-for-denial -- that convention exists to avoid
    confirming a resource's existence to someone who shouldn't know it
    exists; the creator plainly already knows their own plan exists, so a
    normal 403 is the correct, distinct signal. Not exempted for
    `platform_admin` -- the likeliest accidental bypass of this check.
    """
    if plan.created_by_user_id == user.user_id:
        raise ResurgamApiError(
            code="self_approval_forbidden",
            message="You cannot approve or reject your own restore plan.",
            status_code=403,
        )


@router.post("/{plan_id}/approve", response_model=RestorePlanOut)
def approve_restore_plan(
    plan_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> RestorePlanOut:
    plan = _get_plan_with_restore_access(db, plan_id, user)
    _require_not_self_approval(plan, user)
    if plan.approval_status != RestorePlanApprovalStatus.PENDING.value:
        raise ResurgamApiError(
            code="approval_not_pending", message="This plan is not awaiting approval.", status_code=409
        )

    plan.approval_status = RestorePlanApprovalStatus.APPROVED.value
    plan.approved_by_user_id = user.user_id
    plan.approved_at = datetime.now(UTC)

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="restore_plan_approved",
        target_id=plan.id,
        customer_id=plan.customer_id,
    )
    db.commit()
    db.refresh(plan)
    return RestorePlanOut.model_validate(plan)


@router.post("/{plan_id}/reject", response_model=RestorePlanOut)
def reject_restore_plan(
    plan_id: uuid.UUID,
    body: RestorePlanRejectRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> RestorePlanOut:
    plan = _get_plan_with_restore_access(db, plan_id, user)
    _require_not_self_approval(plan, user)
    if plan.approval_status != RestorePlanApprovalStatus.PENDING.value:
        raise ResurgamApiError(
            code="approval_not_pending", message="This plan is not awaiting approval.", status_code=409
        )

    plan.approval_status = RestorePlanApprovalStatus.REJECTED.value
    plan.approved_by_user_id = user.user_id
    plan.approved_at = datetime.now(UTC)
    plan.approval_notes = body.reason

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="restore_plan_rejected",
        target_id=plan.id,
        customer_id=plan.customer_id,
        detail={"reason": body.reason},
    )
    db.commit()
    db.refresh(plan)
    return RestorePlanOut.model_validate(plan)


def _queue_instant_recovery(
    db: Session,
    request: Request,
    settings: Settings,
    user: AuthenticatedUser,
    plan: RestorePlan,
    group: BackupGroup,
    cluster: PveCluster,
) -> RestorePlanOut:
    """`method=instant` (ADR 0030): nothing is called on PVE here -- the plan is
    queued for the cluster's lab agent. The original VM is checked once more
    right now: it may have been started since the plan was created."""
    if not cluster.instant_recovery_enabled:
        raise ResurgamApiError(
            code="instant_recovery_disabled",
            message="Instant Recovery is not allowed on this cluster any more.",
            status_code=409,
        )
    original_vm = find_original_vm(db, settings, group)
    if plan.network_mode == RestorePlanNetworkMode.ORIGINAL.value:
        if original_vm.status == RUNNING:
            raise ResurgamApiError(
                code="original_vm_running",
                message=(
                    f"The original VM is running ({original_vm.cluster_name} / {original_vm.node}); it can't be "
                    "started a second time on the original network. Stop it first or create a plan with another "
                    "network mode."
                ),
                status_code=409,
            )
        if original_vm.status == UNKNOWN and not plan.network_danger_acknowledged:
            raise ResurgamApiError(
                code="network_acknowledgment_required",
                message="The original VM's state can't be checked right now; recheck and acknowledge the risk.",
                status_code=409,
            )
    plan.ir_original_vm = original_vm.as_dict()
    plan.ir_keep_mac = ir_checks.keep_mac(original_vm)
    now = datetime.now(UTC)
    plan.execution_status = RestorePlanExecutionStatus.QUEUED.value
    plan.execution_started_by_user_id = user.user_id
    plan.executed_at = now
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="instant_recovery_queued",
        target_id=plan.id,
        customer_id=plan.customer_id,
        detail={"original_vm": original_vm.as_dict(), "keep_mac": plan.ir_keep_mac, "network_mode": plan.network_mode},
    )
    db.commit()
    db.refresh(plan)
    return RestorePlanOut.model_validate(plan)


@router.post("/{plan_id}/execute", response_model=RestorePlanOut)
def execute_restore_plan(
    plan_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> RestorePlanOut:
    """Manifest §11.2: actually starts the restore via the PVE API. Only
    fires the initial restore call and stores the UPID -- polling to
    completion and the mandatory post-restore network/start finalize
    steps happen in `execution/poll` (see that endpoint's docstring for
    why this can't all happen inline here).
    """
    plan = _get_plan_with_restore_access(db, plan_id, user, for_update=True)

    if plan.approval_status == RestorePlanApprovalStatus.PENDING.value:
        raise ResurgamApiError(
            code="approval_pending", message="This plan requires approval before it can be executed.", status_code=409
        )
    if plan.approval_status == RestorePlanApprovalStatus.REJECTED.value:
        raise ResurgamApiError(code="approval_rejected", message="This plan was rejected.", status_code=409)
    if plan.execution_status is not None:
        raise ResurgamApiError(code="already_executed", message="This plan has already been executed.", status_code=409)

    snapshot, group, datastore = _get_snapshot_group_and_datastore(db, plan.backup_snapshot_id)
    cluster = db.get(PveCluster, plan.pve_cluster_id)
    if cluster is None or cluster.disabled:
        raise ResurgamApiError(code="not_found", message="PVE cluster not found.", status_code=404)

    if plan.method == RestorePlanMethod.INSTANT.value:
        return _queue_instant_recovery(db, request, settings, user, plan, group, cluster)

    trust, token = _cluster_trust_and_token(db, settings, cluster)
    timeout = settings.connectivity_timeout_seconds

    storages_result = list_storages(cluster.host, cluster.port, trust, token, plan.target_node, timeout=timeout)
    if not storages_result.reachable:
        raise upstream_error("pve", storages_result.error)
    pbs_storage_id = next(
        (s.storage for s in storages_result.storages if s.type == "pbs" and s.pbs_datastore == datastore.name),
        None,
    )
    if pbs_storage_id is None:
        # No silent fallback to `plan.target_storage` -- that's the
        # restore *destination* disk storage (e.g. local-lvm), not a PBS
        # storage; using it here would build a volid against the wrong
        # storage entirely instead of failing loudly.
        raise ResurgamApiError(
            code="pbs_storage_not_found",
            message=f"No PVE storage for PBS datastore {datastore.name!r} is configured on node {plan.target_node!r}.",
            status_code=502,
        )
    volid = pbs_volid(pbs_storage_id, group.backup_type, group.backup_id, snapshot.snapshot_time)
    network_mode = RestorePlanNetworkMode(plan.network_mode)

    params = build_restore_params(
        backup_type=group.backup_type,
        target_vmid=plan.target_vmid,
        volid=volid,
        target_storage=plan.target_storage,
        vmid_intent_is_new=plan.vmid_intent == RestorePlanVmidIntent.NEW.value,
        network_mode=network_mode,
        network_bridge=plan.network_bridge,
        temp_name=plan.temp_name,
    )

    # Flip status before firing the PVE call, while still holding the row
    # lock from `for_update=True` above -- a concurrent double-click blocks
    # here and then sees `execution_status is not None` once it proceeds.
    plan.execution_status = RestorePlanExecutionStatus.RESTORE_REQUESTED.value
    plan.execution_started_by_user_id = user.user_id
    plan.executed_at = datetime.now(UTC)
    db.flush()

    try:
        if group.backup_type == "vm":
            upid = start_vm_restore(cluster.host, cluster.port, trust, token, timeout, node=plan.target_node, **params)
        else:
            upid = start_ct_restore(cluster.host, cluster.port, trust, token, timeout, node=plan.target_node, **params)
    except PveWriteError as exc:
        plan.execution_status = RestorePlanExecutionStatus.RESTORE_FAILED.value
        plan.execution_error = str(exc)
        plan.execution_completed_at = datetime.now(UTC)
        _record_audit(
            db,
            request,
            actor_user_id=user.user_id,
            action="restore_plan_execution_failed",
            target_id=plan.id,
            customer_id=plan.customer_id,
            detail={"stage": "restore_start", "error": str(exc)},
        )
        db.commit()
        db.refresh(plan)
        return RestorePlanOut.model_validate(plan)

    plan.upid = upid
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="restore_plan_execution_started",
        target_id=plan.id,
        customer_id=plan.customer_id,
        detail={"upid": upid},
    )
    db.commit()
    db.refresh(plan)
    return RestorePlanOut.model_validate(plan)


@router.post("/{plan_id}/execution/abort", response_model=RestorePlanOut)
def abort_restore_plan_execution(
    plan_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> RestorePlanOut:
    """Stops a live PVE restore task mid-run (admin capability, raised
    directly by the user) -- a real, hard-to-reverse action: interrupting
    a `qmrestore`/`pct restore` partway through can leave the target
    guest's disk in a partially-restored state, which is why this is a
    separate, deliberate endpoint rather than folded into `poll`. Only
    valid while a PVE task is actually in flight (`restore_requested`/
    `restore_running` -- `finalizing` means the PVE-side restore itself
    already finished, nothing left to stop there). On success the plan is
    marked `restore_failed` with an explicit "aborted by an operator"
    message -- there's no separate "aborted" status; an aborted restore
    is a failed one, same as any other, and every existing UI/type
    already knows how to show a failed execution.
    """
    plan = _get_plan_with_restore_access(db, plan_id, user)

    if plan.execution_status not in (
        RestorePlanExecutionStatus.RESTORE_REQUESTED.value,
        RestorePlanExecutionStatus.RESTORE_RUNNING.value,
    ):
        raise ResurgamApiError(
            code="execution_not_running",
            message="This plan's restore is not currently running.",
            status_code=409,
        )

    cluster = db.get(PveCluster, plan.pve_cluster_id)
    if cluster is None:
        raise ResurgamApiError(code="not_found", message="PVE cluster not found.", status_code=404)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    timeout = settings.connectivity_timeout_seconds

    try:
        stop_task(cluster.host, cluster.port, trust, token, timeout, node=plan.target_node, upid=plan.upid)
    except PveWriteError as exc:
        raise ResurgamApiError(
            code="pve_unreachable", message=f"Could not stop the PVE task: {exc}", status_code=502
        ) from exc

    plan.execution_status = RestorePlanExecutionStatus.RESTORE_FAILED.value
    plan.execution_error = "Restore execution was aborted by an operator."
    plan.execution_completed_at = datetime.now(UTC)
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="restore_plan_execution_aborted",
        target_id=plan.id,
        customer_id=plan.customer_id,
        detail={"upid": plan.upid},
    )
    db.commit()
    db.refresh(plan)
    return RestorePlanOut.model_validate(plan)


@router.post("/{plan_id}/execution/poll", response_model=RestorePlanOut)
def poll_restore_plan_execution(
    plan_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> RestorePlanOut:
    """Live-polls the PVE task's status and, the first time it observes
    completion, performs the mandatory post-restore finalize step(s) in
    this same request -- pull-triggered and idempotent, not a background
    worker: starting a PVE restore returns near-instantly with just a
    UPID, so there's no reason to hold a request open or run a queue for
    it, but the finalize call (network lockdown, optional start) must not
    be inline in `execute` either, since that would block for as long as
    the restore itself takes. Safe to call repeatedly: once terminal
    (`completed`/`restore_failed`/`finalize_failed`), this is a no-op.
    """
    plan = _get_plan_with_restore_access(db, plan_id, user)

    if plan.execution_status is None:
        raise ResurgamApiError(code="not_started", message="This plan has not been executed yet.", status_code=409)
    if plan.method == RestorePlanMethod.INSTANT.value:
        # Instant Recovery is driven by the lab agent and the scheduler, not by
        # polling a PVE restore task -- just return the current state.
        return RestorePlanOut.model_validate(plan)
    if plan.execution_status in (
        RestorePlanExecutionStatus.COMPLETED.value,
        RestorePlanExecutionStatus.RESTORE_FAILED.value,
        RestorePlanExecutionStatus.FINALIZE_FAILED.value,
    ):
        return RestorePlanOut.model_validate(plan)

    cluster = db.get(PveCluster, plan.pve_cluster_id)
    if cluster is None:
        raise ResurgamApiError(code="not_found", message="PVE cluster not found.", status_code=404)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    timeout = settings.connectivity_timeout_seconds

    task_status = get_task_status(
        cluster.host, cluster.port, trust, token, timeout, node=plan.target_node, upid=plan.upid
    )
    if not task_status.reachable:
        # PVE's own answer, whenever we can get one, is always authoritative
        # -- a legitimately long-running restore is never force-failed by an
        # arbitrary clock just because time has passed. Staleness only
        # applies here, to a *prolonged inability to reach PVE at all*:
        # anchored on the last time we successfully got any answer (not on
        # `executed_at`), so a restore PVE was still confirming as running
        # five minutes ago isn't punished for one unlucky poll.
        last_known_good = plan.execution_last_confirmed_at or plan.executed_at
        if is_execution_stale(last_known_good, datetime.now(UTC), settings.restore_execution_stale_after_seconds):
            plan.execution_status = RestorePlanExecutionStatus.RESTORE_FAILED.value
            plan.execution_completed_at = datetime.now(UTC)
            plan.execution_error = (
                f"Could not reach PVE to confirm this restore's status for over "
                f"{settings.restore_execution_stale_after_seconds}s; marked as failed. "
                "The restore itself may have completed independently of this record -- check PVE directly."
            )
            _record_audit(
                db,
                request,
                actor_user_id=user.user_id,
                action="restore_plan_execution_failed",
                target_id=plan.id,
                customer_id=plan.customer_id,
                detail={"stage": "stale_unreachable"},
            )
            db.commit()
            db.refresh(plan)
            return RestorePlanOut.model_validate(plan)
        raise upstream_error("pve", task_status.error)

    plan.execution_last_confirmed_at = datetime.now(UTC)
    new_status = next_execution_status(plan.execution_status, task_status)
    plan.execution_status = new_status
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="restore_plan_execution_polled",
        target_id=plan.id,
        customer_id=plan.customer_id,
        detail={"pve_status": task_status.status, "exitstatus": task_status.exitstatus},
    )

    if new_status == RestorePlanExecutionStatus.RESTORE_FAILED.value:
        plan.execution_completed_at = datetime.now(UTC)
        plan.execution_exit_status = task_status.exitstatus
        plan.execution_error = f"PVE task failed: {task_status.exitstatus}"
        _record_audit(
            db,
            request,
            actor_user_id=user.user_id,
            action="restore_plan_execution_failed",
            target_id=plan.id,
            customer_id=plan.customer_id,
            detail={"stage": "restore", "exitstatus": task_status.exitstatus},
        )

    elif new_status == RestorePlanExecutionStatus.FINALIZING.value:
        _, group, _ = _get_snapshot_group_and_datastore(db, plan.backup_snapshot_id)
        network_mode = RestorePlanNetworkMode(plan.network_mode)
        guest_type = "qemu" if group.backup_type == "vm" else "lxc"
        try:
            if needs_network_finalize(group.backup_type, network_mode):
                # Read back the guest's real, just-restored NICs and change
                # every one of them (PVE has no partial-attribute update).
                read_config = get_vm_config if guest_type == "qemu" else get_ct_config
                current = read_config(
                    cluster.host, cluster.port, trust, token, timeout, node=plan.target_node, vmid=plan.target_vmid
                )
                set_guest_network_config(
                    cluster.host,
                    cluster.port,
                    trust,
                    token,
                    timeout,
                    node=plan.target_node,
                    vmid=plan.target_vmid,
                    guest_type=guest_type,
                    nets=network_updates(network_mode, plan.network_bridge, current),
                )
            if needs_start_call(plan.start_after_restore):
                start_guest(
                    cluster.host,
                    cluster.port,
                    trust,
                    token,
                    timeout,
                    node=plan.target_node,
                    vmid=plan.target_vmid,
                    guest_type=guest_type,
                )
            plan.execution_status = RestorePlanExecutionStatus.COMPLETED.value
            plan.execution_completed_at = datetime.now(UTC)
            plan.execution_exit_status = task_status.exitstatus
            _record_audit(
                db,
                request,
                actor_user_id=user.user_id,
                action="restore_plan_execution_finalized",
                target_id=plan.id,
                customer_id=plan.customer_id,
            )
        except PveWriteError as exc:
            plan.execution_status = RestorePlanExecutionStatus.FINALIZE_FAILED.value
            plan.execution_error = str(exc)
            _record_audit(
                db,
                request,
                actor_user_id=user.user_id,
                action="restore_plan_execution_failed",
                target_id=plan.id,
                customer_id=plan.customer_id,
                detail={"stage": "finalize", "error": str(exc)},
            )

    db.commit()
    db.refresh(plan)
    return RestorePlanOut.model_validate(plan)
