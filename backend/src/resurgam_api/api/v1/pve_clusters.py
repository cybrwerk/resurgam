import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from resurgam_api.api.v1._connection_helpers import (
    health_to_response,
    provision,
    required_privileges_out,
    retrieve_token,
    stored_token_id,
    stored_token_secret_set,
    updated_token,
    upstream_error,
)
from resurgam_api.auth.dependency import require_role
from resurgam_api.config import Settings, get_settings
from resurgam_api.connectivity.health import apply_health_result, check_pve_health
from resurgam_api.connectivity.models import TrustConfig, VzdumpJob
from resurgam_api.connectivity.pve_api import (
    PveWriteError,
    check_vmid_free,
    create_vzdump_job,
    delete_vzdump_job,
    get_own_permissions,
    get_vzdump_job,
    list_guests,
    list_nodes,
    list_pools,
    list_storages,
    list_vzdump_jobs,
    set_vzdump_job_enabled,
    update_vzdump_job,
)
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.lab_setup.uplink import UplinkConfig, raise_if_invalid, uplink_config_from_cluster
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.datastore import Datastore
from resurgam_api.models.disk_mount import ACTIVE_STATUSES, DiskMount
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.pbs import ConnectionHealthStatus, PbsInstance, TlsTrustMode
from resurgam_api.models.pve import (
    PveBackupJobCustomerAssignment,
    PveCluster,
    PveClusterDatastore,
    PveClusterPbsInstance,
)
from resurgam_api.models.secrets import SecretPurpose, SecretReference
from resurgam_api.models.tenancy import Customer
from resurgam_api.net.tls_pinning import fetch_peer_certificate_sha256
from resurgam_api.pve_job_reenable import clear_reenable, get_reenable, schedule_reenable
from resurgam_api.restore_test.execution import generate_appliance_run_token, hash_appliance_run_token
from resurgam_api.schemas.connectivity import (
    RequiredPrivilegesOut,
    TestConnectionResponse,
    TlsProbeRequest,
    TlsProbeResponse,
)
from resurgam_api.schemas.lab_agent import LabAgentTokenOut
from resurgam_api.schemas.pve import (
    AssignBackupJobCustomerRequest,
    BackupJobReenableScheduleRequest,
    DisableBackupJobRequest,
    PveCapabilitiesOut,
    PveClusterCreateRequest,
    PveClusterOut,
    PveClusterUpdateRequest,
    PveGuestOut,
    PveNodeOut,
    PvePoolOut,
    PveStorageOut,
    PveVmidCheckOut,
    VzdumpJobCreateRequest,
    VzdumpJobOut,
    VzdumpJobUpdateRequest,
)
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

router = APIRouter(prefix="/pve-clusters", tags=["pve-clusters"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)


def _record_audit(db: Session, request: Request, *, actor_user_id, action: str, target_id, detail=None) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            target_type="pve_cluster",
            target_id=str(target_id),
            correlation_id=getattr(request.state, "correlation_id", None),
            detail=detail,
        )
    )


def _linked_pbs_instance_ids(db: Session, cluster_id: uuid.UUID) -> list[uuid.UUID]:
    rows = db.query(PveClusterPbsInstance.pbs_instance_id).filter_by(pve_cluster_id=cluster_id).all()
    return [row[0] for row in rows]


def _linked_datastore_ids(db: Session, cluster_id: uuid.UUID) -> list[uuid.UUID]:
    rows = db.query(PveClusterDatastore.datastore_id).filter_by(pve_cluster_id=cluster_id).all()
    return [row[0] for row in rows]


def _get_backup_job_customer_assignment(
    db: Session, cluster_id: uuid.UUID, job_id: str
) -> PveBackupJobCustomerAssignment | None:
    return db.query(PveBackupJobCustomerAssignment).filter_by(pve_cluster_id=cluster_id, job_id=job_id).one_or_none()


def _vzdump_job_to_out(db: Session, cluster_id: uuid.UUID, job: VzdumpJob) -> VzdumpJobOut:
    """`VzdumpJobOut(**vars(job))` alone would leave `customer_id` at its
    default of `None` -- this enriches it from the override table
    (docs/adr/0023-backup-and-maintenance-job-administration): a job's
    default, in-design scope is inherited from its target datastore/
    namespace, not stored here, so `None` genuinely means "no override
    set," not "no customer."
    """
    assignment = _get_backup_job_customer_assignment(db, cluster_id, job.id)
    # A pending re-enable is only meaningful for a job that is actually
    # disabled; if someone enabled it in PVE directly, the scheduler drops
    # the row on its next due pass -- don't show a stale timer meanwhile.
    reenable = None if job.enabled else get_reenable(db, cluster_id, job.id)
    return VzdumpJobOut(
        **vars(job),
        customer_id=assignment.customer_id if assignment else None,
        reenable_at=reenable.reenable_at if reenable else None,
        reenable_last_error=reenable.last_error if reenable else None,
    )


