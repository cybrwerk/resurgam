"""PBS-side job administration -- Prune, Verify, and (later) Sync jobs and
Remotes (docs/adr/0023-backup-and-maintenance-job-administration). A separate router file from
`pbs_instances.py` purely for size (that file is already substantial);
nests under the same `/pbs-instances/{instance_id}/...` URL space as the
existing maintenance-mode endpoints there, matching PBS's own real scoping
(a job's `store` field ties it to one datastore *within* a PBS instance,
the instance being where the token/connection actually lives).

No new DB tables -- live pass-through only, following ADR 0018's explicit
precedent for this whole class of feature: PBS remains the system of
record for its own job config. "Import" means the existing list becomes
clickable into a real edit form (a live `GET .../{id}`), not copying
anything into a RESURGAM-owned table.

Capability gating is computed live per instance via `GET
/access/permissions` (`connectivity.pbs_api.get_own_permissions`) --
mirrors `pve_clusters.py`'s identical PVE-side design exactly. Checked
live against the real token this backend actually uses for its own PBS
writes (2026-09-26): it already holds `Datastore.Modify`/`Datastore.Prune`/
`Datastore.Verify`/`Datastore.Backup` plus the full `Remote.*` set at `/`,
so this environment reveals no permission gap the way ADR 0018's PVE
discovery did -- the exact PBS-side privilege-to-endpoint mapping here is
the project's best-grounded expectation from PBS's own documented roles,
not a live-confirmed 403 the way the PVE side was. A write that still
403s for a reason this couldn't predict surfaces PBS's real raw error
text to the operator directly, same discipline as the PVE side.
"""

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from resurgam_api.auth.dependency import require_role
from resurgam_api.config import Settings, get_settings
from resurgam_api.connectivity.models import PbsPruneJob, PbsVerifyJob, RemoteSyncJob, TrustConfig
from resurgam_api.connectivity.pbs_api import (
    PRUNE_JOB_DELETABLE,
    SYNC_JOB_DELETABLE,
    VERIFY_JOB_DELETABLE,
    PbsWriteError,
    create_prune_job,
    create_remote,
    create_sync_job,
    create_verify_job,
    delete_prune_job,
    delete_remote,
    delete_sync_job,
    delete_verify_job,
    get_own_permissions,
    get_prune_job,
    get_sync_job,
    get_verify_job,
    list_prune_jobs,
    list_remotes,
    list_sync_jobs,
    list_verify_jobs,
    update_prune_job,
    update_remote,
    update_sync_job,
    update_verify_job,
)
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.pbs import PbsInstance, TlsTrustMode
from resurgam_api.models.secrets import SecretReference
from resurgam_api.models.sync import SyncJob
from resurgam_api.schemas.pbs import (
    PbsCapabilitiesOut,
    PruneJobCreateRequest,
    PruneJobOut,
    PruneJobUpdateRequest,
    RemoteCreateRequest,
    RemoteOut,
    RemoteUpdateRequest,
    SyncJobCreateRequest,
    SyncJobOut,
    SyncJobUpdateRequest,
    VerifyJobCreateRequest,
    VerifyJobOut,
    VerifyJobUpdateRequest,
)
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

router = APIRouter(prefix="/pbs-instances", tags=["pbs-jobs"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)


def _record_audit(db: Session, request: Request, *, actor_user_id, action: str, target_id, detail=None) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            target_type="pbs_instance",
            target_id=str(target_id),
            correlation_id=getattr(request.state, "correlation_id", None),
            detail=detail,
        )
    )


def _get_instance_or_404(db: Session, instance_id: uuid.UUID) -> PbsInstance:
    instance = db.get(PbsInstance, instance_id)
    if instance is None:
        raise ResurgamApiError(code="not_found", message="PBS instance not found.", status_code=404)
    return instance


def _instance_trust_and_token(db: Session, settings: Settings, instance: PbsInstance) -> tuple[TrustConfig, str]:
    secret_ref = db.get(SecretReference, instance.api_token_secret_ref_id)
    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    token = provider.retrieve(secret_ref.reference_key)
    trust = TrustConfig(
        trust_mode=TlsTrustMode(instance.tls_trust_mode), fingerprint_sha256=instance.tls_fingerprint_sha256
    )
    return trust, token


