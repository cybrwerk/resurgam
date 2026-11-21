import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
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
from resurgam_api.api.v1.datastores import to_out as datastore_to_out
from resurgam_api.auth.dependency import require_role
from resurgam_api.collector.sync import discover_datastores, sync_pbs_instance
from resurgam_api.config import Settings, get_settings
from resurgam_api.connectivity.health import apply_health_result, check_pbs_health
from resurgam_api.connectivity.models import TrustConfig
from resurgam_api.connectivity.pbs_api import (
    PbsWriteError,
    get_datastore_gc_schedule,
    get_datastore_maintenance_mode,
    get_task_log,
    list_datastores,
    set_datastore_gc_schedule,
    set_datastore_maintenance_mode,
)
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.instant_recovery.lifecycle import active_plans_reading_from
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.datastore import Datastore
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.inventory import BackupGroup
from resurgam_api.models.pbs import ConnectionHealthStatus, PbsInstance, TlsTrustMode
from resurgam_api.models.secrets import SecretPurpose, SecretReference
from resurgam_api.net.tls_pinning import fetch_peer_certificate_sha256
from resurgam_api.schemas.connectivity import (
    RequiredPrivilegesOut,
    TestConnectionResponse,
    TlsProbeRequest,
    TlsProbeResponse,
)
from resurgam_api.schemas.datastore import DatastoreOut, ImportDatastoreRequest, RemoteDatastoreOut
from resurgam_api.schemas.inventory import (
    AclSyncOut,
    DatastoreDiscoveryOut,
    DatastoreSyncOut,
    PbsInstanceSyncOut,
    SyncJobSyncOut,
    TaskSyncOut,
)
from resurgam_api.schemas.pbs import (
    DatastoreGcScheduleOut,
    DatastoreMaintenanceModeOut,
    PbsInstanceCreateRequest,
    PbsInstanceOut,
    PbsInstanceUpdateRequest,
    SetDatastoreGcScheduleRequest,
    SetDatastoreMaintenanceModeRequest,
)
from resurgam_api.schemas.pbs_task_log import TaskLogLineOut, TaskLogOut
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

router = APIRouter(prefix="/pbs-instances", tags=["pbs-instances"])
logger = logging.getLogger("resurgam_api.pbs_instances")

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)


def _record_audit(
    db: Session,
    request: Request,
    *,
    actor_user_id,
    action: str,
    target_id,
    target_type: str = "pbs_instance",
    detail=None,
) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            correlation_id=getattr(request.state, "correlation_id", None),
            detail=detail,
        )
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
def get_pbs_required_privileges(_user=Depends(_require_platform_admin)) -> RequiredPrivilegesOut:
    """Every PBS privilege RESURGAM's API token needs (and why), the
    built-in roles RESURGAM grants its own token in credentials mode, and
    the `proxmox-backup-manager` commands to set a token up by hand. Static
    -- no PBS is contacted. Shown in the add form before connecting.
    """
    return required_privileges_out("pbs")


def _to_out(db: Session, settings: Settings, instance: PbsInstance) -> PbsInstanceOut:
    out = PbsInstanceOut.model_validate(instance)
    out.token_id = stored_token_id(db, settings, instance.api_token_secret_ref_id)
    out.token_secret_set = stored_token_secret_set(db, settings, instance.api_token_secret_ref_id)
    out.instant_recoveries_reading = _instant_recovery_datastores(db, instance)
    return out


def _instant_recovery_datastores(db: Session, instance: PbsInstance) -> list[str]:
    names = []
    for plan in active_plans_reading_from(db, pbs_instance_id=instance.id):
        group = db.get(BackupGroup, plan.backup_group_id)
        datastore = db.get(Datastore, group.datastore_id)
        names.append(datastore.name)
    return names


