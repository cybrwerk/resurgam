import re
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from resurgam_api import encryption_keys
from resurgam_api.api.v1._connection_helpers import upstream_error
from resurgam_api.auth.dependency import get_current_user
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.scope import effective_group_customer_id, require_restore_access, visible_customer_ids
from resurgam_api.config import Settings, get_settings
from resurgam_api.connectivity.models import TrustConfig
from resurgam_api.connectivity.pve_api import check_vmid_free, list_guests, list_storages
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.instant_recovery import checks as ir_checks
from resurgam_api.instant_recovery.original_vm import find_original_vm
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.datastore import Datastore, DatastoreCustomerAssignment
from resurgam_api.models.inventory import BackupGroup, BackupSnapshot
from resurgam_api.models.pbs import PbsInstance, TlsTrustMode
from resurgam_api.models.pve import PveCluster, PveClusterPbsInstance
from resurgam_api.models.restore import (
    RestorePlan,
    RestorePlanApprovalStatus,
    RestorePlanMethod,
    RestorePlanNetworkMode,
    RestorePlanVmidIntent,
)
from resurgam_api.models.secrets import SecretReference
from resurgam_api.restore.checks import (
    check_name_conflict,
    check_pbs_storage_visibility,
    check_storage_capacity,
    check_vmid,
    classify_network_risk,
)
from resurgam_api.restore.command import generate_restore_command
from resurgam_api.schemas.restore import RestorePlanCreateRequest, RestorePlanOut
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

router = APIRouter(prefix="/restore-plans", tags=["restore-plans"])