def _capabilities_from_permissions(
    privileges: dict, *, read_privs: set[str], write_privs: set[str]
) -> PbsCapabilitiesOut:
    """Checks the token's `/`-scoped grants (job *management*, not the
    job's own scheduled *run*, which executes under the job's configured
    `owner`, not this token) against whichever privilege sets the caller
    believes this job category needs -- see this module's own docstring
    for why these sets are an informed expectation, not a live-confirmed
    fact, on this particular environment.
    """
    root = privileges.get("/", {})
    missing_write = sorted(p for p in write_privs if not root.get(p))
    return PbsCapabilitiesOut(
        can_read=all(bool(root.get(p)) for p in read_privs),
        can_write=not missing_write,
        missing_write_privileges=missing_write,
    )


def _split_update(
    body,
    *,
    field_map: dict[str, str],
    deletable: frozenset[str],
    bool_fields: frozenset[str] = frozenset(),
    job_label: str,
) -> tuple[dict, list[str]]:
    """Turn a partial-update request into PBS's `(changes, delete)` pair.

    Only fields the client actually sent count (`model_fields_set`), so an
    omitted field is left untouched on PBS while an explicit `null` -- or
    an empty/blank string, which is what a cleared form input produces --
    becomes an entry in PBS's `delete` list. Booleans go on the wire as
    `1`/`0`. A clear request for a property PBS cannot delete (e.g. a
    prune job's `schedule`) is rejected with 400 instead of being silently
    dropped. An all-omitted body is 400 `no_changes`.
    """
    changes: dict = {}
    delete: list[str] = []
    for field_name in sorted(body.model_fields_set):
        wire_name = field_map.get(field_name)
        if wire_name is None:
            continue
        value = getattr(body, field_name)
        if isinstance(value, str):
            value = value.strip() or None
        if value is None:
            if wire_name not in deletable:
                raise ResurgamApiError(
                    code="field_not_clearable",
                    message=(
                        f"'{field_name}' cannot be cleared on a PBS {job_label}: PBS requires it. "
                        f"Send a new value instead, or omit the field to keep the current one."
                    ),
                    status_code=400,
                )
            delete.append(wire_name)
        elif field_name in bool_fields:
            changes[wire_name] = 1 if value else 0
        else:
            changes[wire_name] = value
    if not changes and not delete:
        raise ResurgamApiError(
            code="no_changes",
            message="No fields to update. Send at least one field (a value to set, or null to clear it).",
            status_code=400,
        )
    return changes, delete


def _update_audit_detail(job_id: str, changes: dict, delete: list[str]) -> dict:
    detail = {"job_id": job_id, "changed_fields": sorted(set(changes) | set(delete))}
    if delete:
        detail["cleared_fields"] = sorted(delete)
    return detail


def _get_capabilities(
    db: Session, settings: Settings, instance: PbsInstance, *, read_privs: set[str], write_privs: set[str]
) -> PbsCapabilitiesOut:
    trust, token = _instance_trust_and_token(db, settings, instance)
    result = get_own_permissions(
        instance.host, instance.port, trust, token, timeout=settings.connectivity_timeout_seconds
    )
    if not result.reachable:
        raise ResurgamApiError(
            code="pbs_unreachable", message=result.error or "PBS instance unreachable", status_code=502
        )
    return _capabilities_from_permissions(result.privileges, read_privs=read_privs, write_privs=write_privs)


# --- Prune jobs -------------------------------------------------------------


