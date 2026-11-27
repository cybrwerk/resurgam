import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from resurgam_api.auth.dependency import get_current_user, require_role
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.scope import visible_customer_ids
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.billing import PricingPlan
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.tenancy import Customer
from resurgam_api.schemas.pricing_plan import PricingPlanCreateRequest, PricingPlanOut, PricingPlanUpdateRequest

router = APIRouter(prefix="/pricing-plans", tags=["pricing-plans"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)


def _record_audit(db: Session, request: Request, *, actor_user_id, action: str, target_id, detail=None) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            target_type="pricing_plan",
            target_id=str(target_id),
            correlation_id=getattr(request.state, "correlation_id", None),
            detail=detail,
        )
    )


def get_plan_in_scope_or_404(db: Session, plan_id: uuid.UUID, user: AuthenticatedUser) -> PricingPlan:
    plan = db.get(PricingPlan, plan_id)
    if plan is None:
        raise ResurgamApiError(code="not_found", message="Pricing plan not found.", status_code=404)

    visible = visible_customer_ids(user)
    if visible is not None and (plan.customer_id is None or plan.customer_id not in visible):
        # 404, not 403: don't confirm existence of an out-of-scope object.
        # The global plan (customer_id is None) is platform_admin-only.
        raise ResurgamApiError(code="not_found", message="Pricing plan not found.", status_code=404)
    return plan


@router.post("", response_model=PricingPlanOut)
def create_pricing_plan(
    body: PricingPlanCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(_require_platform_admin),
) -> PricingPlanOut:
    """Not from the manifest — a future billing feature (data model + CRUD
    only in this slice, no invoice/report generation yet). `customer_id`
    null creates/targets the global default plan; at most one global plan
    and at most one plan per customer are enforced at the database level
    (partial unique indexes, migration 0009), checked here first for a
    friendly `409` instead of a raw constraint-violation error.
    """
    if body.customer_id is not None and db.get(Customer, body.customer_id) is None:
        raise ResurgamApiError(code="customer_not_found", message="Customer not found.", status_code=400)

    existing = db.query(PricingPlan).filter_by(customer_id=body.customer_id).one_or_none()
    if existing is not None:
        code = "pricing_plan_customer_exists" if body.customer_id else "pricing_plan_global_exists"
        message = (
            "This customer already has a pricing plan." if body.customer_id else "A global pricing plan already exists."
        )
        raise ResurgamApiError(code=code, message=message, status_code=409)

    plan = PricingPlan(
        customer_id=body.customer_id,
        name=body.name,
        price_per_vm_month=body.price_per_vm_month,
        price_per_ct_month=body.price_per_ct_month,
        price_per_host_month=body.price_per_host_month,
        price_per_gb_month=body.price_per_gb_month,
        price_per_s3_gb_month=body.price_per_s3_gb_month,
        currency=body.currency,
    )
    db.add(plan)
    db.flush()

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pricing_plan_created",
        target_id=plan.id,
        detail={"customer_id": str(body.customer_id) if body.customer_id else None, "name": body.name},
    )
    db.commit()
    db.refresh(plan)
    return PricingPlanOut.model_validate(plan)


@router.get("", response_model=list[PricingPlanOut])
def list_pricing_plans(
    db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> list[PricingPlanOut]:
    visible = visible_customer_ids(user)
    query = db.query(PricingPlan).order_by(PricingPlan.name)
    if visible is not None:
        # The global plan (customer_id is None) never matches an IN(...)
        # filter — it stays platform_admin-only, same as get_plan_in_scope_or_404.
        query = query.filter(PricingPlan.customer_id.in_(visible))
    return [PricingPlanOut.model_validate(p) for p in query.all()]


@router.get("/{plan_id}", response_model=PricingPlanOut)
def get_pricing_plan(
    plan_id: uuid.UUID, db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> PricingPlanOut:
    return PricingPlanOut.model_validate(get_plan_in_scope_or_404(db, plan_id, user))


@router.patch("/{plan_id}", response_model=PricingPlanOut)
def update_pricing_plan(
    plan_id: uuid.UUID,
    body: PricingPlanUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(_require_platform_admin),
) -> PricingPlanOut:
    """UX-audit finding (2026-09-24): the page could create and delete a
    plan but never edit one -- a price correction meant delete-then-recreate,
    losing `created_at` and momentarily leaving the customer with no plan
    at all. `customer_id`/scope stays immutable here (change it by deleting
    and recreating) since the create endpoint's one-plan-per-customer/global
    uniqueness check isn't re-derivable mid-PATCH without duplicating it.
    """
    plan = db.get(PricingPlan, plan_id)
    if plan is None:
        raise ResurgamApiError(code="not_found", message="Pricing plan not found.", status_code=404)

    updates = body.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(plan, field, value)

    audit_detail = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in updates.items()}
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pricing_plan_updated",
        target_id=plan.id,
        detail=audit_detail,
    )
    db.commit()
    db.refresh(plan)
    return PricingPlanOut.model_validate(plan)


@router.delete("/{plan_id}", status_code=204)
def delete_pricing_plan(
    plan_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user=Depends(_require_platform_admin)
) -> None:
    plan = db.get(PricingPlan, plan_id)
    if plan is None:
        raise ResurgamApiError(code="not_found", message="Pricing plan not found.", status_code=404)

    _record_audit(db, request, actor_user_id=user.user_id, action="pricing_plan_deleted", target_id=plan_id)
    db.delete(plan)
    db.commit()
