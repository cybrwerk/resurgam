import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from resurgam_api.auth.dependency import get_current_user, require_role
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.scope import visible_customer_ids
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.inventory import ProtectionPolicy
from resurgam_api.models.tenancy import Customer
from resurgam_api.schemas.protection_policy import ProtectionPolicyCreateRequest, ProtectionPolicyOut

router = APIRouter(prefix="/protection-policies", tags=["protection-policies"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)


def _record_audit(db: Session, request: Request, *, actor_user_id, action: str, target_id, detail=None) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            target_type="protection_policy",
            target_id=str(target_id),
            correlation_id=getattr(request.state, "correlation_id", None),
            detail=detail,
        )
    )


def get_policy_in_scope_or_404(db: Session, policy_id: uuid.UUID, user: AuthenticatedUser) -> ProtectionPolicy:
    policy = db.get(ProtectionPolicy, policy_id)
    if policy is None:
        raise ResurgamApiError(code="not_found", message="Protection policy not found.", status_code=404)

    visible = visible_customer_ids(user)
    if visible is not None and policy.customer_id not in visible:
        # 404, not 403: don't confirm existence of an out-of-scope object.
        raise ResurgamApiError(code="not_found", message="Protection policy not found.", status_code=404)
    return policy


@router.post("", response_model=ProtectionPolicyOut)
def create_protection_policy(
    body: ProtectionPolicyCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(_require_platform_admin),
) -> ProtectionPolicyOut:
    if db.get(Customer, body.customer_id) is None:
        raise ResurgamApiError(code="customer_not_found", message="Customer not found.", status_code=400)

    if db.query(ProtectionPolicy).filter_by(customer_id=body.customer_id, name=body.name).one_or_none() is not None:
        raise ResurgamApiError(
            code="protection_policy_name_taken",
            message=f"A protection policy named {body.name!r} already exists for this customer.",
            status_code=409,
        )

    policy = ProtectionPolicy(
        customer_id=body.customer_id,
        name=body.name,
        expected_interval_hours=body.expected_interval_hours,
        max_time_since_last_backup_hours=body.max_time_since_last_backup_hours,
        expected_verify_deadline_hours=body.expected_verify_deadline_hours,
        min_snapshot_count=body.min_snapshot_count,
        sync_dr_required=body.sync_dr_required,
    )
    db.add(policy)
    db.flush()

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="protection_policy_created",
        target_id=policy.id,
        detail={"customer_id": str(body.customer_id), "name": body.name},
    )
    db.commit()
    db.refresh(policy)
    return ProtectionPolicyOut.model_validate(policy)


@router.get("", response_model=list[ProtectionPolicyOut])
def list_protection_policies(
    db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> list[ProtectionPolicyOut]:
    visible = visible_customer_ids(user)
    query = db.query(ProtectionPolicy).order_by(ProtectionPolicy.name)
    if visible is not None:
        query = query.filter(ProtectionPolicy.customer_id.in_(visible))
    return [ProtectionPolicyOut.model_validate(p) for p in query.all()]


@router.get("/{policy_id}", response_model=ProtectionPolicyOut)
def get_protection_policy(
    policy_id: uuid.UUID, db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> ProtectionPolicyOut:
    return ProtectionPolicyOut.model_validate(get_policy_in_scope_or_404(db, policy_id, user))


@router.delete("/{policy_id}", status_code=204)
def delete_protection_policy(
    policy_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user=Depends(_require_platform_admin)
) -> None:
    policy = db.get(ProtectionPolicy, policy_id)
    if policy is None:
        raise ResurgamApiError(code="not_found", message="Protection policy not found.", status_code=404)

    _record_audit(db, request, actor_user_id=user.user_id, action="protection_policy_deleted", target_id=policy_id)
    db.delete(policy)
    db.commit()
