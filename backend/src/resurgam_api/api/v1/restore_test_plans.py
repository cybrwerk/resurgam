import time
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from resurgam_api import encryption_keys
from resurgam_api.api.v1._connection_helpers import upstream_error
from resurgam_api.auth.dependency import extract_bearer_token, get_current_user
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.scope import require_restore_access_for_customer, visible_customer_ids
from resurgam_api.config import Settings, get_settings
from resurgam_api.connectivity.models import TrustConfig
from resurgam_api.connectivity.pve_api import (
    PveWriteError,
    agent_network_get_interfaces,
    agent_ping,
    apply_sdn_changes,
    check_vmid_free,
    create_pool,
    create_sdn_vnet,
    create_sdn_zone,
    delete_sdn_vnet,
    destroy_vm,
    get_pool_members,
    get_sdn_zone,
    get_task_status,
    list_guests,
    next_free_vmid,
    stop_task,
    stop_vm,
)
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.lab_setup.uplink import (
    PURPOSE_LAB_RUN,
    allocate_uplink_ip,
    overlapping_lab_subnets,
    release_uplink_ip,
    uplink_config_from_cluster,
)
from resurgam_api.models.application_group import ApplicationGroup, ApplicationGroupMember
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.inventory import BackupGroup, BackupSnapshot
from resurgam_api.models.pbs import TlsTrustMode
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
    generate_appliance_run_token,
    generate_vnet_name,
    hash_appliance_run_token,
    member_probe_plan,
    merge_probe_results,
    next_startable_member,
    probe_outcome_to_member_status,
    render_appliance_cloud_init_snippet,
    verify_appliance_run_token,
    zone_requires_single_node,
)
from resurgam_api.restore_test.lab_readiness import DEFAULT_LAB_ZONE_NAME
from resurgam_api.schemas.restore_test import (
    ProbeResultSubmission,
    ProbeTargetOut,
    RestoreTestPlanCreateRequest,
    RestoreTestPlanExecuteOut,
    RestoreTestPlanOut,
)
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

router = APIRouter(prefix="/lab-runs", tags=["recovery-lab"])

# PVE SDN zone ids are hard-limited (confirmed live 2026-09-19: a real
# `resurgam-lab` attempt 400'd -- "value does not match the regex
# pattern") to 2-8 alphanumeric chars starting with a letter, no hyphens
# -- the same constraint already applied to `generate_vnet_name()`. Pool
# ids are not similarly restricted (real `pve-poolid` format), so the
# more readable name is kept there.
# Shared with the Lab Compute readiness check (one source of truth).
DEFAULT_ZONE_NAME = DEFAULT_LAB_ZONE_NAME


def _record_audit(
    db: Session, request: Request, *, actor_user_id, action: str, target_id, customer_id=None, detail=None
) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            target_type="restore_test_plan",
            target_id=str(target_id),
            customer_id=customer_id,
            correlation_id=getattr(request.state, "correlation_id", None),
            detail=detail,
        )
    )


def _cluster_trust_and_token(db: Session, settings: Settings, cluster: PveCluster) -> tuple[TrustConfig, str]:
    from resurgam_api.models.secrets import SecretReference

    secret_ref = db.get(SecretReference, cluster.api_token_secret_ref_id)
    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    token = provider.retrieve(secret_ref.reference_key)
    trust = TrustConfig(
        trust_mode=TlsTrustMode(cluster.tls_trust_mode), fingerprint_sha256=cluster.tls_fingerprint_sha256
    )
    return trust, token


def _get_plan_with_access(
    db: Session, plan_id: uuid.UUID, user: AuthenticatedUser, *, for_update: bool = False
) -> RestoreTestPlan:
    query = db.query(RestoreTestPlan).filter_by(id=plan_id)
    if for_update:
        query = query.with_for_update()
    plan = query.one_or_none()
    if plan is None:
        raise ResurgamApiError(code="not_found", message="Recovery Lab run not found.", status_code=404)
    visible = visible_customer_ids(user)
    if visible is not None and plan.customer_id not in visible:
        raise ResurgamApiError(code="not_found", message="Recovery Lab run not found.", status_code=404)
    require_restore_access_for_customer(user, plan.customer_id)
    return plan


_MAX_VMID_PROBES = 1000


def _next_available_vmid(cluster: PveCluster, trust, token, timeout: float, candidate: int) -> int:
    """`next_free_vmid()` only guarantees the *one* id it returns is free
    -- it says nothing about subsequent ids. For an Application Group
    with more than one member (this feature's core use case), every
    target_vmid past the first must be independently re-checked, not
    just locally incremented, since an unrelated guest could already
    occupy `candidate` on this cluster.
    """
    for _ in range(_MAX_VMID_PROBES):
        check = check_vmid_free(cluster.host, cluster.port, trust, token, candidate, timeout=timeout)
        if not check.reachable:
            raise upstream_error("pve", check.error)
        if check.free:
            return candidate
        candidate += 1
    raise ResurgamApiError(code="no_free_vmid", message="Could not find a free VMID on this cluster.", status_code=502)