def _to_out(db: Session, cluster: PveCluster, settings: Settings) -> PveClusterOut:
    return PveClusterOut(
        id=cluster.id,
        display_name=cluster.display_name,
        host=cluster.host,
        port=cluster.port,
        tls_trust_mode=TlsTrustMode(cluster.tls_trust_mode),
        tls_fingerprint_sha256=cluster.tls_fingerprint_sha256,
        token_id=stored_token_id(db, settings, cluster.api_token_secret_ref_id),
        token_secret_set=stored_token_secret_set(db, settings, cluster.api_token_secret_ref_id),
        allowed_target_storages=cluster.allowed_target_storages,
        restore_network_rules=cluster.restore_network_rules,
        require_restore_approval=cluster.require_restore_approval,
        instant_recovery_enabled=cluster.instant_recovery_enabled,
        instant_recovery_alarm_hours=cluster.instant_recovery_alarm_hours,
        version=cluster.version,
        last_health_check_at=cluster.last_health_check_at,
        last_health_status=ConnectionHealthStatus(cluster.last_health_status),
        disabled=cluster.disabled,
        linked_pbs_instance_ids=_linked_pbs_instance_ids(db, cluster.id),
        linked_datastore_ids=_linked_datastore_ids(db, cluster.id),
        is_lab_compute=cluster.is_lab_compute,
        lab_sdn_zone_name=cluster.lab_sdn_zone_name,
        lab_appliance_template_vmid=cluster.lab_appliance_template_vmid,
        lab_snippet_storage_id=cluster.lab_snippet_storage_id,
        lab_appliance_uplink_bridge=cluster.lab_appliance_uplink_bridge,
        lab_agent_last_seen_at=cluster.lab_agent_last_seen_at,
        lab_agent_node=cluster.lab_agent_node,
        lab_appliance_uplink_vlan_tag=cluster.lab_appliance_uplink_vlan_tag,
        lab_appliance_uplink_ip_mode=cluster.lab_appliance_uplink_ip_mode or "dhcp",
        lab_appliance_uplink_ip_range_start=cluster.lab_appliance_uplink_ip_range_start,
        lab_appliance_uplink_ip_range_end=cluster.lab_appliance_uplink_ip_range_end,
        lab_appliance_uplink_ip_prefix_length=cluster.lab_appliance_uplink_ip_prefix_length,
        lab_appliance_uplink_gateway=cluster.lab_appliance_uplink_gateway,
        lab_appliance_uplink_dns_servers=list(cluster.lab_appliance_uplink_dns_servers or []),
    )


