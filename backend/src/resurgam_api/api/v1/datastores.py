import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from resurgam_api import encryption_keys
from resurgam_api.auth.dependency import get_current_user, require_role
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.scope import visible_customer_ids
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.datastore import Datastore, DatastoreCustomerAssignment
from resurgam_api.models.encryption_key import EncryptionKey
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.inventory import BackupGroup, BackupSnapshot
from resurgam_api.models.secrets import SecretReference
from resurgam_api.models.tenancy import Customer
from resurgam_api.schemas.datastore import AssignCustomerRequest, DatastoreOut, SetDatastoreEncryptionKeyRequest
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

router = APIRouter(prefix="/datastores", tags=["datastores"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)


def _record_audit(db: Session, request: Request, *, actor_user_id, action: str, target_id, detail=None) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            target_type="datastore",
            target_id=str(target_id),
            correlation_id=getattr(request.state, "correlation_id", None),
            detail=detail,
        )
    )


def get_datastore_assignment(db: Session, datastore_id: uuid.UUID) -> DatastoreCustomerAssignment | None:
    return db.query(DatastoreCustomerAssignment).filter_by(datastore_id=datastore_id).one_or_none()


def to_out(db: Session, datastore: Datastore) -> DatastoreOut:
    assignment = get_datastore_assignment(db, datastore.id)
    return DatastoreOut(
        id=datastore.id,
        pbs_instance_id=datastore.pbs_instance_id,
        name=datastore.name,
        path=datastore.path,
        comment=datastore.comment,
        created_at=datastore.created_at,
        customer_id=assignment.customer_id if assignment else None,
        role=assignment.role if assignment else None,
        encryption_key_configured=datastore.encryption_key_secret_ref_id is not None,
        removed_from_pbs_at=datastore.removed_from_pbs_at,
        **datastore_encryption(db, datastore.id),
    )


def datastore_encryption(db: Session, datastore_id: uuid.UUID) -> dict:
    """Key fingerprints in use by the datastore's current backups (ADR 0031)."""
    current = (
        db.query(BackupSnapshot.key_fingerprint)
        .join(BackupGroup, BackupGroup.id == BackupSnapshot.backup_group_id)
        .filter(
            BackupGroup.datastore_id == datastore_id,
            BackupGroup.superseded_at.is_(None),
            BackupGroup.removed_from_pbs_at.is_(None),
            BackupSnapshot.key_fingerprint.isnot(None),
        )
    )
    in_use = sorted({row[0] for row in current.distinct().all()})
    return {
        "key_fingerprints_in_use": in_use,
        "keys_missing": encryption_keys.fingerprints_without_key(db, in_use),
        "encrypted_snapshot_count": current.count(),
    }


def get_datastore_in_scope_or_404(db: Session, datastore_id: uuid.UUID, user: AuthenticatedUser) -> Datastore:
    datastore = db.get(Datastore, datastore_id)
    if datastore is None:
        raise ResurgamApiError(code="not_found", message="Datastore not found.", status_code=404)

    visible = visible_customer_ids(user)
    if visible is not None:
        assignment = get_datastore_assignment(db, datastore.id)
        if assignment is None or assignment.customer_id not in visible:
            # 404, not 403: don't confirm existence of an out-of-scope object.
            raise ResurgamApiError(code="not_found", message="Datastore not found.", status_code=404)
    return datastore