def members_ordered(db: Session, plan: RestoreTestPlan) -> list[RestoreTestPlanMember]:
    rows = (
        db.query(RestoreTestPlanMember, ApplicationGroupMember)
        .join(ApplicationGroupMember, RestoreTestPlanMember.application_group_member_id == ApplicationGroupMember.id)
        .filter(RestoreTestPlanMember.restore_test_plan_id == plan.id)
        .order_by(ApplicationGroupMember.startup_order)
        .all()
    )
    return [m for m, _ in rows]


def create_plan_for_group(
    db: Session,
    settings: Settings,
    group: ApplicationGroup,
    *,
    pve_cluster_id: uuid.UUID,
    target_node: str,
    target_storage: str,
    created_by_user_id: uuid.UUID | None,
) -> tuple[RestoreTestPlan, int]:
    """The actual "review the group, resolve snapshots + a target VMID"
    logic behind `POST /lab-runs` -- extracted so
    `worker/recovery_lab_scheduler.py` can create a scheduled run without
    a `Request`/`AuthenticatedUser`. No access check (the endpoint's own
    `require_restore_access_for_customer` stays there -- a scheduled run
    reads `group.lab_schedule_*`, which is only ever set by someone who
    already had write access to the group), no audit/commit -- callers
    own those. Returns the plan and its member count (for the caller's
    own audit detail).
    """
    members = (
        db.query(ApplicationGroupMember)
        .filter_by(application_group_id=group.id)
        .order_by(ApplicationGroupMember.startup_order)
        .all()
    )
    if not members:
        raise ResurgamApiError(
            code="empty_application_group", message="This application group has no members.", status_code=400
        )

    cluster = db.get(PveCluster, pve_cluster_id)
    if cluster is None or cluster.disabled or not cluster.is_lab_compute:
        raise ResurgamApiError(
            code="not_a_lab_cluster", message="This PVE cluster is not enabled for Recovery Lab.", status_code=400
        )
    if cluster.lab_appliance_template_vmid is None:
        raise ResurgamApiError(
            code="lab_appliance_template_not_configured",
            message="This cluster has no Lab probe appliance template configured.",
            status_code=400,
        )

    for member in members:
        backup_group = db.get(BackupGroup, member.backup_group_id)
        if backup_group is None:
            raise ResurgamApiError(
                code="backup_group_not_found", message="A member's backup group no longer exists.", status_code=400
            )
        if backup_group.backup_type != "vm":
            raise ResurgamApiError(
                code="ct_not_yet_supported",
                message="Recovery Lab currently only supports VM members (CT support is a follow-up).",
                status_code=400,
            )

    trust, token = _cluster_trust_and_token(db, settings, cluster)
    timeout = settings.connectivity_timeout_seconds
    vmid_result = next_free_vmid(cluster.host, cluster.port, trust, token, timeout=timeout)
    if not vmid_result.reachable or vmid_result.vmid is None:
        raise upstream_error("pve", vmid_result.error)
    next_vmid = vmid_result.vmid

    plan = RestoreTestPlan(
        application_group_id=group.id,
        customer_id=group.customer_id,
        pve_cluster_id=cluster.id,
        vnet_name=generate_vnet_name(),
        status=RestoreTestPlanStatus.PLANNED.value,
        created_by_user_id=created_by_user_id,
    )
    db.add(plan)
    db.flush()

    for index, member in enumerate(members):
        backup_group = db.get(BackupGroup, member.backup_group_id)
        snapshot = (
            db.query(BackupSnapshot)
            .filter_by(backup_group_id=backup_group.id)
            .order_by(BackupSnapshot.snapshot_time.desc())
            .first()
        )
        if snapshot is None:
            raise ResurgamApiError(
                code="no_snapshot_available",
                message=f"Backup group {backup_group.backup_id} has no snapshots to restore.",
                status_code=400,
            )
        encryption_keys.ensure_key_for_snapshot(db, snapshot)
        # The first member's vmid is already confirmed free by
        # next_free_vmid() above; every subsequent one (and the
        # appliance, below) must be independently re-checked.
        if index > 0:
            next_vmid = _next_available_vmid(cluster, trust, token, timeout, next_vmid)
        db.add(
            RestoreTestPlanMember(
                restore_test_plan_id=plan.id,
                application_group_member_id=member.id,
                backup_snapshot_id=snapshot.id,
                target_vmid=next_vmid,
                target_node=target_node,
                target_storage=target_storage,
                member_status=RestoreTestPlanMemberStatus.PENDING.value,
            )
        )
        next_vmid += 1
    plan.appliance_vmid = _next_available_vmid(cluster, trust, token, timeout, next_vmid)

    return plan, len(members)