@router.post("", response_model=PbsInstanceOut)
def create_pbs_instance(
    body: PbsInstanceCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> PbsInstanceOut:
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

    reference_key = f"pbs-instance-token-{uuid.uuid4()}"
    secret_ref = SecretReference(
        reference_key=reference_key,
        description=f"API token for PBS instance '{body.display_name}'",
        purpose=SecretPurpose.INFRA_CREDENTIAL,
    )
    db.add(secret_ref)
    db.flush()

    if body.credentials is not None:
        trust = TrustConfig(trust_mode=body.tls_trust_mode, fingerprint_sha256=body.tls_fingerprint_sha256)
        token = provision("pbs", body.host, body.port, trust, body.credentials, settings)
    else:
        token = body.stored_token()
    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    provider.store(reference_key, token)

    instance = PbsInstance(
        display_name=body.display_name,
        location=body.location,
        host=body.host,
        port=body.port,
        tls_trust_mode=body.tls_trust_mode.value,
        tls_fingerprint_sha256=body.tls_fingerprint_sha256,
        api_token_secret_ref_id=secret_ref.id,
        last_health_status=ConnectionHealthStatus.UNKNOWN.value,
    )
    db.add(instance)
    db.flush()

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pbs_instance_created",
        target_id=instance.id,
        detail={
            "display_name": body.display_name,
            "host": body.host,
            "trust_mode": body.tls_trust_mode.value,
            "token_mode": "credentials" if body.credentials is not None else "token",
        },
    )
    db.commit()

    # Import the new PBS's datastores right away instead of after the first
    # scheduled sync (the scheduler keeps them current from then on). The
    # instance is already committed: a discovery failure must never undo the
    # create, and an unreachable PBS simply discovers nothing yet.
    try:
        discover_datastores(db, settings, instance)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("datastore discovery after creating PBS instance %s failed", instance.id)

    db.refresh(instance)
    return _to_out(db, settings, instance)


