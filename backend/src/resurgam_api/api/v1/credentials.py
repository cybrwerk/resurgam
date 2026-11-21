import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from resurgam_api.auth.dependency import require_role
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.pbs import PbsInstance
from resurgam_api.models.pve import PveCluster
from resurgam_api.models.secrets import SecretReference
from resurgam_api.schemas.credentials import CredentialOut, RotateCredentialRequest
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

router = APIRouter(prefix="/credentials", tags=["credentials"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)


def _record_audit(db: Session, request: Request, *, actor_user_id, action: str, target_id, detail=None) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            target_type="secret_reference",
            target_id=str(target_id),
            correlation_id=getattr(request.state, "correlation_id", None),
            detail=detail,
        )
    )


def _credential_rows(db: Session) -> list[CredentialOut]:
    """Resolved *from* PbsInstance/PveCluster, not from a generic scan of
    `secret_references` -- that table also holds per-user TOTP secrets
    (`mfa.py`), which must never surface on an infra-admin credentials
    page. Only rows actually referenced by one of these two tables' own
    `api_token_secret_ref_id` FK are infra credentials at all.
    """
    rows: list[CredentialOut] = []

    pbs_instances = db.query(PbsInstance).all()
    pve_clusters = db.query(PveCluster).all()
    secret_ref_ids = {i.api_token_secret_ref_id for i in pbs_instances} | {
        c.api_token_secret_ref_id for c in pve_clusters
    }
    secret_refs = {s.id: s for s in db.query(SecretReference).filter(SecretReference.id.in_(secret_ref_ids)).all()}

    for instance in pbs_instances:
        ref = secret_refs.get(instance.api_token_secret_ref_id)
        if ref is None:
            continue
        rows.append(
            CredentialOut(
                secret_reference_id=ref.id,
                description=ref.description,
                owner_type="pbs_instance",
                owner_id=instance.id,
                owner_label=instance.display_name,
                created_at=ref.created_at,
                last_rotated_at=ref.last_rotated_at,
            )
        )
    for cluster in pve_clusters:
        ref = secret_refs.get(cluster.api_token_secret_ref_id)
        if ref is None:
            continue
        rows.append(
            CredentialOut(
                secret_reference_id=ref.id,
                description=ref.description,
                owner_type="pve_cluster",
                owner_id=cluster.id,
                owner_label=cluster.display_name,
                created_at=ref.created_at,
                last_rotated_at=ref.last_rotated_at,
            )
        )
    rows.sort(key=lambda r: r.owner_label)
    return rows


@router.get("", response_model=list[CredentialOut])
def list_credentials(db: Session = Depends(get_db), _user=Depends(_require_platform_admin)) -> list[CredentialOut]:
    """Manifest §6.1/§6.2/§14 -- every PBS/PVE API token's `SecretReference`
    (ADR 0005), one row per PBS instance / PVE cluster. Never returns the
    decrypted token itself; only the pointer/metadata already safe to
    show (description, owner, when it was first stored).
    """
    return _credential_rows(db)


@router.post("/{secret_reference_id}/rotate", response_model=CredentialOut)
def rotate_credential(
    secret_reference_id: uuid.UUID,
    body: RotateCredentialRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> CredentialOut:
    """A second, centralized rotation entry point alongside each entity's
    own PATCH endpoint (`pbs_instances.py`/`pve_clusters.py`, unchanged --
    this is additive, not a replacement) -- for rotating a token without
    touching anything else about the owning PBS instance/PVE cluster.
    Deliberately re-resolves the credential through `_credential_rows`
    rather than trusting `secret_reference_id` directly: that's what
    guarantees this can only ever rotate an infra token, never a user's
    TOTP secret, even though both live in the same `secret_references`
    table.
    """
    matching = [r for r in _credential_rows(db) if r.secret_reference_id == secret_reference_id]
    if not matching:
        raise ResurgamApiError(code="not_found", message="Credential not found.", status_code=404)
    credential = matching[0]

    secret_ref = db.get(SecretReference, secret_reference_id)
    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    provider.store(secret_ref.reference_key, body.new_value)
    secret_ref.last_rotated_at = datetime.now(UTC)

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="credential_rotated",
        target_id=secret_reference_id,
        detail={"owner_type": credential.owner_type, "owner_id": str(credential.owner_id)},
    )
    db.commit()
    return credential.model_copy(update={"last_rotated_at": secret_ref.last_rotated_at})