@router.post("", response_model=RestoreTestPlanOut)
def create_restore_test_plan(
    body: RestoreTestPlanCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> RestoreTestPlanOut:
    """Manifest EPIC 10 (§22): reviews an Application Group and resolves
    each member's most recent snapshot + a target VMID -- nothing runs
    yet, matching Guided Restore's own create/execute split (ADR 0012).
    """
    group = db.get(ApplicationGroup, body.application_group_id)
    if group is None:
        raise ResurgamApiError(code="not_found", message="Application group not found.", status_code=404)
    require_restore_access_for_customer(user, group.customer_id)

    plan, member_count = create_plan_for_group(
        db,
        settings,
        group,
        pve_cluster_id=body.pve_cluster_id,
        target_node=body.target_node,
        target_storage=body.target_storage,
        created_by_user_id=user.user_id,
    )

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="restore_test_plan_created",
        target_id=plan.id,
        customer_id=plan.customer_id,
        detail={"application_group_id": str(group.id), "member_count": member_count},
    )
    db.commit()
    db.refresh(plan)
    plan.members = members_ordered(db, plan)
    return RestoreTestPlanOut.model_validate(plan)


@router.get("", response_model=list[RestoreTestPlanOut])
def list_restore_test_plans(
    db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> list[RestoreTestPlanOut]:
    visible = visible_customer_ids(user)
    query = db.query(RestoreTestPlan).order_by(RestoreTestPlan.created_at.desc())
    if visible is not None:
        query = query.filter(RestoreTestPlan.customer_id.in_(visible))
    plans = query.all()
    out = []
    for plan in plans:
        plan.members = members_ordered(db, plan)
        out.append(RestoreTestPlanOut.model_validate(plan))
    return out


@router.get("/{plan_id}", response_model=RestoreTestPlanOut)
def get_restore_test_plan(
    plan_id: uuid.UUID, db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> RestoreTestPlanOut:
    plan = _get_plan_with_access(db, plan_id, user)
    plan.members = members_ordered(db, plan)
    return RestoreTestPlanOut.model_validate(plan)


def _ensure_zone(cluster, trust, token, timeout) -> tuple[str, str | None]:
    """Returns (zone_name, zone_type). Uses the cluster's admin-configured
    zone if set (never created/modified by RESURGAM); otherwise ensures
    RESURGAM's own default Simple zone exists, creating it if needed.
    """
    if cluster.lab_sdn_zone_name:
        zone = get_sdn_zone(cluster.host, cluster.port, trust, token, timeout, name=cluster.lab_sdn_zone_name)
        if zone is None:
            raise ResurgamApiError(
                code="lab_sdn_zone_not_found",
                message=f"Configured SDN zone {cluster.lab_sdn_zone_name!r} does not exist on this cluster.",
                status_code=502,
            )
        return cluster.lab_sdn_zone_name, zone.get("type")

    zone = get_sdn_zone(cluster.host, cluster.port, trust, token, timeout, name=DEFAULT_ZONE_NAME)
    if zone is None:
        try:
            create_sdn_zone(
                cluster.host, cluster.port, trust, token, timeout, name=DEFAULT_ZONE_NAME, zone_type="simple"
            )
        except PveWriteError as exc:
            # Two concurrent first-ever lab runs on this cluster can both
            # see the zone as missing and both try to create it -- the
            # same real race `_ensure_pool` already tolerates below for
            # pool creation.
            if "already exists" not in str(exc):
                raise
        apply_sdn_changes(cluster.host, cluster.port, trust, token, timeout)
        return DEFAULT_ZONE_NAME, "simple"
    return DEFAULT_ZONE_NAME, zone.get("type")


def _ensure_pool(host, port, trust, token, timeout) -> None:
    try:
        create_pool(host, port, trust, token, timeout, poolid=DEFAULT_POOL_NAME, comment="RESURGAM Recovery Lab")
    except PveWriteError as exc:
        if "already exists" not in str(exc):
            raise


@router.post("/{plan_id}/execute", response_model=RestoreTestPlanExecuteOut)
def execute_restore_test_plan(
    plan_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> RestoreTestPlanExecuteOut:
    """Provisions this run's isolation (zone/pool/VNet) and queues the
    first eligible Application Group member (manifest §22.4's startup
    order) for `resurgam-lab-agent` to pick up (ADR 0019 -- a zero-copy
    `proxmox-backup-client map` + direct VM-config attach, not a PVE-API
    `qmrestore` call). `poll` queues subsequent members once each
    predecessor's agent-reported status clears its gate.

    The probe appliance is **not** cloned here. PVE has no REST endpoint
    to upload a cloud-init snippet file (confirmed live 2026-09-21 --
    `content=snippets` is rejected by the upload API's own parameter
    enum), so its per-run backend URL/token/router config is queued as
    `appliance_snippet_pending` for `resurgam-lab-agent` to write
    directly to the cluster's local snippet storage path (host-local
    work, matching ADR 0019's own agent/backend split). The clone itself
    happens once the agent confirms the write
    (`POST /lab-agent/appliance-jobs/{plan_id}/write-snippet`,
    `api/v1/lab_agent.py`) -- this run sits in `PROVISIONING_APPLIANCE`
    until then.
    """
    plan = _get_plan_with_access(db, plan_id, user, for_update=True)
    if plan.status != RestoreTestPlanStatus.PLANNED.value:
        raise ResurgamApiError(code="already_executed", message="This run has already been executed.", status_code=409)

    members = members_ordered(db, plan)
    run_token = provision_and_execute_plan(db, settings, plan, members)

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="restore_test_plan_execution_started",
        target_id=plan.id,
        customer_id=plan.customer_id,
        detail={"member_id": str(members[0].id)},
    )
    db.commit()
    db.refresh(plan)
    plan.members = members
    return RestoreTestPlanExecuteOut(plan=RestoreTestPlanOut.model_validate(plan), appliance_run_token=run_token)


def provision_and_execute_plan(
    db: Session, settings: Settings, plan: RestoreTestPlan, members: list[RestoreTestPlanMember]
) -> str:
    """The actual isolation-provisioning + first-member-queueing logic
    behind `POST /{plan_id}/execute` -- extracted so
    `worker/recovery_lab_scheduler.py` can execute a scheduled run
    without a `Request`/`AuthenticatedUser`. No audit/commit -- callers
    own those. Returns the appliance run token (only ever shown once,
    same as the endpoint's own response -- the scheduler's caller logs it
    to the audit trail's `detail`, since there's no human session to
    hand it to directly).
    """
    cluster = db.get(PveCluster, plan.pve_cluster_id)
    if cluster is None:
        raise ResurgamApiError(code="not_found", message="PVE cluster not found.", status_code=404)
    if cluster.lab_snippet_storage_id is None or cluster.lab_appliance_uplink_bridge is None:
        raise ResurgamApiError(
            code="lab_appliance_network_not_configured",
            message="This cluster has no Lab snippet storage or appliance uplink bridge configured.",
            status_code=400,
        )
    if not settings.recovery_lab_public_backend_url:
        raise ResurgamApiError(
            code="recovery_lab_backend_url_not_configured",
            message="This deployment has no Recovery Lab public backend URL configured.",
            status_code=400,
        )
    group = db.get(ApplicationGroup, plan.application_group_id)
    if group.lab_subnet_cidr is None or group.lab_gateway_ip is None:
        raise ResurgamApiError(
            code="lab_network_not_configured",
            message="This Application Group has no Lab network (subnet/gateway) configured.",
            status_code=400,
        )
    # The appliance's uplink: refuse a lab subnet that overlaps the static
    # uplink subnet (the appliance routes between the two), and reserve this
    # run's uplink address before anything is created on PVE -- an exhausted
    # range must fail the run up front, not leave a half-built VNet.
    uplink = uplink_config_from_cluster(cluster)
    overlaps = overlapping_lab_subnets(uplink, [(group.name, group.lab_subnet_cidr)])
    if overlaps:
        raise ResurgamApiError(
            code="lab_subnet_overlaps_uplink",
            message=f"The Application Group's lab subnet {group.lab_subnet_cidr} overlaps this cluster's probe "
            f"appliance uplink subnet {uplink.network()}. Change one of them (Application Group lab network, or "
            "the uplink range in the Lab Compute setup).",
            status_code=400,
        )
    allocate_uplink_ip(db, cluster, purpose=PURPOSE_LAB_RUN, restore_test_plan_id=plan.id)

    trust, token = _cluster_trust_and_token(db, settings, cluster)
    timeout = settings.connectivity_timeout_seconds

    zone_name, zone_type = _ensure_zone(cluster, trust, token, timeout)
    if zone_requires_single_node(zone_type):
        distinct_nodes = {m.target_node for m in members}
        if len(distinct_nodes) > 1:
            raise ResurgamApiError(
                code="single_node_zone_requires_shared_node",
                message="This cluster's SDN zone does not span nodes; every member must target the same node.",
                status_code=400,
            )

    _ensure_pool(cluster.host, cluster.port, trust, token, timeout)
    create_sdn_vnet(cluster.host, cluster.port, trust, token, timeout, vnet_name=plan.vnet_name, zone_name=zone_name)
    apply_sdn_changes(cluster.host, cluster.port, trust, token, timeout)

    run_token = generate_appliance_run_token()
    plan.appliance_run_token_hash = hash_appliance_run_token(run_token)
    snippet_content = render_appliance_cloud_init_snippet(
        backend_url=settings.recovery_lab_public_backend_url,
        plan_id=plan.id,
        run_token=run_token,
        gateway_ip=group.lab_gateway_ip,
        subnet_cidr=group.lab_subnet_cidr,
        dns_ip=group.lab_dns_ip,
        dhcp_range_start=group.lab_dhcp_range_start,
        dhcp_range_end=group.lab_dhcp_range_end,
    )
    plan.appliance_snippet_pending = {"filename": f"resurgam-lab-{plan.id.hex[:12]}.yaml", "content": snippet_content}
    plan.status = RestoreTestPlanStatus.PROVISIONING_APPLIANCE.value

    # All members are PENDING at execute time, so the first-by-startup_order
    # member (`members_ordered` already sorts this way) is always the one
    # `next_startable_member` would pick -- no need to call it here.
    #
    # ADR 0019: no PVE restore call happens for members anymore -- queue
    # the first one for `resurgam-lab-agent` to pick up via
    # `GET /lab-agent/jobs`. Member restoration doesn't depend on the
    # appliance being up yet, so this proceeds in parallel with the
    # deferred appliance provisioning above.
    members[0].member_status = RestoreTestPlanMemberStatus.QUEUED.value
    members[0].started_at = datetime.now(UTC)

    plan.requested_at = datetime.now(UTC)
    return run_token


@router.post("/{plan_id}/poll", response_model=RestoreTestPlanOut)
def poll_restore_test_plan(
    plan_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> RestoreTestPlanOut:
    """Queues the next eligible member in `startup_order` once the
    current one has cleared its gate (ADR 0019: `resurgam-lab-agent`
    itself drives a queued member from `QUEUED` through `MOUNTING` to
    `PROBING`/`FAILED` via `api/v1/lab_agent.py` -- `poll` no longer
    talks to PVE directly for member restoration, only decides *when*
    the next member becomes the agent's job). Mirrors
    `poll_restore_plan_execution`'s pull-triggered, idempotent shape.

    Also drives Level 1 (`guest_agent`) and IP resolution for every
    `PROBING` member (Recovery Lab Phase B): both are PVE-REST-proxied
    guest-agent management-channel calls, not network-path operations,
    so they stay backend-driven even though Level 2/3 checks are
    delegated to the probe appliance (see `restore_test/execution.py`'s
    module docstring and `member_probe_plan`).
    """
    plan = _get_plan_with_access(db, plan_id, user)
    members = members_ordered(db, plan)
    advance_restore_test_plan(db, settings, plan, members)

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="restore_test_plan_polled",
        target_id=plan.id,
        customer_id=plan.customer_id,
        detail={"status": plan.status},
    )
    db.commit()
    db.refresh(plan)
    plan.members = members
    return RestoreTestPlanOut.model_validate(plan)


def advance_restore_test_plan(
    db: Session, settings: Settings, plan: RestoreTestPlan, members: list[RestoreTestPlanMember]
) -> None:
    """The actual "one poll cycle" logic behind `POST /{plan_id}/poll`,
    extracted so `worker/recovery_lab_scheduler.py` can drive it
    automatically instead of relying solely on the frontend's manual
    "Poll now" button (raised directly by the user: Recovery Lab runs
    only ever advanced when someone was watching and clicking). No
    `Request`/user/audit/commit here -- callers own those, same split
    `_advance_member_probing` below already models within this endpoint.
    """
    # While an appliance snippet is still queued for the agent, this
    # run's member/appliance progress is driven by other callbacks
    # (`lab_agent.py`'s job endpoints) -- `poll` only advances the
    # member-queueing/aggregate-status machinery once past that stage.
    #
    # A run that was never executed (`PLANNED`) has no isolated VNet, no
    # probe appliance and no uplink address yet: polling it must not queue a
    # member. Found live 2026-11-21 -- an execute refused with
    # `lab_network_not_configured`, then one poll queued the first member and
    # the agent created its VM on a VNet that never existed.
    if plan.status in (
        RestoreTestPlanStatus.PLANNED.value,
        RestoreTestPlanStatus.PROVISIONING_APPLIANCE.value,
        RestoreTestPlanStatus.FAILED.value,
    ):
        return

    probing_members = [m for m in members if m.member_status == RestoreTestPlanMemberStatus.PROBING.value]
    if probing_members:
        cluster = db.get(PveCluster, plan.pve_cluster_id)
        trust, token = _cluster_trust_and_token(db, settings, cluster)
        timeout = settings.connectivity_timeout_seconds
        for member in probing_members:
            _advance_member_probing(db, member, cluster, trust, token, timeout)

    next_member = next_startable_member(
        [{"startup_order": i, "member_status": m.member_status} for i, m in enumerate(members)]
    )
    if next_member is not None:
        target = members[next_member["startup_order"]]
        target.member_status = RestoreTestPlanMemberStatus.QUEUED.value
        target.started_at = datetime.now(UTC)

    plan.status = aggregate_run_status([m.member_status for m in members])


# How long Level 1 keeps asking a member's guest agent before it counts as
# failed. A member boots straight from its backup, so a first boot takes
# minutes (Windows 11 from a PBS on iSCSI: about 11 min to the lock screen,
# measured 2026-11-20). Found live 2026-11-21: with a single ping on the
# first poll (about 15 s after the start), Level 1 failed for every guest.
GUEST_AGENT_GRACE = timedelta(minutes=15)


def _advance_member_probing(
    db: Session, member: RestoreTestPlanMember, cluster: PveCluster, trust, token, timeout
) -> None:
    """One `PROBING` member's Level 1 (`guest_agent`) + IP-resolution
    step, called once per `poll`. Finalizes the member immediately if
    Level 1 was requested and fails (nothing further can pass), or once
    every requested check type has a result (a member requesting only
    already-resolved tiers, e.g. `guest_agent` alone).
    """
    group_member = db.get(ApplicationGroupMember, member.application_group_member_id)
    check_types = group_member.check_types

    if member.resolved_ip is None:
        if group_member.expected_ip:
            member.resolved_ip = group_member.expected_ip
        else:
            ips = agent_network_get_interfaces(
                cluster.host, cluster.port, trust, token, timeout, node=member.target_node, vmid=member.target_vmid
            )
            if ips:
                member.resolved_ip = ips[0]

    plan_step = member_probe_plan(check_types, member.probe_results, member.resolved_ip)
    if plan_step.needs_guest_agent_check:
        reachable = agent_ping(
            cluster.host, cluster.port, trust, token, timeout, node=member.target_node, vmid=member.target_vmid
        )
        now = datetime.now(UTC)
        if not reachable and member.started_at is not None and now - member.started_at < GUEST_AGENT_GRACE:
            return  # still booting -- ask again on the next poll
        result = {
            "check_type": "guest_agent",
            "status": "pass" if reachable else "fail",
            "checked_at": now.isoformat(),
        }
        if not reachable:
            result["detail"] = (
                f"The guest agent did not answer within {int(GUEST_AGENT_GRACE.total_seconds() // 60)} minutes "
                "of the start."
            )
        member.probe_results = merge_probe_results(member.probe_results, [result])
        plan_step = member_probe_plan(check_types, member.probe_results, member.resolved_ip)

    if plan_step.is_complete:
        member.member_status = probe_outcome_to_member_status(member.probe_results)
        member.completed_at = datetime.now(UTC)


@router.post("/{plan_id}/probe-results", response_model=RestoreTestPlanOut)
def submit_probe_results(
    plan_id: uuid.UUID,
    body: ProbeResultSubmission,
    request: Request,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> RestoreTestPlanOut:
    """The probe appliance's callback -- authenticated by this run's own
    bearer token (see `restore_test/execution.py`'s hashing), never a
    user session. A token only authenticates against the *exact* run it
    was issued for.
    """
    plan = db.get(RestoreTestPlan, plan_id)
    if plan is None or plan.appliance_run_token_hash is None:
        raise ResurgamApiError(code="not_found", message="Recovery Lab run not found.", status_code=404)

    token = extract_bearer_token(authorization)
    if token is None:
        raise ResurgamApiError(code="unauthorized", message="Missing bearer token.", status_code=401)
    if not verify_appliance_run_token(token, plan.appliance_run_token_hash):
        raise ResurgamApiError(code="unauthorized", message="Invalid token for this run.", status_code=401)

    member = db.get(RestoreTestPlanMember, body.member_id)
    if member is None or member.restore_test_plan_id != plan.id:
        raise ResurgamApiError(code="not_found", message="Run member not found.", status_code=404)

    # Merge, not replace: the backend also writes into this same list
    # (the guest_agent tier, via `poll`'s `_advance_member_probing`) --
    # a wholesale overwrite here would lose that entry.
    incoming = [r.model_dump(mode="json") for r in body.results]
    member.probe_results = merge_probe_results(member.probe_results, incoming)

    group_member = db.get(ApplicationGroupMember, member.application_group_member_id)
    plan_step = member_probe_plan(group_member.check_types, member.probe_results, member.resolved_ip)
    if plan_step.is_complete:
        member.member_status = probe_outcome_to_member_status(member.probe_results)
        member.completed_at = datetime.now(UTC)

    members = members_ordered(db, plan)
    plan.status = aggregate_run_status([m.member_status for m in members])
    _record_audit(
        db,
        request,
        actor_user_id=None,
        action="restore_test_plan_probe_results_received",
        target_id=plan.id,
        customer_id=plan.customer_id,
        detail={"member_id": str(member.id), "member_status": member.member_status},
    )
    db.commit()
    db.refresh(plan)
    plan.members = members
    return RestoreTestPlanOut.model_validate(plan)


@router.get("/{plan_id}/probe-targets", response_model=list[ProbeTargetOut])
def list_probe_targets(
    plan_id: uuid.UUID,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> list[ProbeTargetOut]:
    """The probe appliance's own job list -- every `PROBING` member with
    a resolved IP and at least one still-pending Level 2/3 (network-tier)
    check. Authenticated the same way as `probe-results`, this run's own
    per-run bearer token. `guest_agent` never appears here -- that tier
    is always resolved backend-side (`poll`'s `_advance_member_probing`),
    not by the appliance.
    """
    plan = db.get(RestoreTestPlan, plan_id)
    if plan is None or plan.appliance_run_token_hash is None:
        raise ResurgamApiError(code="not_found", message="Recovery Lab run not found.", status_code=404)

    token = extract_bearer_token(authorization)
    if token is None:
        raise ResurgamApiError(code="unauthorized", message="Missing bearer token.", status_code=401)
    if not verify_appliance_run_token(token, plan.appliance_run_token_hash):
        raise ResurgamApiError(code="unauthorized", message="Invalid token for this run.", status_code=401)

    targets: list[ProbeTargetOut] = []
    for member in members_ordered(db, plan):
        if member.member_status != RestoreTestPlanMemberStatus.PROBING.value or member.resolved_ip is None:
            continue
        group_member = db.get(ApplicationGroupMember, member.application_group_member_id)
        plan_step = member_probe_plan(group_member.check_types, member.probe_results, member.resolved_ip)
        if plan_step.ready_for_appliance:
            targets.append(
                ProbeTargetOut(member_id=member.id, ip=member.resolved_ip, check_types=plan_step.network_checks_pending)
            )
    return targets


def _destroy_lab_vm(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, node: str, vmid: int, label: str
) -> str | None:
    """Stops (if running) then destroys one Recovery Lab VM, polling the
    stop task to completion first -- PVE rejects deleting a running VM.
    Returns an error string on failure, `None` on success. The caller
    must have already confirmed `vmid` is a real `resurgam-lab` pool
    member before calling this -- this function itself does not check.
    """
    try:
        stop_upid = stop_vm(host, port, trust, token, timeout, node=node, vmid=vmid)
        if stop_upid is None:
            return None  # already gone -- e.g. a member whose restore never ran
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            task = get_task_status(host, port, trust, token, timeout, node=node, upid=stop_upid)
            if not task.reachable:
                return f"Could not poll {label} (vmid {vmid})'s stop task: {task.error}"
            if task.status == "stopped":
                break
            time.sleep(1)
        else:
            return f"{label} (vmid {vmid}) did not stop within 60s"
        destroy_vm(host, port, trust, token, timeout, node=node, vmid=vmid)
    except PveWriteError as exc:
        return f"Could not destroy {label} (vmid {vmid}): {exc}"
    return None


@router.post("/{plan_id}/cleanup", response_model=RestoreTestPlanOut)
def cleanup_restore_test_plan(
    plan_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> RestoreTestPlanOut:
    """Tears down this run: stops any in-flight restore tasks, destroys
    the restored member VM(s) and the probe appliance VM, then deletes
    the run's VNet and applies the SDN reload -- deliberately in that
    order (VMs first, network last), raised directly by the user: the
    network must not be pulled out from under VMs that are still
    running. Idempotent -- safe to call repeatedly, same acceptance as
    `poll_restore_plan_execution`'s pull-driven design (ADR 0014).

    Guardrail (raised directly by the user): a VM is only ever destroyed
    once confirmed a real member of the `resurgam-lab` pool
    (`get_pool_members`) -- a stored `target_vmid`/`appliance_vmid` is
    never trusted blindly, since a bug or stale data there would
    otherwise mean destroying whatever real VM happens to sit at that
    vmid today.
    """
    plan = _get_plan_with_access(db, plan_id, user)
    members = members_ordered(db, plan)
    cluster = db.get(PveCluster, plan.pve_cluster_id)
    if cluster is None:
        raise ResurgamApiError(code="not_found", message="PVE cluster not found.", status_code=404)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    timeout = settings.connectivity_timeout_seconds

    cleanup_errors: list[str] = []
    for member in members:
        if member.member_status == RestoreTestPlanMemberStatus.RESTORE_REQUESTED.value and member.upid:
            try:
                stop_task(cluster.host, cluster.port, trust, token, timeout, node=member.target_node, upid=member.upid)
            except PveWriteError as exc:
                cleanup_errors.append(f"Could not stop member {member.id}'s in-flight task: {exc}")

    to_destroy: list[tuple[int, str, str]] = [  # (vmid, node, label)
        (member.target_vmid, member.target_node, f"member {member.id}") for member in members
    ]
    if plan.appliance_vmid is not None and members:
        to_destroy.append((plan.appliance_vmid, members[0].target_node, "probe appliance"))

    appliance_may_remain = False
    if to_destroy:
        pool_result = get_pool_members(cluster.host, cluster.port, trust, token, timeout, poolid=DEFAULT_POOL_NAME)
        if not pool_result.reachable:
            appliance_may_remain = plan.appliance_vmid is not None
            cleanup_errors.append(
                f"Could not verify {DEFAULT_POOL_NAME!r} pool membership -- no VMs were destroyed: {pool_result.error}"
            )
        else:
            member_vmids = pool_result.member_vmids or frozenset()
            # A vmid already destroyed by an earlier cleanup pass (this
            # endpoint is explicitly idempotent/repeatable) is *also*
            # absent from the pool -- a destroyed VM can't be a member of
            # anything. That's a benign no-op, not the guardrail's real
            # target (a real, unrelated VM sitting at that vmid), so it
            # must not produce the same scary "refused" error. Only
            # `list_guests` (does this vmid exist on PVE *at all* right
            # now) can tell the two apart -- raised directly by the user
            # after a real second cleanup pass on an already-cleaned-up
            # run showed a "Refused to destroy" error for vmids that had,
            # in fact, already been correctly destroyed.
            existing_result = list_guests(cluster.host, cluster.port, trust, token, timeout)
            # `existing_result.guests == []` is a real, meaningful answer
            # (the cluster genuinely has zero guests right now) -- must
            # not be conflated with `reachable=False`'s "couldn't confirm
            # anything" case, which is exactly why this used a falsy
            # check on `.guests` before and treated an empty list the
            # same as unreachable.
            existing_vmids = {g.vmid for g in (existing_result.guests or [])} if existing_result.reachable else None
            for vmid, node, label in to_destroy:
                if vmid in member_vmids:
                    error = _destroy_lab_vm(
                        cluster.host, cluster.port, trust, token, timeout, node=node, vmid=vmid, label=label
                    )
                    if error is not None:
                        cleanup_errors.append(error)
                        appliance_may_remain = appliance_may_remain or label == "probe appliance"
                elif existing_vmids is not None and vmid not in existing_vmids:
                    continue  # already gone -- a previous cleanup pass already destroyed it
                else:
                    # Either confirmed to still exist outside the pool
                    # (the real danger case), or existence itself
                    # couldn't be confirmed -- refuse in both cases
                    # rather than guess.
                    appliance_may_remain = appliance_may_remain or label == "probe appliance"
                    cleanup_errors.append(
                        f"Refused to destroy {label} (vmid {vmid}): not a member of the {DEFAULT_POOL_NAME!r} pool"
                    )

    try:
        delete_sdn_vnet(cluster.host, cluster.port, trust, token, timeout, vnet_name=plan.vnet_name)
        apply_sdn_changes(cluster.host, cluster.port, trust, token, timeout)
    except PveWriteError as exc:
        cleanup_errors.append(f"Cleanup could not remove the run's VNet: {exc}")

    # The run's uplink address (static range) is free again once the probe
    # appliance is gone; if its removal failed or could not be confirmed, the
    # address stays reserved until a later cleanup pass succeeds.
    if not appliance_may_remain:
        release_uplink_ip(db, restore_test_plan_id=plan.id)

    if cleanup_errors:
        # Every cleanup step's failure is visible on the plan, not just
        # whichever step happened to run last -- a stuck restore task and
        # a stuck VNet are both real, independently actionable problems.
        plan.error = " | ".join(cleanup_errors)

    # A real bug found live (2026-09-23, UX/CISO audit): this used to set
    # `plan.status = COMPLETED` unconditionally, discarding whatever real
    # outcome `poll` had already computed via `aggregate_run_status` --
    # a run whose members genuinely FAILED their probes (e.g. a Windows
    # guest's expected ICMP-blocked-by-firewall result) came out of
    # cleanup looking identical to a run that PASSED, both rendered as
    # the same green "Abgeschlossen"/"Completed" badge. The Recovery
    # Evidence report already reads the real per-member outcome
    # (`RestoreTestPlanMember.member_status`) and correctly showed RED --
    # only this operational list view was wrong. `COMPLETED` is only
    # the right label for "cleaned up before any real outcome existed"
    # (aborted from PLANNED/RUNNING); once a genuine PASSED/PARTIAL/
    # FAILED verdict exists, cleanup must preserve it, not overwrite it.
    # A plan already FAILED at the appliance-provisioning level (never
    # member-driven, see `report_lab_agent_job_status`'s own identical
    # guard) also stays FAILED here for the same reason.
    if plan.status != RestoreTestPlanStatus.FAILED.value:
        outcome = aggregate_run_status([m.member_status for m in members])
        plan.status = (
            outcome
            if outcome
            in (
                RestoreTestPlanStatus.PASSED.value,
                RestoreTestPlanStatus.PARTIAL.value,
                RestoreTestPlanStatus.FAILED.value,
            )
            else RestoreTestPlanStatus.COMPLETED.value
        )
    plan.completed_at = datetime.now(UTC)
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="restore_test_plan_cleaned_up",
        target_id=plan.id,
        customer_id=plan.customer_id,
    )
    db.commit()
    db.refresh(plan)
    plan.members = members
    return RestoreTestPlanOut.model_validate(plan)