@router.post("/probe-tls", response_model=TlsProbeResponse)
def probe_tls(
    body: TlsProbeRequest,
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> TlsProbeResponse:
    try:
        fingerprint = fetch_peer_certificate_sha256(body.host, body.port, timeout=settings.connectivity_timeout_seconds)
    except OSError as exc:
        raise ResurgamApiError(code="tls_probe_failed", message=str(exc), status_code=502) from exc
    return TlsProbeResponse(fingerprint_sha256=fingerprint)


@router.get("/required-privileges", response_model=RequiredPrivilegesOut)
def get_pve_required_privileges(_user=Depends(_require_platform_admin)) -> RequiredPrivilegesOut:
    """Every PVE privilege RESURGAM's API token needs (and why), the custom
    role `RESURGAM` that credentials mode creates and grants to its own
    token, and the `pveum` commands to set a token up by hand. Static -- no
    PVE is contacted. Shown in the add form before connecting.
    """
    return required_privileges_out("pve")


@router.post("", response_model=PveClusterOut)
def create_pve_cluster(
    body: PveClusterCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> PveClusterOut:
    # Before anything with side effects (credentials mode creates a token on PVE).
    raise_if_invalid(
        UplinkConfig(
            bridge=body.lab_appliance_uplink_bridge,
            vlan_tag=body.lab_appliance_uplink_vlan_tag,
            ip_mode=body.lab_appliance_uplink_ip_mode,
            range_start=body.lab_appliance_uplink_ip_range_start,
            range_end=body.lab_appliance_uplink_ip_range_end,
            prefix_length=body.lab_appliance_uplink_ip_prefix_length,
            gateway=body.lab_appliance_uplink_gateway,
            dns_servers=list(body.lab_appliance_uplink_dns_servers),
        )
    )
    if body.tls_trust_mode == TlsTrustMode.FINGERPRINT:
        try:
            observed = fetch_peer_certificate_sha256(
                body.host, body.port, timeout=settings.connectivity_timeout_seconds
            )
        except OSError as exc:
            raise ResurgamApiError(code="tls_probe_failed", message=str(exc), status_code=502) from exc
        if observed.lower() != body.tls_fingerprint_sha256.lower():
            raise ResurgamApiError(
                code="tls_fingerprint_mismatch",
                message="The confirmed fingerprint no longer matches the host's current certificate.",
                status_code=409,
                details={"confirmed": body.tls_fingerprint_sha256, "observed": observed},
            )

    if body.linked_pbs_instance_ids:
        found = db.query(PbsInstance.id).filter(PbsInstance.id.in_(body.linked_pbs_instance_ids)).all()
        found_ids = {row[0] for row in found}
        missing = set(body.linked_pbs_instance_ids) - found_ids
        if missing:
            raise ResurgamApiError(
                code="pbs_instance_not_found",
                message="One or more linked PBS instances do not exist.",
                status_code=400,
                details={"missing_ids": [str(i) for i in missing]},
            )

    if body.linked_datastore_ids:
        found = db.query(Datastore.id).filter(Datastore.id.in_(body.linked_datastore_ids)).all()
        found_ids = {row[0] for row in found}
        missing = set(body.linked_datastore_ids) - found_ids
        if missing:
            raise ResurgamApiError(
                code="datastore_not_found",
                message="One or more linked datastores do not exist.",
                status_code=400,
                details={"missing_ids": [str(i) for i in missing]},
            )

    reference_key = f"pve-cluster-token-{uuid.uuid4()}"
    secret_ref = SecretReference(
        reference_key=reference_key,
        description=f"API token for PVE cluster '{body.display_name}'",
        purpose=SecretPurpose.INFRA_CREDENTIAL,
    )
    db.add(secret_ref)
    db.flush()

    if body.credentials is not None:
        trust = TrustConfig(trust_mode=body.tls_trust_mode, fingerprint_sha256=body.tls_fingerprint_sha256)
        token = provision("pve", body.host, body.port, trust, body.credentials, settings)
    else:
        token = body.stored_token()
    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    provider.store(reference_key, token)

    cluster = PveCluster(
        display_name=body.display_name,
        host=body.host,
        port=body.port,
        tls_trust_mode=body.tls_trust_mode.value,
        tls_fingerprint_sha256=body.tls_fingerprint_sha256,
        api_token_secret_ref_id=secret_ref.id,
        allowed_target_storages=body.allowed_target_storages,
        restore_network_rules=body.restore_network_rules,
        require_restore_approval=body.require_restore_approval,
        is_lab_compute=body.is_lab_compute,
        lab_sdn_zone_name=body.lab_sdn_zone_name,
        lab_appliance_template_vmid=body.lab_appliance_template_vmid,
        lab_snippet_storage_id=body.lab_snippet_storage_id,
        lab_appliance_uplink_bridge=body.lab_appliance_uplink_bridge,
        lab_appliance_uplink_vlan_tag=body.lab_appliance_uplink_vlan_tag,
        lab_appliance_uplink_ip_mode=body.lab_appliance_uplink_ip_mode,
        lab_appliance_uplink_ip_range_start=body.lab_appliance_uplink_ip_range_start,
        lab_appliance_uplink_ip_range_end=body.lab_appliance_uplink_ip_range_end,
        lab_appliance_uplink_ip_prefix_length=body.lab_appliance_uplink_ip_prefix_length,
        lab_appliance_uplink_gateway=body.lab_appliance_uplink_gateway,
        lab_appliance_uplink_dns_servers=body.lab_appliance_uplink_dns_servers or None,
        last_health_status=ConnectionHealthStatus.UNKNOWN.value,
    )
    db.add(cluster)
    db.flush()

    for pbs_instance_id in body.linked_pbs_instance_ids:
        db.add(PveClusterPbsInstance(pve_cluster_id=cluster.id, pbs_instance_id=pbs_instance_id))

    for datastore_id in body.linked_datastore_ids:
        db.add(PveClusterDatastore(pve_cluster_id=cluster.id, datastore_id=datastore_id))

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pve_cluster_created",
        target_id=cluster.id,
        detail={
            "display_name": body.display_name,
            "host": body.host,
            "trust_mode": body.tls_trust_mode.value,
            "token_mode": "credentials" if body.credentials is not None else "token",
        },
    )
    db.commit()
    db.refresh(cluster)
    return _to_out(db, cluster, settings)


@router.get("", response_model=list[PveClusterOut])
def list_pve_clusters(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> list[PveClusterOut]:
    clusters = db.query(PveCluster).order_by(PveCluster.created_at).all()
    return [_to_out(db, c, settings) for c in clusters]


def _get_cluster_or_404(db: Session, cluster_id: uuid.UUID) -> PveCluster:
    cluster = db.get(PveCluster, cluster_id)
    if cluster is None:
        raise ResurgamApiError(code="not_found", message="PVE cluster not found.", status_code=404)
    return cluster


@router.get("/{cluster_id}", response_model=PveClusterOut)
def get_pve_cluster(
    cluster_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> PveClusterOut:
    return _to_out(db, _get_cluster_or_404(db, cluster_id), settings)


@router.patch("/{cluster_id}", response_model=PveClusterOut)
def update_pve_cluster(
    cluster_id: uuid.UUID,
    body: PveClusterUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> PveClusterOut:
    cluster = _get_cluster_or_404(db, cluster_id)

    new_host = body.host if body.host is not None else cluster.host
    new_port = body.port if body.port is not None else cluster.port
    new_trust_mode = body.tls_trust_mode if body.tls_trust_mode is not None else TlsTrustMode(cluster.tls_trust_mode)
    new_fingerprint = (
        body.tls_fingerprint_sha256 if body.tls_fingerprint_sha256 is not None else cluster.tls_fingerprint_sha256
    )

    if new_trust_mode == TlsTrustMode.FINGERPRINT and not new_fingerprint:
        raise ResurgamApiError(
            code="fingerprint_required",
            message="tls_fingerprint_sha256 is required when tls_trust_mode is 'fingerprint'.",
            status_code=400,
        )

    connection_fields_changed = (
        body.host is not None or body.port is not None or body.tls_fingerprint_sha256 is not None
    )
    if new_trust_mode == TlsTrustMode.FINGERPRINT and connection_fields_changed:
        try:
            observed = fetch_peer_certificate_sha256(new_host, new_port, timeout=settings.connectivity_timeout_seconds)
        except OSError as exc:
            raise ResurgamApiError(code="tls_probe_failed", message=str(exc), status_code=502) from exc
        if observed.lower() != new_fingerprint.lower():
            raise ResurgamApiError(
                code="tls_fingerprint_mismatch",
                message="The confirmed fingerprint no longer matches the host's current certificate.",
                status_code=409,
                details={"confirmed": new_fingerprint, "observed": observed},
            )

    if body.linked_pbs_instance_ids is not None:
        found = db.query(PbsInstance.id).filter(PbsInstance.id.in_(body.linked_pbs_instance_ids)).all()
        found_ids = {row[0] for row in found}
        missing = set(body.linked_pbs_instance_ids) - found_ids
        if missing:
            raise ResurgamApiError(
                code="pbs_instance_not_found",
                message="One or more linked PBS instances do not exist.",
                status_code=400,
                details={"missing_ids": [str(i) for i in missing]},
            )

    if body.linked_datastore_ids is not None:
        found = db.query(Datastore.id).filter(Datastore.id.in_(body.linked_datastore_ids)).all()
        found_ids = {row[0] for row in found}
        missing = set(body.linked_datastore_ids) - found_ids
        if missing:
            raise ResurgamApiError(
                code="datastore_not_found",
                message="One or more linked datastores do not exist.",
                status_code=400,
                details={"missing_ids": [str(i) for i in missing]},
            )

    if body.display_name is not None:
        cluster.display_name = body.display_name
    cluster.host = new_host
    cluster.port = new_port
    cluster.tls_trust_mode = new_trust_mode.value
    cluster.tls_fingerprint_sha256 = new_fingerprint
    if body.allowed_target_storages is not None:
        cluster.allowed_target_storages = body.allowed_target_storages
    if body.restore_network_rules is not None:
        cluster.restore_network_rules = body.restore_network_rules
    if body.require_restore_approval is not None:
        cluster.require_restore_approval = body.require_restore_approval
    if body.instant_recovery_enabled is not None:
        cluster.instant_recovery_enabled = body.instant_recovery_enabled
    if body.instant_recovery_alarm_hours is not None:
        cluster.instant_recovery_alarm_hours = body.instant_recovery_alarm_hours
    if body.is_lab_compute is not None:
        cluster.is_lab_compute = body.is_lab_compute
    # Unlike every other field above, these two are meant to be
    # clearable back to null (e.g. "stop using an admin-configured SDN
    # zone, fall back to RESURGAM's own auto-provisioned default") -- an
    # explicit `null` in the request body must be distinguished from the
    # field being omitted entirely, so this checks `model_fields_set`
    # instead of `is not None` (confirmed live: PveClustersPage's edit
    # form sends `lab_sdn_zone_name: null` to clear the field, which the
    # `is not None` pattern used elsewhere in this function would
    # silently ignore).
    if "lab_sdn_zone_name" in body.model_fields_set:
        cluster.lab_sdn_zone_name = body.lab_sdn_zone_name
    if "lab_appliance_template_vmid" in body.model_fields_set:
        cluster.lab_appliance_template_vmid = body.lab_appliance_template_vmid
    if "lab_snippet_storage_id" in body.model_fields_set:
        cluster.lab_snippet_storage_id = body.lab_snippet_storage_id
    if "lab_appliance_uplink_bridge" in body.model_fields_set:
        cluster.lab_appliance_uplink_bridge = body.lab_appliance_uplink_bridge
    # Uplink VLAN/IP settings (migration 0055): clearable with an explicit
    # null like the fields above; validated as a whole on the merged state,
    # since a partial update (e.g. only the range end) must still produce
    # a consistent static range.
    if "lab_appliance_uplink_vlan_tag" in body.model_fields_set:
        cluster.lab_appliance_uplink_vlan_tag = body.lab_appliance_uplink_vlan_tag
    if body.lab_appliance_uplink_ip_mode is not None:
        cluster.lab_appliance_uplink_ip_mode = body.lab_appliance_uplink_ip_mode
    for uplink_field in (
        "lab_appliance_uplink_ip_range_start",
        "lab_appliance_uplink_ip_range_end",
        "lab_appliance_uplink_ip_prefix_length",
        "lab_appliance_uplink_gateway",
    ):
        if uplink_field in body.model_fields_set:
            value = getattr(body, uplink_field)
            setattr(cluster, uplink_field, value.strip() if isinstance(value, str) and value.strip() else value or None)
    if body.lab_appliance_uplink_dns_servers is not None:
        cluster.lab_appliance_uplink_dns_servers = [
            d.strip() for d in body.lab_appliance_uplink_dns_servers if d.strip()
        ]
    raise_if_invalid(uplink_config_from_cluster(cluster))

    if body.credentials is not None:
        trust = TrustConfig(trust_mode=new_trust_mode, fingerprint_sha256=new_fingerprint)
        new_token = provision("pve", new_host, new_port, trust, body.credentials, settings)
    else:
        new_token = updated_token(
            "pve",
            retrieve_token(db, settings, cluster.api_token_secret_ref_id),
            token_id=body.token_id,
            token_secret=body.token_secret,
            api_token=body.api_token,
        )
    if new_token is not None:
        secret_ref = db.get(SecretReference, cluster.api_token_secret_ref_id)
        provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
        provider.store(secret_ref.reference_key, new_token)
        secret_ref.last_rotated_at = datetime.now(UTC)

    if body.linked_pbs_instance_ids is not None:
        db.query(PveClusterPbsInstance).filter_by(pve_cluster_id=cluster.id).delete()
        for pbs_instance_id in body.linked_pbs_instance_ids:
            db.add(PveClusterPbsInstance(pve_cluster_id=cluster.id, pbs_instance_id=pbs_instance_id))

    if body.linked_datastore_ids is not None:
        db.query(PveClusterDatastore).filter_by(pve_cluster_id=cluster.id).delete()
        for datastore_id in body.linked_datastore_ids:
            db.add(PveClusterDatastore(pve_cluster_id=cluster.id, datastore_id=datastore_id))

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pve_cluster_updated",
        target_id=cluster.id,
        detail={
            "host": new_host,
            "port": new_port,
            "api_token_rotated": new_token is not None,
            "token_mode": "credentials" if body.credentials is not None else "token",
        },
    )
    db.commit()
    db.refresh(cluster)
    return _to_out(db, cluster, settings)


@router.post("/{cluster_id}/test-connection", response_model=TestConnectionResponse)
def test_pve_cluster_connection(
    cluster_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> TestConnectionResponse:
    """Classified connection test: DNS, TCP/TLS (pinned fingerprint or CA),
    token authentication (`GET /version`) and the token's effective
    privileges (`GET /access/permissions`) compared with the required list.
    Always HTTP 200; the outcome is in `status` (also stored as
    `last_health_status`, with `last_health_check_at`). The UI calls this
    automatically right after create and after a change to host, port, TLS
    or token. Same classification as the periodic heartbeat.
    """
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    result = check_pve_health(cluster.host, cluster.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    apply_health_result(cluster, result)

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pve_cluster_test_connection",
        target_id=cluster.id,
        detail={
            "reachable": result.reachable,
            "status": result.status.value,
            "error": result.message,
            "missing_privileges": list(result.missing_privileges),
        },
    )
    db.commit()
    return health_to_response(result)


def _cluster_trust_and_token(db: Session, settings: Settings, cluster: PveCluster) -> tuple[TrustConfig, str]:
    secret_ref = db.get(SecretReference, cluster.api_token_secret_ref_id)
    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    token = provider.retrieve(secret_ref.reference_key)
    trust = TrustConfig(
        trust_mode=TlsTrustMode(cluster.tls_trust_mode), fingerprint_sha256=cluster.tls_fingerprint_sha256
    )
    return trust, token


@router.get("/{cluster_id}/nodes", response_model=list[PveNodeOut])
def list_pve_cluster_nodes(
    cluster_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> list[PveNodeOut]:
    """Live-probe the cluster's nodes. Nothing persisted — used by the
    Guided Restore wizard's target-selection step (manifest EPIC 8).
    """
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    result = list_nodes(cluster.host, cluster.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    if not result.reachable:
        raise upstream_error("pve", result.error)
    return [PveNodeOut(**vars(n)) for n in result.nodes]


@router.get("/{cluster_id}/nodes/{node}/storages", response_model=list[PveStorageOut])
def list_pve_cluster_node_storages(
    cluster_id: uuid.UUID,
    node: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> list[PveStorageOut]:
    """Live-probe one node's storages, merged from cluster-config and
    per-node status — see `connectivity.pve_api.list_storages`.
    """
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    result = list_storages(
        cluster.host, cluster.port, trust, token, node, timeout=settings.connectivity_timeout_seconds
    )
    if not result.reachable:
        raise upstream_error("pve", result.error)
    return [PveStorageOut(**vars(s)) for s in result.storages]


@router.get("/{cluster_id}/guests", response_model=list[PveGuestOut])
def list_pve_cluster_guests(
    cluster_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> list[PveGuestOut]:
    """Live-probe every VM/CT cluster-wide (VMID uniqueness is cluster-wide
    in PVE, not per-node — ADR 0009).
    """
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    result = list_guests(cluster.host, cluster.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    if not result.reachable:
        raise upstream_error("pve", result.error)
    return [PveGuestOut(**vars(g)) for g in result.guests]


@router.get("/{cluster_id}/pools", response_model=list[PvePoolOut])
def list_pve_cluster_pools(
    cluster_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> list[PvePoolOut]:
    """Live-probe every resource pool on the cluster -- one of the two
    real ways to target a vzdump job at a set of guests
    (docs/adr/0023-backup-and-maintenance-job-administration), surfaced
    so the job form can offer a real picker instead of a free-text id.
    """
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    result = list_pools(cluster.host, cluster.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    if not result.reachable:
        raise upstream_error("pve", result.error)
    return [PvePoolOut(**vars(p)) for p in result.pools]


@router.get("/{cluster_id}/vmid-check", response_model=PveVmidCheckOut)
def check_pve_cluster_vmid(
    cluster_id: uuid.UUID,
    vmid: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> PveVmidCheckOut:
    """Live-check whether one VMID is free — used for the wizard's inline
    VMID-conflict feedback as the operator types.
    """
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    result = check_vmid_free(
        cluster.host, cluster.port, trust, token, vmid, timeout=settings.connectivity_timeout_seconds
    )
    if not result.reachable:
        raise upstream_error("pve", result.error)
    return PveVmidCheckOut(vmid=result.vmid, free=result.free)


@router.get("/{cluster_id}/backup-jobs", response_model=list[VzdumpJobOut])
def list_pve_cluster_backup_jobs(
    cluster_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> list[VzdumpJobOut]:
    """Live-probe the cluster's own configured vzdump backup jobs. Nothing
    persisted — PVE remains the system of record for its own job config,
    same as `list_pve_cluster_nodes`/`list_pve_cluster_guests` above.
    """
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    result = list_vzdump_jobs(cluster.host, cluster.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    if not result.reachable:
        raise upstream_error("pve", result.error)
    return [_vzdump_job_to_out(db, cluster_id, j) for j in result.jobs]


def _vzdump_update_payload(body: VzdumpJobUpdateRequest) -> dict:
    """Maps `VzdumpJobUpdateRequest`'s Python field names to PVE's real
    hyphenated wire names and drops every field the operator didn't set
    -- mirrors `connectivity.pve_api._vzdump_job_payload`'s own mapping,
    kept separate since this one starts from the *update* schema (every
    field genuinely optional) rather than a full `VzdumpJob`.

    `clear_prune_backups` is handled as a dedicated, atomic action --
    same reasoning as `pbs_api.set_datastore_maintenance_mode`'s own
    mode=null clear path: a real PVE `delete=prune-backups` call, not
    mixed with any other field change in the same request.
    """
    if body.clear_prune_backups:
        return {"delete": "prune-backups"}

    raw = {
        "schedule": body.schedule,
        "storage": body.storage,
        "node": body.node,
        "comment": body.comment,
        "all": None if body.all_guests is None else (1 if body.all_guests else 0),
        "vmid": body.vmid,
        "pool": body.pool,
        "exclude": body.exclude,
        "mode": body.mode,
        "compress": body.compress,
        "notes-template": body.notes_template,
        "mailnotification": body.mailnotification,
        "mailto": body.mailto,
        "enabled": None if body.enabled is None else (1 if body.enabled else 0),
    }
    prune_fields = (
        ("keep-last", body.prune_keep_last),
        ("keep-hourly", body.prune_keep_hourly),
        ("keep-daily", body.prune_keep_daily),
        ("keep-weekly", body.prune_keep_weekly),
        ("keep-monthly", body.prune_keep_monthly),
        ("keep-yearly", body.prune_keep_yearly),
    )
    if any(value is not None for _, value in prune_fields):
        raw["prune-backups"] = ",".join(f"{wire}={value}" for wire, value in prune_fields if value is not None)
    return {key: value for key, value in raw.items() if value is not None}


def _set_backup_job_enabled(
    cluster_id: uuid.UUID,
    job_id: str,
    enabled: bool,
    request: Request,
    db: Session,
    settings: Settings,
    user,
    reenable_after_hours: int | None = None,
) -> VzdumpJobOut:
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    try:
        set_vzdump_job_enabled(
            cluster.host,
            cluster.port,
            trust,
            token,
            settings.connectivity_timeout_seconds,
            job_id=job_id,
            enabled=enabled,
        )
    except PveWriteError as exc:
        raise ResurgamApiError(code="pve_write_failed", message=str(exc), status_code=502) from exc

    result = list_vzdump_jobs(cluster.host, cluster.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    if not result.reachable:
        raise upstream_error("pve", result.error)
    job = next((j for j in result.jobs if j.id == job_id), None)
    if job is None:
        raise ResurgamApiError(
            code="pve_vzdump_job_not_found", message=f"job {job_id} not found after update", status_code=502
        )

    # Any explicit enable/disable replaces a pending timer: enabling makes
    # it pointless, and a new disable either sets a fresh one or means
    # "until further notice".
    clear_reenable(db, cluster.id, job_id)
    detail = {"job_id": job_id, "schedule": job.schedule}
    if not enabled and reenable_after_hours is not None:
        row = schedule_reenable(db, cluster.id, job_id, reenable_after_hours, user.user_id)
        detail["reenable_at"] = row.reenable_at.isoformat()

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pve_vzdump_job_enabled" if enabled else "pve_vzdump_job_disabled",
        target_id=cluster.id,
        detail=detail,
    )
    db.commit()
    return _vzdump_job_to_out(db, cluster.id, job)


@router.post("/{cluster_id}/backup-jobs/{job_id}/enable", response_model=VzdumpJobOut)
def enable_pve_cluster_backup_job(
    cluster_id: uuid.UUID,
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> VzdumpJobOut:
    """Enable a PVE backup job now. Cancels a pending automatic re-enable
    timer, if one was set."""
    return _set_backup_job_enabled(cluster_id, job_id, True, request, db, settings, user)


@router.post("/{cluster_id}/backup-jobs/{job_id}/disable", response_model=VzdumpJobOut)
def disable_pve_cluster_backup_job(
    cluster_id: uuid.UUID,
    job_id: str,
    request: Request,
    body: DisableBackupJobRequest | None = None,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> VzdumpJobOut:
    """Disable a PVE backup job, either until someone enables it again
    (no body) or for a limited time (`reenable_after_hours`): RESURGAM then
    enables it again automatically once the time is up (checked every
    minute by the collector scheduler). Disabling again replaces any
    earlier timer."""
    hours = body.reenable_after_hours if body else None
    return _set_backup_job_enabled(cluster_id, job_id, False, request, db, settings, user, hours)


@router.put("/{cluster_id}/backup-jobs/{job_id}/reenable-schedule", response_model=VzdumpJobOut)
def set_pve_cluster_backup_job_reenable_schedule(
    cluster_id: uuid.UUID,
    job_id: str,
    body: BackupJobReenableScheduleRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> VzdumpJobOut:
    """Set or change the automatic re-enable timer of a job that is
    already disabled, counted from now. The job itself is not changed in
    PVE now. Fails with 409 `pve_vzdump_job_not_disabled` for an enabled
    job."""
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    job = _get_backup_job_or_404(cluster, job_id, trust, token, settings)
    if job.enabled:
        raise ResurgamApiError(
            code="pve_vzdump_job_not_disabled",
            message="This backup job is enabled; a re-enable timer only applies to a disabled job.",
            status_code=409,
        )
    row = schedule_reenable(db, cluster.id, job_id, body.reenable_after_hours, user.user_id)
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pve_vzdump_job_reenable_scheduled",
        target_id=cluster.id,
        detail={"job_id": job_id, "reenable_at": row.reenable_at.isoformat()},
    )
    db.commit()
    return _vzdump_job_to_out(db, cluster.id, job)


@router.delete("/{cluster_id}/backup-jobs/{job_id}/reenable-schedule", status_code=204)
def cancel_pve_cluster_backup_job_reenable_schedule(
    cluster_id: uuid.UUID,
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(_require_platform_admin),
) -> None:
    """Cancel a pending automatic re-enable: the job stays disabled until
    someone enables it. Fails with 404 `reenable_schedule_not_found` if no
    timer is set for this job. Does not contact PVE."""
    cluster = _get_cluster_or_404(db, cluster_id)
    reenable_at = clear_reenable(db, cluster.id, job_id)
    if reenable_at is None:
        raise ResurgamApiError(
            code="reenable_schedule_not_found",
            message="No automatic re-enable is scheduled for this backup job.",
            status_code=404,
        )
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pve_vzdump_job_reenable_cancelled",
        target_id=cluster.id,
        detail={"job_id": job_id, "reenable_at": reenable_at.isoformat()},
    )
    db.commit()


@router.get("/{cluster_id}/backup-jobs/capabilities", response_model=PveCapabilitiesOut)
def get_pve_cluster_backup_job_capabilities(
    cluster_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> PveCapabilitiesOut:
    """Whether this cluster's own configured PVE token can actually
    read/write vzdump job definitions, checked live against `GET
    /access/permissions` -- never assumed (docs/adr/0023-backup-and-maintenance-job-administration).
    Checked against PVE's own source (`PVE::API2::Backup`): list/read
    needs `Sys.Audit` on `/`, create/update/delete needs `Sys.Modify` on
    `/`. `Sys.Modify` alone was already found live (ADR 0018) not to
    always be sufficient -- a real write can still 403 for a
    storage-scoped privilege this check can't predict up front (which
    storage isn't known until an operator picks one); that case is
    surfaced as the real PVE error text at write time instead, not
    pre-empted here.
    """
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    result = get_own_permissions(
        cluster.host, cluster.port, trust, token, timeout=settings.connectivity_timeout_seconds
    )
    if not result.reachable:
        raise upstream_error("pve", result.error)
    root_privileges = result.privileges.get("/", {})
    return PveCapabilitiesOut(
        can_read=bool(root_privileges.get("Sys.Audit")), can_write=bool(root_privileges.get("Sys.Modify"))
    )


@router.get("/{cluster_id}/backup-jobs/{job_id}", response_model=VzdumpJobOut)
def get_pve_cluster_backup_job(
    cluster_id: uuid.UUID,
    job_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> VzdumpJobOut:
    """One job's full current definition, for pre-filling an edit form --
    "import" means the existing list becomes clickable into a real edit
    form, not copying anything into a RESURGAM-owned table
    (docs/adr/0023-backup-and-maintenance-job-administration).
    """
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    job = get_vzdump_job(cluster.host, cluster.port, trust, token, settings.connectivity_timeout_seconds, job_id=job_id)
    if job is None:
        raise ResurgamApiError(code="pve_vzdump_job_not_found", message=f"job {job_id} not found", status_code=404)
    return _vzdump_job_to_out(db, cluster.id, job)


@router.post("/{cluster_id}/backup-jobs", response_model=VzdumpJobOut)
def create_pve_cluster_backup_job(
    cluster_id: uuid.UUID,
    body: VzdumpJobCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> VzdumpJobOut:
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    # Confirmed live (2026-09-26): POST /cluster/backup's response body
    # is empty even on success, and PVE's own autogenerated job IDs are
    # random UUIDs with no creation-time ordering -- there is no reliable
    # way to find "the job that was just created" from a subsequent list.
    # RESURGAM generates the ID itself and sends it explicitly instead
    # (confirmed live that PVE accepts and uses a caller-supplied `--id`
    # as-is), so the job it just created can be fetched directly by ID.
    new_job_id = f"resurgam-{uuid.uuid4().hex[:12]}"
    new_job = VzdumpJob(id=new_job_id, **body.model_dump())
    try:
        create_vzdump_job(cluster.host, cluster.port, trust, token, settings.connectivity_timeout_seconds, job=new_job)
    except PveWriteError as exc:
        raise ResurgamApiError(code="pve_write_failed", message=str(exc), status_code=502) from exc

    created = get_vzdump_job(
        cluster.host, cluster.port, trust, token, settings.connectivity_timeout_seconds, job_id=new_job_id
    )
    if created is None:
        raise ResurgamApiError(code="pve_vzdump_job_not_found", message="job not found after creation", status_code=502)

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pve_backup_job_created",
        target_id=cluster.id,
        detail={"job_id": created.id, "schedule": created.schedule, "storage": created.storage},
    )
    db.commit()
    return _vzdump_job_to_out(db, cluster.id, created)


@router.put("/{cluster_id}/backup-jobs/{job_id}", response_model=VzdumpJobOut)
def update_pve_cluster_backup_job(
    cluster_id: uuid.UUID,
    job_id: str,
    body: VzdumpJobUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> VzdumpJobOut:
    """Partial update -- only fields the operator actually set on `body`
    are sent to PVE (confirmed live, ADR 0018: `PUT /cluster/backup/{id}`
    merges, doesn't replace), matching `_set_backup_job_enabled`'s own
    existing precedent for this endpoint family.
    """
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    changes = _vzdump_update_payload(body)
    if not changes:
        raise ResurgamApiError(code="no_changes", message="No fields to update.", status_code=400)

    try:
        update_vzdump_job(
            cluster.host,
            cluster.port,
            trust,
            token,
            settings.connectivity_timeout_seconds,
            job_id=job_id,
            changes=changes,
        )
    except PveWriteError as exc:
        raise ResurgamApiError(code="pve_write_failed", message=str(exc), status_code=502) from exc

    job = get_vzdump_job(cluster.host, cluster.port, trust, token, settings.connectivity_timeout_seconds, job_id=job_id)
    if job is None:
        raise ResurgamApiError(
            code="pve_vzdump_job_not_found", message=f"job {job_id} not found after update", status_code=502
        )

    if "enabled" in changes:
        clear_reenable(db, cluster.id, job_id)

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pve_backup_job_updated",
        target_id=cluster.id,
        detail={"job_id": job_id, "changed_fields": sorted(changes.keys())},
    )
    db.commit()
    return _vzdump_job_to_out(db, cluster.id, job)


@router.delete("/{cluster_id}/backup-jobs/{job_id}", status_code=204)
def delete_pve_cluster_backup_job(
    cluster_id: uuid.UUID,
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> None:
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    try:
        delete_vzdump_job(
            cluster.host, cluster.port, trust, token, settings.connectivity_timeout_seconds, job_id=job_id
        )
    except PveWriteError as exc:
        raise ResurgamApiError(code="pve_write_failed", message=str(exc), status_code=502) from exc

    # A deleted job's id is never reused (RESURGAM-generated ids are
    # random), but drop any leftover override row anyway rather than let
    # it sit orphaned in the table.
    existing_assignment = _get_backup_job_customer_assignment(db, cluster_id, job_id)
    if existing_assignment is not None:
        db.delete(existing_assignment)
    clear_reenable(db, cluster.id, job_id)

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pve_backup_job_deleted",
        target_id=cluster.id,
        detail={"job_id": job_id},
    )
    db.commit()


def _get_backup_job_or_404(
    cluster: PveCluster, job_id: str, trust: TrustConfig, token: str, settings: Settings
) -> VzdumpJob:
    job = get_vzdump_job(cluster.host, cluster.port, trust, token, settings.connectivity_timeout_seconds, job_id=job_id)
    if job is None:
        raise ResurgamApiError(code="pve_vzdump_job_not_found", message=f"job {job_id} not found", status_code=404)
    return job


@router.post("/{cluster_id}/backup-jobs/{job_id}/assign-customer", response_model=VzdumpJobOut)
def assign_backup_job_customer(
    cluster_id: uuid.UUID,
    job_id: str,
    body: AssignBackupJobCustomerRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> VzdumpJobOut:
    """An explicit customer-scope *override* for one job (docs/adr/0023) --
    a job's default, in-design scope is inherited from its target
    datastore/namespace (a `Datastore` is customer-scoped), not stored
    here. Meant for the case where a job's real targeting doesn't line up
    with that (`all_guests` spanning multiple customers' storage, or a
    job intentionally targeting a different customer's VMs). Mirrors
    `datastores.assign_customer`'s upsert shape exactly.
    """
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    job = _get_backup_job_or_404(cluster, job_id, trust, token, settings)

    if db.get(Customer, body.customer_id) is None:
        raise ResurgamApiError(code="customer_not_found", message="Customer not found.", status_code=400)

    existing = _get_backup_job_customer_assignment(db, cluster_id, job_id)
    if existing is not None:
        existing.customer_id = body.customer_id
    else:
        db.add(PveBackupJobCustomerAssignment(pve_cluster_id=cluster_id, job_id=job_id, customer_id=body.customer_id))

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pve_backup_job_customer_assigned",
        target_id=cluster.id,
        detail={"job_id": job_id, "customer_id": str(body.customer_id)},
    )
    db.commit()
    return _vzdump_job_to_out(db, cluster.id, job)


@router.post("/{cluster_id}/backup-jobs/{job_id}/unassign-customer", response_model=VzdumpJobOut)
def unassign_backup_job_customer(
    cluster_id: uuid.UUID,
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> VzdumpJobOut:
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    job = _get_backup_job_or_404(cluster, job_id, trust, token, settings)

    existing = _get_backup_job_customer_assignment(db, cluster_id, job_id)
    if existing is not None:
        db.delete(existing)

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pve_backup_job_customer_unassigned",
        target_id=cluster.id,
        detail={"job_id": job_id},
    )
    db.commit()
    return _vzdump_job_to_out(db, cluster.id, job)


@router.post("/{cluster_id}/lab-agent-token", response_model=LabAgentTokenOut)
def generate_lab_agent_token(
    cluster_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user=Depends(_require_platform_admin)
) -> LabAgentTokenOut:
    """Generates (or rotates) this cluster's `resurgam-lab-agent` bearer
    token (ADR 0019) -- shown here exactly once; only its bcrypt hash is
    persisted (`PveCluster.lab_agent_token_hash`). Rotating invalidates
    any previously-issued token immediately, same as every other
    credential-rotation endpoint in this codebase.
    """
    cluster = _get_cluster_or_404(db, cluster_id)
    token = generate_appliance_run_token()
    cluster.lab_agent_token_hash = hash_appliance_run_token(token)
    _record_audit(
        db, request, actor_user_id=user.user_id, action="pve_cluster_lab_agent_token_rotated", target_id=cluster.id
    )
    db.commit()
    return LabAgentTokenOut(token=token)


@router.post("/{cluster_id}/disable", response_model=PveClusterOut)
def disable_pve_cluster(
    cluster_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> PveClusterOut:
    cluster = _get_cluster_or_404(db, cluster_id)
    cluster.disabled = True
    _record_audit(db, request, actor_user_id=user.user_id, action="pve_cluster_disabled", target_id=cluster.id)
    db.commit()
    db.refresh(cluster)
    return _to_out(db, cluster, settings)


@router.post("/{cluster_id}/enable", response_model=PveClusterOut)
def enable_pve_cluster(
    cluster_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> PveClusterOut:
    cluster = _get_cluster_or_404(db, cluster_id)
    cluster.disabled = False
    _record_audit(db, request, actor_user_id=user.user_id, action="pve_cluster_enabled", target_id=cluster.id)
    db.commit()
    db.refresh(cluster)
    return _to_out(db, cluster, settings)


@router.delete("/{cluster_id}", status_code=204)
def delete_pve_cluster(
    cluster_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> None:
    cluster = _get_cluster_or_404(db, cluster_id)
    # Disk mounts reference their cluster (RESTRICT): an active one still has a
    # helper VM and mapped disks on it -- stop it first. Ended ones are only
    # history (every lifecycle step is in the audit log) and go with the cluster.
    active_mounts = (
        db.query(DiskMount)
        .filter(DiskMount.pve_cluster_id == cluster.id, DiskMount.status.in_(ACTIVE_STATUSES))
        .count()
    )
    if active_mounts:
        raise ResurgamApiError(
            code="pve_cluster_has_active_disk_mounts",
            message=f"{active_mounts} disk mount(s) on this cluster are still active. Stop them (Recovery → Disk "
            "Mounts) and wait until they have ended, then delete the cluster.",
            status_code=409,
        )
    secret_ref = db.get(SecretReference, cluster.api_token_secret_ref_id)
    reference_key = secret_ref.reference_key

    ended_mounts = db.query(DiskMount).filter_by(pve_cluster_id=cluster.id).delete()
    db.query(PveClusterPbsInstance).filter_by(pve_cluster_id=cluster.id).delete()
    db.query(PveClusterDatastore).filter_by(pve_cluster_id=cluster.id).delete()
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pve_cluster_deleted",
        target_id=cluster.id,
        detail={"ended_disk_mounts_removed": ended_mounts} if ended_mounts else None,
    )
    db.delete(cluster)
    db.flush()

    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    provider.delete(reference_key)
    db.delete(secret_ref)

    db.commit()