@router.get("/{instance_id}/prune-jobs/capabilities", response_model=PbsCapabilitiesOut)
def get_prune_job_capabilities(
    instance_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> PbsCapabilitiesOut:
    """Whether this instance's PBS token may list (`Datastore.Audit`) and
    create/edit/delete (`Datastore.Modify`) prune jobs, computed live from
    PBS's `GET /access/permissions`. `missing_write_privileges` names
    exactly what to grant when editing is not possible.
    """
    instance = _get_instance_or_404(db, instance_id)
    return _get_capabilities(db, settings, instance, read_privs={"Datastore.Audit"}, write_privs={"Datastore.Modify"})


# --- Garbage collection -----------------------------------------------------
#
# GC is a schedule on the datastore config, read/written by the
# `.../datastores/{name}/gc-schedule` endpoints in `pbs_instances.py`.
# Only the capability check lives here, next to the other job categories.


@router.get("/{instance_id}/gc-schedule/capabilities", response_model=PbsCapabilitiesOut)
def get_gc_schedule_capabilities(
    instance_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> PbsCapabilitiesOut:
    """Whether this instance's PBS token may read (`Datastore.Audit`) and
    set or remove (`Datastore.Modify`) a datastore's garbage-collection
    schedule. PBS's `PUT /config/datastore/{name}` requires
    `Datastore.Modify` (pbs.proxmox.com/docs/api-viewer). Computed live
    from `GET /access/permissions`, same as the job categories.
    """
    instance = _get_instance_or_404(db, instance_id)
    return _get_capabilities(db, settings, instance, read_privs={"Datastore.Audit"}, write_privs={"Datastore.Modify"})


@router.get("/{instance_id}/datastores/{name}/prune-jobs", response_model=list[PruneJobOut])
def list_datastore_prune_jobs(
    instance_id: uuid.UUID,
    name: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> list[PruneJobOut]:
    """Live-probe this datastore's own configured Prune Jobs -- PBS lists
    every prune job on the whole instance in one call; filtered here down
    to `name`'s own jobs so RESURGAM's API stays datastore-scoped like
    the rest of this router.
    """
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    result = list_prune_jobs(instance.host, instance.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    if not result.reachable:
        raise ResurgamApiError(
            code="pbs_unreachable", message=result.error or "PBS instance unreachable", status_code=502
        )
    return [PruneJobOut(**vars(j)) for j in result.jobs if j.store == name]


@router.get("/{instance_id}/prune-jobs/{job_id}", response_model=PruneJobOut)
def get_pbs_prune_job(
    instance_id: uuid.UUID,
    job_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> PruneJobOut:
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    job = get_prune_job(
        instance.host, instance.port, trust, token, settings.connectivity_timeout_seconds, job_id=job_id
    )
    if job is None:
        raise ResurgamApiError(code="pbs_prune_job_not_found", message=f"job {job_id} not found", status_code=404)
    return PruneJobOut(**vars(job))


@router.post("/{instance_id}/datastores/{name}/prune-jobs", response_model=PruneJobOut)
def create_datastore_prune_job(
    instance_id: uuid.UUID,
    name: str,
    body: PruneJobCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> PruneJobOut:
    """This *is* the intended home for retention policy in this project's
    design (docs/adr/0023-backup-and-maintenance-job-administration) -- deliberately not a vzdump job property.
    """
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    new_job_id = f"resurgam-{uuid.uuid4().hex[:12]}"
    new_job = PbsPruneJob(id=new_job_id, store=name, **body.model_dump())
    try:
        create_prune_job(instance.host, instance.port, trust, token, settings.connectivity_timeout_seconds, job=new_job)
    except PbsWriteError as exc:
        raise ResurgamApiError(code="pbs_write_failed", message=str(exc), status_code=502) from exc

    created = get_prune_job(
        instance.host, instance.port, trust, token, settings.connectivity_timeout_seconds, job_id=new_job_id
    )
    if created is None:
        raise ResurgamApiError(code="pbs_prune_job_not_found", message="job not found after creation", status_code=502)

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pbs_prune_job_created",
        target_id=instance.id,
        detail={"job_id": created.id, "store": name, "schedule": created.schedule},
    )
    db.commit()
    return PruneJobOut(**vars(created))


@router.put("/{instance_id}/prune-jobs/{job_id}", response_model=PruneJobOut)
def update_pbs_prune_job(
    instance_id: uuid.UUID,
    job_id: str,
    body: PruneJobUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> PruneJobOut:
    """Partially update a PBS prune job. Omitted fields stay unchanged;
    fields sent as `null`/`""` are removed on PBS (PBS `delete` parameter).
    400 `field_not_clearable` for `schedule: null` (PBS requires a
    schedule), 400 `no_changes` for an empty body, 502 with PBS's own
    error text if PBS rejects the write. Returns the job as PBS now has it.
    """
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    changes, delete = _split_update(
        body,
        field_map={
            "schedule": "schedule",
            "ns": "ns",
            "keep_last": "keep-last",
            "keep_hourly": "keep-hourly",
            "keep_daily": "keep-daily",
            "keep_weekly": "keep-weekly",
            "keep_monthly": "keep-monthly",
            "keep_yearly": "keep-yearly",
            "disable": "disable",
            "comment": "comment",
        },
        deletable=PRUNE_JOB_DELETABLE,
        bool_fields=frozenset({"disable"}),
        job_label="prune job",
    )

    try:
        update_prune_job(
            instance.host,
            instance.port,
            trust,
            token,
            settings.connectivity_timeout_seconds,
            job_id=job_id,
            changes=changes,
            delete=delete,
        )
    except PbsWriteError as exc:
        raise ResurgamApiError(code="pbs_write_failed", message=str(exc), status_code=502) from exc

    job = get_prune_job(
        instance.host, instance.port, trust, token, settings.connectivity_timeout_seconds, job_id=job_id
    )
    if job is None:
        raise ResurgamApiError(
            code="pbs_prune_job_not_found", message=f"job {job_id} not found after update", status_code=502
        )

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pbs_prune_job_updated",
        target_id=instance.id,
        detail=_update_audit_detail(job_id, changes, delete),
    )
    db.commit()
    return PruneJobOut(**vars(job))


@router.delete("/{instance_id}/prune-jobs/{job_id}", status_code=204)
def delete_pbs_prune_job(
    instance_id: uuid.UUID,
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> None:
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    try:
        delete_prune_job(
            instance.host, instance.port, trust, token, settings.connectivity_timeout_seconds, job_id=job_id
        )
    except PbsWriteError as exc:
        raise ResurgamApiError(code="pbs_write_failed", message=str(exc), status_code=502) from exc

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pbs_prune_job_deleted",
        target_id=instance.id,
        detail={"job_id": job_id},
    )
    db.commit()


# --- Verify jobs -------------------------------------------------------------


@router.get("/{instance_id}/verify-jobs/capabilities", response_model=PbsCapabilitiesOut)
def get_verify_job_capabilities(
    instance_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> PbsCapabilitiesOut:
    instance = _get_instance_or_404(db, instance_id)
    return _get_capabilities(db, settings, instance, read_privs={"Datastore.Audit"}, write_privs={"Datastore.Verify"})


@router.get("/{instance_id}/datastores/{name}/verify-jobs", response_model=list[VerifyJobOut])
def list_datastore_verify_jobs(
    instance_id: uuid.UUID,
    name: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> list[VerifyJobOut]:
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    result = list_verify_jobs(instance.host, instance.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    if not result.reachable:
        raise ResurgamApiError(
            code="pbs_unreachable", message=result.error or "PBS instance unreachable", status_code=502
        )
    return [VerifyJobOut(**vars(j)) for j in result.jobs if j.store == name]


@router.get("/{instance_id}/verify-jobs/{job_id}", response_model=VerifyJobOut)
def get_pbs_verify_job(
    instance_id: uuid.UUID,
    job_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> VerifyJobOut:
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    job = get_verify_job(
        instance.host, instance.port, trust, token, settings.connectivity_timeout_seconds, job_id=job_id
    )
    if job is None:
        raise ResurgamApiError(code="pbs_verify_job_not_found", message=f"job {job_id} not found", status_code=404)
    return VerifyJobOut(**vars(job))


@router.post("/{instance_id}/datastores/{name}/verify-jobs", response_model=VerifyJobOut)
def create_datastore_verify_job(
    instance_id: uuid.UUID,
    name: str,
    body: VerifyJobCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> VerifyJobOut:
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    new_job_id = f"resurgam-{uuid.uuid4().hex[:12]}"
    new_job = PbsVerifyJob(id=new_job_id, store=name, **body.model_dump())
    try:
        create_verify_job(
            instance.host, instance.port, trust, token, settings.connectivity_timeout_seconds, job=new_job
        )
    except PbsWriteError as exc:
        raise ResurgamApiError(code="pbs_write_failed", message=str(exc), status_code=502) from exc

    created = get_verify_job(
        instance.host, instance.port, trust, token, settings.connectivity_timeout_seconds, job_id=new_job_id
    )
    if created is None:
        raise ResurgamApiError(code="pbs_verify_job_not_found", message="job not found after creation", status_code=502)

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pbs_verify_job_created",
        target_id=instance.id,
        detail={"job_id": created.id, "store": name, "schedule": created.schedule},
    )
    db.commit()
    return VerifyJobOut(**vars(created))


@router.put("/{instance_id}/verify-jobs/{job_id}", response_model=VerifyJobOut)
def update_pbs_verify_job(
    instance_id: uuid.UUID,
    job_id: str,
    body: VerifyJobUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> VerifyJobOut:
    """Partially update a PBS verify job. Omitted fields stay unchanged;
    fields sent as `null`/`""` are removed on PBS (PBS `delete` parameter)
    -- clearing `schedule` makes the job manual-only. 400 `no_changes` for
    an empty body, 502 with PBS's own error text if PBS rejects the write.
    """
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    changes, delete = _split_update(
        body,
        field_map={
            "schedule": "schedule",
            "ns": "ns",
            "outdated_after": "outdated-after",
            "ignore_verified": "ignore-verified",
            "comment": "comment",
        },
        deletable=VERIFY_JOB_DELETABLE,
        bool_fields=frozenset({"ignore_verified"}),
        job_label="verify job",
    )

    try:
        update_verify_job(
            instance.host,
            instance.port,
            trust,
            token,
            settings.connectivity_timeout_seconds,
            job_id=job_id,
            changes=changes,
            delete=delete,
        )
    except PbsWriteError as exc:
        raise ResurgamApiError(code="pbs_write_failed", message=str(exc), status_code=502) from exc

    job = get_verify_job(
        instance.host, instance.port, trust, token, settings.connectivity_timeout_seconds, job_id=job_id
    )
    if job is None:
        raise ResurgamApiError(
            code="pbs_verify_job_not_found", message=f"job {job_id} not found after update", status_code=502
        )

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pbs_verify_job_updated",
        target_id=instance.id,
        detail=_update_audit_detail(job_id, changes, delete),
    )
    db.commit()
    return VerifyJobOut(**vars(job))


@router.delete("/{instance_id}/verify-jobs/{job_id}", status_code=204)
def delete_pbs_verify_job(
    instance_id: uuid.UUID,
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> None:
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    try:
        delete_verify_job(
            instance.host, instance.port, trust, token, settings.connectivity_timeout_seconds, job_id=job_id
        )
    except PbsWriteError as exc:
        raise ResurgamApiError(code="pbs_write_failed", message=str(exc), status_code=502) from exc

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pbs_verify_job_deleted",
        target_id=instance.id,
        detail={"job_id": job_id},
    )
    db.commit()


# --- Remotes ------------------------------------------------------------------
#
# A Remote isn't tied to one datastore (it's a server-to-server trust
# relationship the *instance* holds), so unlike prune/verify/sync jobs
# these endpoints stay flat under the instance, not nested under
# `datastores/{name}`. `password` is accepted on create/update and
# forwarded straight through to PBS -- RESURGAM never stores it (see
# `connectivity.pbs_api.create_remote`'s own docstring for why this
# differs from the PBS-encryption-key case).


@router.get("/{instance_id}/remotes/capabilities", response_model=PbsCapabilitiesOut)
def get_remote_capabilities(
    instance_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> PbsCapabilitiesOut:
    instance = _get_instance_or_404(db, instance_id)
    return _get_capabilities(db, settings, instance, read_privs={"Remote.Audit"}, write_privs={"Remote.Modify"})


@router.get("/{instance_id}/remotes", response_model=list[RemoteOut])
def list_pbs_remotes(
    instance_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> list[RemoteOut]:
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    result = list_remotes(instance.host, instance.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    if not result.reachable:
        raise ResurgamApiError(
            code="pbs_unreachable", message=result.error or "PBS instance unreachable", status_code=502
        )
    return [RemoteOut(**vars(r)) for r in result.remotes]


@router.post("/{instance_id}/remotes", response_model=RemoteOut)
def create_pbs_remote(
    instance_id: uuid.UUID,
    body: RemoteCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> RemoteOut:
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    try:
        create_remote(
            instance.host,
            instance.port,
            trust,
            token,
            settings.connectivity_timeout_seconds,
            name=body.name,
            remote_host=body.host,
            auth_id=body.auth_id,
            password=body.password,
            remote_port=body.port,
            fingerprint=body.fingerprint,
            comment=body.comment,
        )
    except PbsWriteError as exc:
        raise ResurgamApiError(code="pbs_write_failed", message=str(exc), status_code=502) from exc

    result = list_remotes(instance.host, instance.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    created = next((r for r in (result.remotes or []) if r.name == body.name), None)
    if created is None:
        raise ResurgamApiError(code="pbs_remote_not_found", message="remote not found after creation", status_code=502)

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pbs_remote_created",
        target_id=instance.id,
        detail={"name": body.name, "host": body.host},
    )
    db.commit()
    return RemoteOut(**vars(created))


@router.put("/{instance_id}/remotes/{name}", response_model=RemoteOut)
def update_pbs_remote(
    instance_id: uuid.UUID,
    name: str,
    body: RemoteUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> RemoteOut:
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    changes = {
        "host": body.host,
        "auth-id": body.auth_id,
        "password": body.password,
        "port": body.port,
        "fingerprint": body.fingerprint,
        "comment": body.comment,
    }
    changes = {key: value for key, value in changes.items() if value is not None}
    if not changes:
        raise ResurgamApiError(code="no_changes", message="No fields to update.", status_code=400)

    try:
        update_remote(
            instance.host,
            instance.port,
            trust,
            token,
            settings.connectivity_timeout_seconds,
            name=name,
            changes=changes,
        )
    except PbsWriteError as exc:
        raise ResurgamApiError(code="pbs_write_failed", message=str(exc), status_code=502) from exc

    result = list_remotes(instance.host, instance.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    updated = next((r for r in (result.remotes or []) if r.name == name), None)
    if updated is None:
        raise ResurgamApiError(
            code="pbs_remote_not_found", message=f"remote {name} not found after update", status_code=502
        )

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pbs_remote_updated",
        target_id=instance.id,
        detail={"name": name, "changed_fields": sorted(k for k in changes if k != "password")},
    )
    db.commit()
    return RemoteOut(**vars(updated))


@router.delete("/{instance_id}/remotes/{name}", status_code=204)
def delete_pbs_remote(
    instance_id: uuid.UUID,
    name: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> None:
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    try:
        delete_remote(instance.host, instance.port, trust, token, settings.connectivity_timeout_seconds, name=name)
    except PbsWriteError as exc:
        raise ResurgamApiError(code="pbs_write_failed", message=str(exc), status_code=502) from exc

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pbs_remote_deleted",
        target_id=instance.id,
        detail={"name": name},
    )
    db.commit()


# --- Sync jobs ------------------------------------------------------------------
#
# Sync jobs reference an existing Remote by name -- RESURGAM treats the
# remote picker as read-only reference data here (`GET .../remotes`
# above), never authoring a remote implicitly from a sync job form.
#
# Distinct from `inventory.py`'s `GET /pbs-instances/{id}/sync-jobs`,
# which returns the Collector's DB-cached `SyncJob` rows (manifest §8,
# refreshed periodically for reporting/DR-posture purposes) -- these
# endpoints here are live pass-through admin, same "no new DB tables"
# principle as the rest of this router. Both read the same
# `connectivity.pbs_api.list_sync_jobs`, so the field-shape corrections
# this session's live PBS verification made benefit the Collector's
# ingestion too, not just this router.


@router.get("/{instance_id}/sync-jobs/capabilities", response_model=PbsCapabilitiesOut)
def get_sync_job_capabilities(
    instance_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> PbsCapabilitiesOut:
    instance = _get_instance_or_404(db, instance_id)
    return _get_capabilities(
        db, settings, instance, read_privs={"Datastore.Audit"}, write_privs={"Datastore.Backup", "Remote.Read"}
    )


@router.get("/{instance_id}/datastores/{name}/sync-jobs", response_model=list[SyncJobOut])
def list_datastore_sync_jobs(
    instance_id: uuid.UUID,
    name: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> list[SyncJobOut]:
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    result = list_sync_jobs(instance.host, instance.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    if not result.reachable:
        raise ResurgamApiError(
            code="pbs_unreachable", message=result.error or "PBS instance unreachable", status_code=502
        )
    return [SyncJobOut(**vars(j)) for j in result.sync_jobs if j.local_store == name]


@router.get("/{instance_id}/sync-jobs/{job_id}", response_model=SyncJobOut)
def get_pbs_sync_job(
    instance_id: uuid.UUID,
    job_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> SyncJobOut:
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    job = get_sync_job(instance.host, instance.port, trust, token, settings.connectivity_timeout_seconds, job_id=job_id)
    if job is None:
        raise ResurgamApiError(code="pbs_sync_job_not_found", message=f"job {job_id} not found", status_code=404)
    return SyncJobOut(**vars(job))


@router.post("/{instance_id}/datastores/{name}/sync-jobs", response_model=SyncJobOut)
def create_datastore_sync_job(
    instance_id: uuid.UUID,
    name: str,
    body: SyncJobCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> SyncJobOut:
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    new_job_id = f"resurgam-{uuid.uuid4().hex[:12]}"
    new_job = RemoteSyncJob(job_id=new_job_id, local_store=name, **body.model_dump())
    try:
        create_sync_job(instance.host, instance.port, trust, token, settings.connectivity_timeout_seconds, job=new_job)
    except PbsWriteError as exc:
        raise ResurgamApiError(code="pbs_write_failed", message=str(exc), status_code=502) from exc

    created = get_sync_job(
        instance.host, instance.port, trust, token, settings.connectivity_timeout_seconds, job_id=new_job_id
    )
    if created is None:
        raise ResurgamApiError(code="pbs_sync_job_not_found", message="job not found after creation", status_code=502)

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pbs_sync_job_created",
        target_id=instance.id,
        detail={"job_id": created.job_id, "store": name, "remote_store": created.remote_store},
    )
    db.commit()
    return SyncJobOut(**vars(created))


@router.put("/{instance_id}/sync-jobs/{job_id}", response_model=SyncJobOut)
def update_pbs_sync_job(
    instance_id: uuid.UUID,
    job_id: str,
    body: SyncJobUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> SyncJobOut:
    """Partially update a PBS sync job. Omitted fields stay unchanged;
    fields sent as `null`/`""` are removed on PBS (PBS `delete` parameter).
    PBS sync jobs cannot be disabled; clearing `schedule` is how to stop
    automatic runs (the job then runs only when started manually). 400
    `field_not_clearable` for `remote_store: null`, 400 `no_changes` for an
    empty body, 502 with PBS's own error text if PBS rejects the write.
    """
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    changes, delete = _split_update(
        body,
        field_map={
            "remote_store": "remote-store",
            "remote_name": "remote",
            "ns": "ns",
            "schedule": "schedule",
            "remove_vanished": "remove-vanished",
            "owner": "owner",
            "comment": "comment",
        },
        deletable=SYNC_JOB_DELETABLE,
        bool_fields=frozenset({"remove_vanished"}),
        job_label="sync job",
    )

    try:
        update_sync_job(
            instance.host,
            instance.port,
            trust,
            token,
            settings.connectivity_timeout_seconds,
            job_id=job_id,
            changes=changes,
            delete=delete,
        )
    except PbsWriteError as exc:
        raise ResurgamApiError(code="pbs_write_failed", message=str(exc), status_code=502) from exc

    job = get_sync_job(instance.host, instance.port, trust, token, settings.connectivity_timeout_seconds, job_id=job_id)
    if job is None:
        raise ResurgamApiError(
            code="pbs_sync_job_not_found", message=f"job {job_id} not found after update", status_code=502
        )

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pbs_sync_job_updated",
        target_id=instance.id,
        detail=_update_audit_detail(job_id, changes, delete),
    )
    db.commit()
    return SyncJobOut(**vars(job))


@router.delete("/{instance_id}/sync-jobs/{job_id}", status_code=204)
def delete_pbs_sync_job(
    instance_id: uuid.UUID,
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> None:
    """Delete a PBS sync job. On success the Collector's cached copy of the
    job is removed too, so it disappears from `GET /pbs-instances/{id}/
    sync-jobs`, DR posture and reports immediately. 502 with PBS's own
    error text if PBS rejects the delete (the cached copy is then kept).
    """
    instance = _get_instance_or_404(db, instance_id)
    trust, token = _instance_trust_and_token(db, settings, instance)
    try:
        delete_sync_job(
            instance.host, instance.port, trust, token, settings.connectivity_timeout_seconds, job_id=job_id
        )
    except PbsWriteError as exc:
        raise ResurgamApiError(code="pbs_write_failed", message=str(exc), status_code=502) from exc

    # Drop the Collector's cached copy right away so the deleted job stops
    # showing up in `GET /pbs-instances/{id}/sync-jobs`, DR posture and
    # reports without waiting for the next Collector run.
    db.query(SyncJob).filter(SyncJob.pbs_instance_id == instance.id, SyncJob.job_id == job_id).delete(
        synchronize_session=False
    )

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pbs_sync_job_deleted",
        target_id=instance.id,
        detail={"job_id": job_id},
    )
    db.commit()