@router.get("", response_model=list[DatastoreOut])
def list_datastores_endpoint(
    db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> list[DatastoreOut]:
    visible = visible_customer_ids(user)
    datastores = db.query(Datastore).order_by(Datastore.created_at).all()

    if visible is None:
        return [to_out(db, d) for d in datastores]

    result = []
    for d in datastores:
        assignment = get_datastore_assignment(db, d.id)
        if assignment is not None and assignment.customer_id in visible:
            result.append(to_out(db, d))
    return result


@router.get("/{datastore_id}", response_model=DatastoreOut)
def get_datastore(
    datastore_id: uuid.UUID, db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> DatastoreOut:
    return to_out(db, get_datastore_in_scope_or_404(db, datastore_id, user))


@router.post("/{datastore_id}/assign-customer", response_model=DatastoreOut)
def assign_customer(
    datastore_id: uuid.UUID,
    body: AssignCustomerRequest,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(_require_platform_admin),
) -> DatastoreOut:
    datastore = db.get(Datastore, datastore_id)
    if datastore is None:
        raise ResurgamApiError(code="not_found", message="Datastore not found.", status_code=404)

    if db.get(Customer, body.customer_id) is None:
        raise ResurgamApiError(code="customer_not_found", message="Customer not found.", status_code=400)

    existing = get_datastore_assignment(db, datastore_id)
    if existing is not None:
        existing.customer_id = body.customer_id
        existing.role = body.role
    else:
        db.add(DatastoreCustomerAssignment(datastore_id=datastore_id, customer_id=body.customer_id, role=body.role))

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="datastore_customer_assigned",
        target_id=datastore_id,
        detail={"customer_id": str(body.customer_id), "role": body.role},
    )
    db.commit()
    return to_out(db, datastore)


@router.post("/{datastore_id}/unassign-customer", response_model=DatastoreOut)
def unassign_customer(
    datastore_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user=Depends(_require_platform_admin)
) -> DatastoreOut:
    datastore = db.get(Datastore, datastore_id)
    if datastore is None:
        raise ResurgamApiError(code="not_found", message="Datastore not found.", status_code=404)

    existing = get_datastore_assignment(db, datastore_id)
    if existing is not None:
        db.delete(existing)

    _record_audit(
        db, request, actor_user_id=user.user_id, action="datastore_customer_unassigned", target_id=datastore_id
    )
    db.commit()
    return to_out(db, datastore)


@router.delete("/{datastore_id}", status_code=204)
def delete_datastore(
    datastore_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user=Depends(_require_platform_admin)
) -> None:
    datastore = db.get(Datastore, datastore_id)
    if datastore is None:
        raise ResurgamApiError(code="not_found", message="Datastore not found.", status_code=404)

    _record_audit(db, request, actor_user_id=user.user_id, action="datastore_deleted", target_id=datastore_id)
    db.delete(datastore)
    db.commit()


@router.post("/{datastore_id}/encryption-key", response_model=DatastoreOut, deprecated=True)
def set_datastore_encryption_key(
    datastore_id: uuid.UUID,
    body: SetDatastoreEncryptionKeyRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> DatastoreOut:
    """Deprecated (ADR 0031): use POST `/encryption-keys`. Kept so existing
    scripts keep working: the keyfile goes into the key store (by its
    fingerprint, assigned to this datastore's tenant), not onto the datastore --
    RESURGAM picks the key per backup, so one datastore can hold backups with
    different keys and unencrypted ones. Uploading a key that is already stored
    is accepted and changes nothing."""
    datastore = db.get(Datastore, datastore_id)
    if datastore is None:
        raise ResurgamApiError(code="not_found", message="Datastore not found.", status_code=404)
    try:
        parsed = encryption_keys.parse_keyfile(body.keyfile_content)
    except encryption_keys.KeyfileError as exc:
        raise ResurgamApiError(code=exc.code, message=exc.message, status_code=400) from exc
    if db.query(EncryptionKey.id).filter_by(fingerprint=parsed.fingerprint).first() is None:
        assignment = get_datastore_assignment(db, datastore.id)
        key = encryption_keys.store_key(
            db,
            settings,
            body.keyfile_content,
            label=f"Datastore {datastore.name}",
            customer_id=assignment.customer_id if assignment else None,
            user_id=user.user_id,
        )
        db.add(
            AuditEvent(
                actor_user_id=user.user_id,
                action="encryption_key_uploaded",
                target_type="encryption_key",
                target_id=str(key.id),
                correlation_id=getattr(request.state, "correlation_id", None),
                detail={"fingerprint": key.fingerprint, "via_datastore": str(datastore.id)},
            )
        )
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="datastore_encryption_key_set",
        target_id=datastore_id,
        detail={"fingerprint": parsed.fingerprint},
    )
    db.commit()
    db.refresh(datastore)
    return to_out(db, datastore)


@router.delete("/{datastore_id}/encryption-key", response_model=DatastoreOut, deprecated=True)
def delete_datastore_encryption_key(
    datastore_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> DatastoreOut:
    """Deprecated (ADR 0031): removes a key still attached to this datastore from
    before the key store (the collector moves those into the store by itself).
    400 `no_encryption_key` otherwise -- delete stored keys with DELETE
    `/encryption-keys/{id}`."""
    datastore = db.get(Datastore, datastore_id)
    if datastore is None:
        raise ResurgamApiError(code="not_found", message="Datastore not found.", status_code=404)
    if datastore.encryption_key_secret_ref_id is None:
        raise ResurgamApiError(
            code="no_encryption_key",
            message="This datastore has no key of its own. Keys are managed under Administration -> Encryption keys "
            "(DELETE /encryption-keys/{id}).",
            status_code=400,
        )

    secret_ref = db.get(SecretReference, datastore.encryption_key_secret_ref_id)
    reference_key = secret_ref.reference_key

    datastore.encryption_key_secret_ref_id = None
    _record_audit(
        db, request, actor_user_id=user.user_id, action="datastore_encryption_key_removed", target_id=datastore_id
    )
    db.flush()

    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    provider.delete(reference_key)
    db.delete(secret_ref)

    db.commit()
    db.refresh(datastore)
    return to_out(db, datastore)