@router.get("", response_model=list[PbsInstanceOut])
def list_pbs_instances(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> list[PbsInstanceOut]:
    instances = db.query(PbsInstance).order_by(PbsInstance.created_at).all()
    return [_to_out(db, settings, i) for i in instances]


def _get_instance_or_404(db: Session, instance_id: uuid.UUID) -> PbsInstance:
    instance = db.get(PbsInstance, instance_id)
    if instance is None:
        raise ResurgamApiError(code="not_found", message="PBS instance not found.", status_code=404)
    return instance


@router.get("/{instance_id}", response_model=PbsInstanceOut)
def get_pbs_instance(
    instance_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> PbsInstanceOut:
    return _to_out(db, settings, _get_instance_or_404(db, instance_id))


@router.patch("/{instance_id}", response_model=PbsInstanceOut)
def update_pbs_instance(
    instance_id: uuid.UUID,
    body: PbsInstanceUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> PbsInstanceOut:
    instance = _get_instance_or_404(db, instance_id)

    new_host = body.host if body.host is not None else instance.host
    new_port = body.port if body.port is not None else instance.port
    new_trust_mode = body.tls_trust_mode if body.tls_trust_mode is not None else TlsTrustMode(instance.tls_trust_mode)
    new_fingerprint = (
        body.tls_fingerprint_sha256 if body.tls_fingerprint_sha256 is not None else instance.tls_fingerprint_sha256
    )

    if new_trust_mode == TlsTrustMode.FINGERPRINT and not new_fingerprint:
        raise ResurgamApiError(
            code="fingerprint_required",
            message="tls_fingerprint_sha256 is required when tls_trust_mode is 'fingerprint'.",
            status_code=400,
        )

    # Re-verify against the live host whenever the trust-relevant fields
    # actually change — same "server re-verifies" rule as create.
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

    if body.display_name is not None:
        instance.display_name = body.display_name
    if body.location is not None:
        instance.location = body.location
    instance.host = new_host
    instance.port = new_port
    instance.tls_trust_mode = new_trust_mode.value
    instance.tls_fingerprint_sha256 = new_fingerprint

    if body.credentials is not None:
        trust = TrustConfig(trust_mode=new_trust_mode, fingerprint_sha256=new_fingerprint)
        new_token = provision("pbs", new_host, new_port, trust, body.credentials, settings)
    else:
        new_token = updated_token(
            "pbs",
            retrieve_token(db, settings, instance.api_token_secret_ref_id),
            token_id=body.token_id,
            token_secret=body.token_secret,
            api_token=body.api_token,
        )
    if new_token is not None:
        secret_ref = db.get(SecretReference, instance.api_token_secret_ref_id)
        provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
        provider.store(secret_ref.reference_key, new_token)
        secret_ref.last_rotated_at = datetime.now(UTC)

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pbs_instance_updated",
        target_id=instance.id,
        detail={
            "host": new_host,
            "port": new_port,
            "api_token_rotated": new_token is not None,
            "token_mode": "credentials" if body.credentials is not None else "token",
        },
    )
    db.commit()
    db.refresh(instance)
    return _to_out(db, settings, instance)


@router.post("/{instance_id}/test-connection", response_model=TestConnectionResponse)
def test_pbs_instance_connection(
    instance_id: uuid.UUID,
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
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    result = check_pbs_health(instance.host, instance.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    apply_health_result(instance, result)

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pbs_instance_test_connection",
        target_id=instance.id,
        detail={
            "reachable": result.reachable,
            "status": result.status.value,
            "error": result.message,
            "missing_privileges": list(result.missing_privileges),
        },
    )
    db.commit()
    return health_to_response(result)


@router.post("/{instance_id}/sync", response_model=PbsInstanceSyncOut)
def sync_pbs_instance_endpoint(
    instance_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> PbsInstanceSyncOut:
    """Run the Collector for this PBS instance: automatic datastore
    discovery (new datastores imported, path/comment refreshed, datastores
    gone from PBS marked `removed_from_pbs_at`), task history, sync jobs,
    ACL, then backup inventory (groups/snapshots) for every datastore still
    present on PBS.
    On-demand, ahead of the scheduled pass (`worker/collector_scheduler.py`,
    which re-syncs every non-disabled instance on the interval configured
    in System Settings) — both update `last_collector_sync_at` the same way.
    """
    instance = _get_instance_or_404(db, instance_id)
    result = sync_pbs_instance(db, settings, instance)

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pbs_instance_synced",
        target_id=instance.id,
        detail={
            "tasks_reachable": result.tasks.reachable,
            "datastore_count": len(result.datastores),
        },
    )
    db.commit()

    return PbsInstanceSyncOut(
        tasks=TaskSyncOut(
            reachable=result.tasks.reachable,
            tasks_created=result.tasks.tasks_created,
            tasks_updated=result.tasks.tasks_updated,
            error=result.tasks.error,
        ),
        sync_jobs=SyncJobSyncOut(
            reachable=result.sync_jobs.reachable,
            sync_jobs_created=result.sync_jobs.sync_jobs_created,
            sync_jobs_updated=result.sync_jobs.sync_jobs_updated,
            sync_jobs_removed=result.sync_jobs.sync_jobs_removed,
            error=result.sync_jobs.error,
        ),
        discovery=(
            DatastoreDiscoveryOut(
                reachable=result.discovery.reachable,
                created=result.discovery.created,
                updated=result.discovery.updated,
                removed=result.discovery.removed,
                restored=result.discovery.restored,
                error=result.discovery.error,
            )
            if result.discovery is not None
            else None
        ),
        acl=AclSyncOut(
            reachable=result.acl.reachable,
            entries_synced=result.acl.entries_synced,
            error=result.acl.error,
        ),
        datastores=[
            DatastoreSyncOut(
                datastore_id=outcome.datastore_id,
                datastore_name=outcome.datastore_name,
                reachable=outcome.result.reachable,
                groups_created=outcome.result.groups_created,
                groups_updated=outcome.result.groups_updated,
                groups_removed=outcome.result.groups_removed,
                snapshots_created=outcome.result.snapshots_created,
                snapshots_updated=outcome.result.snapshots_updated,
                error=outcome.result.error,
            )
            for outcome in result.datastores
        ],
    )


@router.get("/{instance_id}/available-datastores", response_model=list[RemoteDatastoreOut])
def list_available_datastores(
    instance_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> list[RemoteDatastoreOut]:
    """Live-probe the PBS instance for its configured datastores and return
    the ones not already imported. Nothing is persisted here.
    """
    instance = _get_instance_or_404(db, instance_id)
    secret_ref = db.get(SecretReference, instance.api_token_secret_ref_id)
    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    token = provider.retrieve(secret_ref.reference_key)

    trust = TrustConfig(
        trust_mode=TlsTrustMode(instance.tls_trust_mode), fingerprint_sha256=instance.tls_fingerprint_sha256
    )
    result = list_datastores(instance.host, instance.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    if not result.reachable:
        raise upstream_error("pbs", result.error)

    imported_names = {row[0] for row in db.query(Datastore.name).filter_by(pbs_instance_id=instance_id).all()}
    return [
        RemoteDatastoreOut(name=ds.name, path=ds.path, comment=ds.comment)
        for ds in result.datastores
        if ds.name not in imported_names
    ]


def _instance_trust_and_token(db: Session, settings: Settings, instance: PbsInstance) -> tuple[TrustConfig, str]:
    secret_ref = db.get(SecretReference, instance.api_token_secret_ref_id)
    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    token = provider.retrieve(secret_ref.reference_key)
    trust = TrustConfig(
        trust_mode=TlsTrustMode(instance.tls_trust_mode), fingerprint_sha256=instance.tls_fingerprint_sha256
    )
    return trust, token


@router.get("/{instance_id}/datastores/{name}/maintenance-mode", response_model=DatastoreMaintenanceModeOut)
def get_datastore_maintenance_mode_endpoint(
    instance_id: uuid.UUID,
    name: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> DatastoreMaintenanceModeOut:
    """Live-probe one datastore's current `maintenance-mode`. Nothing
    persisted — PBS remains the system of record for its own datastore
    config, same as `list_available_datastores` above.
    """
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    result = get_datastore_maintenance_mode(
        instance.host, instance.port, trust, token, timeout=settings.connectivity_timeout_seconds, name=name
    )
    if not result.reachable:
        raise upstream_error("pbs", result.error)
    return DatastoreMaintenanceModeOut(mode=result.mode, message=result.message)


@router.put("/{instance_id}/datastores/{name}/maintenance-mode", response_model=DatastoreMaintenanceModeOut)
def set_datastore_maintenance_mode_endpoint(
    instance_id: uuid.UUID,
    name: str,
    body: SetDatastoreMaintenanceModeRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> DatastoreMaintenanceModeOut:
    """Set (or, with `mode: null`, clear) a datastore's `maintenance-mode`.
    Requires `Datastore.Modify` on the PBS token (see
    `connectivity.pbs_api.set_datastore_maintenance_mode`).
    """
    instance = _get_instance_or_404(db, instance_id)
    if body.mode is not None and not body.confirm_instant_recovery:
        active = active_plans_reading_from(db, pbs_instance_id=instance.id, datastore_name=name)
        if active:
            raise ResurgamApiError(
                code="instant_recovery_active",
                message=(
                    f"{len(active)} VM(s) run straight from backups on this datastore (Instant Recovery). "
                    "Maintenance mode can cut them off their disks; confirm to continue."
                ),
                status_code=409,
                details={
                    "restore_plans": [
                        {"id": str(p.id), "name": p.temp_name, "vmid": p.target_vmid, "node": p.target_node}
                        for p in active
                    ]
                },
            )
    trust, token = _instance_trust_and_token(db, settings, instance)
    try:
        set_datastore_maintenance_mode(
            instance.host,
            instance.port,
            trust,
            token,
            settings.connectivity_timeout_seconds,
            name=name,
            mode=body.mode,
            message=body.message,
        )
    except PbsWriteError as exc:
        raise ResurgamApiError(code="pbs_write_failed", message=str(exc), status_code=502) from exc

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pbs_datastore_maintenance_mode_set" if body.mode else "pbs_datastore_maintenance_mode_cleared",
        target_id=instance.id,
        detail={
            "datastore": name,
            "mode": body.mode,
            "message": body.message,
            **({"confirmed_instant_recovery": True} if body.mode and body.confirm_instant_recovery else {}),
        },
    )
    db.commit()
    return DatastoreMaintenanceModeOut(mode=body.mode, message=body.message if body.mode else None)


@router.get("/{instance_id}/datastores/{name}/gc-schedule", response_model=DatastoreGcScheduleOut)
def get_datastore_gc_schedule_endpoint(
    instance_id: uuid.UUID,
    name: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> DatastoreGcScheduleOut:
    """Live-probe one datastore's current `gc-schedule`. Garbage collection
    isn't a job category with its own list (docs/adr/0023-backup-and-
    maintenance-job-administration) -- just this one field on the datastore's own config.
    """
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    reachable, gc_schedule, error = get_datastore_gc_schedule(
        instance.host, instance.port, trust, token, timeout=settings.connectivity_timeout_seconds, name=name
    )
    if not reachable:
        raise upstream_error("pbs", error)
    return DatastoreGcScheduleOut(gc_schedule=gc_schedule)


@router.put("/{instance_id}/datastores/{name}/gc-schedule", response_model=DatastoreGcScheduleOut)
def set_datastore_gc_schedule_endpoint(
    instance_id: uuid.UUID,
    name: str,
    body: SetDatastoreGcScheduleRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> DatastoreGcScheduleOut:
    """Set (or, with `gc_schedule: null`, clear) a datastore's `gc-schedule`.
    Requires `Datastore.Modify` on the PBS token, same as maintenance-mode.
    """
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    try:
        set_datastore_gc_schedule(
            instance.host,
            instance.port,
            trust,
            token,
            settings.connectivity_timeout_seconds,
            name=name,
            schedule=body.gc_schedule,
        )
    except PbsWriteError as exc:
        raise ResurgamApiError(code="pbs_write_failed", message=str(exc), status_code=502) from exc

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pbs_datastore_gc_schedule_set" if body.gc_schedule else "pbs_datastore_gc_schedule_cleared",
        target_id=instance.id,
        detail={"datastore": name, "gc_schedule": body.gc_schedule},
    )
    db.commit()
    return DatastoreGcScheduleOut(gc_schedule=body.gc_schedule)


@router.get("/{instance_id}/tasks/{upid}/log", response_model=TaskLogOut)
def get_pbs_task_log(
    instance_id: uuid.UUID,
    upid: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> TaskLogOut:
    """Live-probe a single task's full log (Jobs page: click a UPID to see
    it). Confirmed live (2026-09-17) — see `connectivity.pbs_api.get_task_log`'s
    docstring. Returns the *tail* of the log, not the head; `truncated`
    tells the frontend whether the log actually has more lines than were
    returned.
    """
    instance = _get_instance_or_404(db, instance_id)
    secret_ref = db.get(SecretReference, instance.api_token_secret_ref_id)
    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    token = provider.retrieve(secret_ref.reference_key)

    trust = TrustConfig(
        trust_mode=TlsTrustMode(instance.tls_trust_mode), fingerprint_sha256=instance.tls_fingerprint_sha256
    )
    result = get_task_log(
        instance.host, instance.port, trust, token, timeout=settings.connectivity_timeout_seconds, upid=upid
    )
    if not result.reachable:
        raise upstream_error("pbs", result.error)

    return TaskLogOut(
        lines=[TaskLogLineOut(n=line.n, text=line.text) for line in (result.lines or [])],
        truncated=result.truncated,
    )


@router.post("/{instance_id}/datastores", response_model=DatastoreOut)
def import_datastore(
    instance_id: uuid.UUID,
    body: ImportDatastoreRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> DatastoreOut:
    """Import a datastore by name, re-fetching its details from PBS rather
    than trusting anything the client supplied beyond the name — the same
    "server re-verifies" pattern used for TLS fingerprint confirmation.
    """
    instance = _get_instance_or_404(db, instance_id)

    existing = db.query(Datastore).filter_by(pbs_instance_id=instance_id, name=body.name).one_or_none()
    if existing is not None:
        raise ResurgamApiError(
            code="datastore_already_imported", message=f"Datastore {body.name!r} is already imported.", status_code=409
        )

    secret_ref = db.get(SecretReference, instance.api_token_secret_ref_id)
    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    token = provider.retrieve(secret_ref.reference_key)

    trust = TrustConfig(
        trust_mode=TlsTrustMode(instance.tls_trust_mode), fingerprint_sha256=instance.tls_fingerprint_sha256
    )
    result = list_datastores(instance.host, instance.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    if not result.reachable:
        raise upstream_error("pbs", result.error)

    remote = next((ds for ds in result.datastores if ds.name == body.name), None)
    if remote is None:
        raise ResurgamApiError(
            code="datastore_not_found_on_pbs",
            message=f"Datastore {body.name!r} was not found on this PBS instance.",
            status_code=404,
        )

    datastore = Datastore(pbs_instance_id=instance_id, name=remote.name, path=remote.path, comment=remote.comment)
    db.add(datastore)
    db.flush()

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="datastore_imported",
        target_id=datastore.id,
        target_type="datastore",
        detail={"pbs_instance_id": str(instance_id), "name": remote.name},
    )
    db.commit()
    db.refresh(datastore)
    return datastore_to_out(db, datastore)


@router.post("/{instance_id}/disable", response_model=PbsInstanceOut)
def disable_pbs_instance(
    instance_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> PbsInstanceOut:
    instance = _get_instance_or_404(db, instance_id)
    instance.disabled = True
    _record_audit(db, request, actor_user_id=user.user_id, action="pbs_instance_disabled", target_id=instance.id)
    db.commit()
    db.refresh(instance)
    return _to_out(db, settings, instance)


@router.post("/{instance_id}/enable", response_model=PbsInstanceOut)
def enable_pbs_instance(
    instance_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> PbsInstanceOut:
    instance = _get_instance_or_404(db, instance_id)
    instance.disabled = False
    _record_audit(db, request, actor_user_id=user.user_id, action="pbs_instance_enabled", target_id=instance.id)
    db.commit()
    db.refresh(instance)
    return _to_out(db, settings, instance)


@router.delete("/{instance_id}", status_code=204)
def delete_pbs_instance(
    instance_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> None:
    instance = _get_instance_or_404(db, instance_id)
    secret_ref = db.get(SecretReference, instance.api_token_secret_ref_id)
    reference_key = secret_ref.reference_key

    _record_audit(db, request, actor_user_id=user.user_id, action="pbs_instance_deleted", target_id=instance.id)
    db.delete(instance)
    db.flush()

    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    provider.delete(reference_key)
    db.delete(secret_ref)

    db.commit()