def _slugify(value: str) -> str:
    """A PVE-safe name fragment (alphanumeric + hyphens only) from PBS's
    own reported guest name, for the default temp_name -- see the
    `name_base` comment at its call site.
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return slug or "restore"


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


def _get_snapshot_group_and_datastore(
    db: Session, snapshot_id: uuid.UUID
) -> tuple[BackupSnapshot, BackupGroup, Datastore]:
    snapshot = db.get(BackupSnapshot, snapshot_id)
    if snapshot is None:
        raise ResurgamApiError(code="not_found", message="Snapshot not found.", status_code=404)
    group = db.get(BackupGroup, snapshot.backup_group_id)
    datastore = db.get(Datastore, group.datastore_id)
    return snapshot, group, datastore


def _get_plan_or_404(db: Session, plan_id: uuid.UUID, user: AuthenticatedUser) -> RestorePlan:
    plan = db.get(RestorePlan, plan_id)
    if plan is None:
        raise ResurgamApiError(code="not_found", message="Restore plan not found.", status_code=404)
    visible = visible_customer_ids(user)
    if visible is not None and plan.customer_id not in visible:
        raise ResurgamApiError(code="not_found", message="Restore plan not found.", status_code=404)
    return plan


@router.post("", response_model=RestorePlanOut)
def create_restore_plan(
    body: RestorePlanCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> RestorePlanOut:
    """Manifest EPIC 8/§11.1: run live checks against the target PVE
    cluster and generate a reviewed, human-runnable restore command.
    **Nothing here executes a restore** — see `restore/command.py`.
    """
    snapshot, group, datastore = _get_snapshot_group_and_datastore(db, body.backup_snapshot_id)
    require_restore_access(db, user, group)
    instant = body.method == RestorePlanMethod.INSTANT

    if group.backup_type not in ("vm", "ct"):
        raise ResurgamApiError(
            code="unsupported_backup_type",
            message="Guided restore only supports VM and CT backups.",
            status_code=400,
        )
    if instant and group.backup_type != "vm":
        raise ResurgamApiError(
            code="instant_recovery_vm_only",
            message="Instant Recovery runs VMs only; restore a container with the full method.",
            status_code=400,
        )
    if instant:
        # Instant Recovery reads the backup through RESURGAM's lab agent, which
        # needs the backup's key (a full restore uses the PVE storage's own key).
        encryption_keys.ensure_key_for_snapshot(db, snapshot)
    if instant and body.vmid_intent != RestorePlanVmidIntent.NEW:
        raise ResurgamApiError(
            code="instant_recovery_needs_free_vmid",
            message="Instant Recovery creates a new VM; pick a free VMID (RESURGAM never replaces the original).",
            status_code=400,
        )

    cluster = db.get(PveCluster, body.pve_cluster_id)
    if cluster is None or cluster.disabled:
        raise ResurgamApiError(code="not_found", message="PVE cluster not found.", status_code=404)

    if body.network_mode == RestorePlanNetworkMode.CUSTOM and not body.network_bridge:
        raise ResurgamApiError(
            code="network_bridge_required",
            message="network_bridge is required for a custom network mode.",
            status_code=400,
        )

    secret_ref = db.get(SecretReference, cluster.api_token_secret_ref_id)
    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    token = provider.retrieve(secret_ref.reference_key)
    trust = TrustConfig(
        trust_mode=TlsTrustMode(cluster.tls_trust_mode), fingerprint_sha256=cluster.tls_fingerprint_sha256
    )
    timeout = settings.connectivity_timeout_seconds

    storages_result = list_storages(cluster.host, cluster.port, trust, token, body.target_node, timeout=timeout)
    guests_result = list_guests(cluster.host, cluster.port, trust, token, timeout=timeout)
    vmid_result = check_vmid_free(cluster.host, cluster.port, trust, token, body.target_vmid, timeout=timeout)

    if not storages_result.reachable or not guests_result.reachable:
        error = storages_result.error or guests_result.error
        raise upstream_error("pve", error)

    pbs_instance = db.get(PbsInstance, datastore.pbs_instance_id)
    is_linked = (
        db.query(PveClusterPbsInstance).filter_by(pve_cluster_id=cluster.id, pbs_instance_id=pbs_instance.id).first()
        is not None
    )

    # Prefer the group's real resolved name (PBS's own reported guest name)
    # over the bare backup id when generating a default temp_name, now that
    # temp_name is actually applied to the restored guest as its name/
    # hostname (EPIC 9, ADR 0014) rather than only being conflict-checked
    # and then discarded.
    name_base = _slugify(group.resolved_name) if group.resolved_name else group.backup_id
    temp_name = body.temp_name or f"restore-{name_base}-{uuid.uuid4().hex[:8]}"

    checks = [
        check_vmid(guests_result.guests, body.target_vmid, body.vmid_intent, vmid_result),
        check_storage_capacity(storages_result.storages, body.target_storage),
        check_name_conflict(guests_result.guests, temp_name),
        classify_network_risk(cluster.restore_network_rules, body.network_mode, body.network_bridge),
    ]
    original_vm = None
    if instant:
        # The agent reads the backup with its own PBS token, so the PBS storage
        # need not exist on the target cluster; the cluster, agent and original
        # VM checks replace it (ADR 0030 §2/§4).
        original_vm = find_original_vm(db, settings, group)
        checks += ir_checks.check_cluster_ready(cluster, body.target_node, now=datetime.now(UTC))
        checks.append(ir_checks.check_original_vm(original_vm, body.network_mode))
    else:
        checks.insert(
            2,
            check_pbs_storage_visibility(
                storages_result.storages, datastore.name, group.namespace, pbs_instance.host, is_linked
            ),
        )

    failing = [c for c in checks if c["status"] == "fail"]
    if failing:
        raise ResurgamApiError(
            code="restore_plan_checks_failed",
            message="One or more checks failed.",
            status_code=400,
            details={"checks": checks},
        )

    needs_ack = [c for c in checks if c["check"] in ("network_risk", "original_vm", "agent_node")]
    if any(c["status"] in ("warn", "unknown") for c in needs_ack) and not body.network_danger_acknowledged:
        raise ResurgamApiError(
            code="network_acknowledgment_required",
            message="This network configuration requires explicit acknowledgment before the plan can be created.",
            status_code=400,
            details={"checks": checks},
        )

    if instant:
        command = ir_checks.describe_plan(
            backup_label=group.resolved_name or f"{group.backup_type}/{group.backup_id}",
            target_vmid=body.target_vmid,
            target_node=body.target_node,
            target_storage=body.target_storage,
        )
    else:
        command = generate_restore_command(
            backup_type=group.backup_type,
            backup_id=group.backup_id,
            snapshot_time=snapshot.snapshot_time,
            pbs_storage_id=next(
                (s.storage for s in storages_result.storages if s.type == "pbs" and s.pbs_datastore == datastore.name),
                body.target_storage,
            ),
            target_vmid=body.target_vmid,
            target_storage=body.target_storage,
            vmid_intent_is_new=body.vmid_intent == RestorePlanVmidIntent.NEW,
            network_mode=body.network_mode,
            network_bridge=body.network_bridge,
            start_after_restore=body.start_after_restore,
            temp_name=temp_name,
        )

    assignment = db.query(DatastoreCustomerAssignment).filter_by(datastore_id=datastore.id).one_or_none()
    effective_customer_id = effective_group_customer_id(group, assignment)

    plan = RestorePlan(
        backup_snapshot_id=snapshot.id,
        backup_group_id=group.id,
        customer_id=effective_customer_id,
        pve_cluster_id=cluster.id,
        target_node=body.target_node,
        target_storage=body.target_storage,
        vmid_intent=body.vmid_intent.value,
        target_vmid=body.target_vmid,
        temp_name=temp_name,
        network_mode=body.network_mode.value,
        network_bridge=body.network_bridge,
        start_after_restore=body.start_after_restore,
        network_danger_acknowledged=body.network_danger_acknowledged,
        checks_result=checks,
        checks_performed_at=datetime.now(UTC),
        generated_command=command,
        created_by_user_id=user.user_id,
        # EPIC 9 (manifest §11.2): four-eyes gating is visible from
        # creation time, but always re-checked at the execute gate too
        # (see api/v1/restore_execution.py), so it's safe regardless of
        # when this was set.
        approval_status=RestorePlanApprovalStatus.PENDING.value if cluster.require_restore_approval else None,
        method=body.method.value,
        ir_original_vm=original_vm.as_dict() if original_vm else None,
        ir_keep_mac=ir_checks.keep_mac(original_vm) if original_vm else False,
    )
    db.add(plan)
    db.flush()

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="restore_plan_created",
        target_id=plan.id,
        customer_id=plan.customer_id,
        detail={
            "backup_snapshot_id": str(snapshot.id),
            "pve_cluster_id": str(cluster.id),
            "target_vmid": body.target_vmid,
            "method": body.method.value,
            **({"original_vm": original_vm.as_dict()} if original_vm else {}),
        },
    )
    db.commit()
    db.refresh(plan)
    return RestorePlanOut.model_validate(plan)


@router.get("", response_model=list[RestorePlanOut])
def list_restore_plans(
    db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> list[RestorePlanOut]:
    query = db.query(RestorePlan)
    visible = visible_customer_ids(user)
    if visible is not None:
        query = query.filter(RestorePlan.customer_id.in_(visible))
    plans = query.order_by(RestorePlan.created_at.desc()).all()
    return [RestorePlanOut.model_validate(p) for p in plans]


@router.get("/{plan_id}", response_model=RestorePlanOut)
def get_restore_plan(
    plan_id: uuid.UUID, db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> RestorePlanOut:
    plan = _get_plan_or_404(db, plan_id, user)
    return RestorePlanOut.model_validate(plan)


@router.post("/{plan_id}/recheck", response_model=RestorePlanOut)
def recheck_restore_plan(
    plan_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> RestorePlanOut:
    """Re-run the live checks only — the generated command and every other
    field stay as originally created (the command doesn't depend on live
    PVE state, only on the request's own fields plus the snapshot).
    """
    plan = _get_plan_or_404(db, plan_id, user)
    _snapshot, group, datastore = _get_snapshot_group_and_datastore(db, plan.backup_snapshot_id)
    cluster = db.get(PveCluster, plan.pve_cluster_id)
    if cluster is None:
        raise ResurgamApiError(code="not_found", message="PVE cluster not found.", status_code=404)

    secret_ref = db.get(SecretReference, cluster.api_token_secret_ref_id)
    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    token = provider.retrieve(secret_ref.reference_key)
    trust = TrustConfig(
        trust_mode=TlsTrustMode(cluster.tls_trust_mode), fingerprint_sha256=cluster.tls_fingerprint_sha256
    )
    timeout = settings.connectivity_timeout_seconds

    storages_result = list_storages(cluster.host, cluster.port, trust, token, plan.target_node, timeout=timeout)
    guests_result = list_guests(cluster.host, cluster.port, trust, token, timeout=timeout)
    vmid_result = check_vmid_free(cluster.host, cluster.port, trust, token, plan.target_vmid, timeout=timeout)

    if not storages_result.reachable or not guests_result.reachable:
        error = storages_result.error or guests_result.error
        raise upstream_error("pve", error)

    pbs_instance = db.get(PbsInstance, datastore.pbs_instance_id)
    is_linked = (
        db.query(PveClusterPbsInstance).filter_by(pve_cluster_id=cluster.id, pbs_instance_id=pbs_instance.id).first()
        is not None
    )

    checks = [
        check_vmid(guests_result.guests, plan.target_vmid, RestorePlanVmidIntent(plan.vmid_intent), vmid_result),
        check_storage_capacity(storages_result.storages, plan.target_storage),
        check_name_conflict(guests_result.guests, plan.temp_name),
        classify_network_risk(
            cluster.restore_network_rules, RestorePlanNetworkMode(plan.network_mode), plan.network_bridge
        ),
    ]
    if plan.method == RestorePlanMethod.INSTANT.value:
        original_vm = find_original_vm(db, settings, group)
        checks += ir_checks.check_cluster_ready(cluster, plan.target_node, now=datetime.now(UTC))
        checks.append(ir_checks.check_original_vm(original_vm, RestorePlanNetworkMode(plan.network_mode)))
        if plan.execution_status is None:
            plan.ir_original_vm = original_vm.as_dict()
            plan.ir_keep_mac = ir_checks.keep_mac(original_vm)
    else:
        checks.insert(
            2,
            check_pbs_storage_visibility(
                storages_result.storages, datastore.name, group.namespace, pbs_instance.host, is_linked
            ),
        )

    plan.checks_result = checks
    plan.checks_performed_at = datetime.now(UTC)

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="restore_plan_rechecked",
        target_id=plan.id,
        customer_id=plan.customer_id,
    )
    db.commit()
    db.refresh(plan)
    return RestorePlanOut.model_validate(plan)
