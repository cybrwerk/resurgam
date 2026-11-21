import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from resurgam_api.api.v1.customers import get_customer_in_scope_or_404
from resurgam_api.auth.dependency import get_current_user, require_role
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.identity import RoleKey, User
from resurgam_api.models.leg_applicability import ThreeTwoOneOneZeroLegSetting
from resurgam_api.schemas.leg_applicability import ALL_LEGS, LegApplicabilityOut, UpdateLegApplicabilityRequest

router = APIRouter(tags=["leg-applicability"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)


def _to_out(leg: str, row: ThreeTwoOneOneZeroLegSetting | None, db: Session) -> LegApplicabilityOut:
    if row is None:
        return LegApplicabilityOut(leg=leg, applicable=True, updated_by_display_name=None, updated_at=None)
    user = db.get(User, row.updated_by_user_id) if row.updated_by_user_id else None
    return LegApplicabilityOut(
        leg=leg,
        applicable=row.applicable,
        updated_by_display_name=user.display_name if user else None,
        updated_at=row.updated_at,
    )


@router.get("/customers/{customer_id}/leg-applicability", response_model=list[LegApplicabilityOut])
def list_leg_applicability(
    customer_id: uuid.UUID, db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> list[LegApplicabilityOut]:
    """Which of the 5 3-2-1-1-0 legs are in scope for this customer's
    Recovery Evidence report -- always returns all 5, defaulting a leg
    with no stored row to `applicable=True` (the honest default; see
    `models/leg_applicability.py`).
    """
    get_customer_in_scope_or_404(db, customer_id, user)
    rows = {
        row.leg: row
        for row in db.query(ThreeTwoOneOneZeroLegSetting)
        .filter(ThreeTwoOneOneZeroLegSetting.customer_id == customer_id)
        .all()
    }
    return [_to_out(leg, rows.get(leg), db) for leg in ALL_LEGS]


@router.put("/customers/{customer_id}/leg-applicability/{leg}", response_model=LegApplicabilityOut)
def update_leg_applicability(
    customer_id: uuid.UUID,
    leg: str,
    body: UpdateLegApplicabilityRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(_require_platform_admin),
) -> LegApplicabilityOut:
    """`platform_admin` only -- deliberately not open to a customer's own
    `customer_admin`, unlike compliance attestations: this decides
    whether a leg counts against the customer's own report at all, a
    scoping decision the operator makes (e.g. per contract), not a fact
    the customer self-reports.
    """
    get_customer_in_scope_or_404(db, customer_id, user)
    if leg not in ALL_LEGS:
        raise ResurgamApiError(code="invalid_leg", message=f"Unknown leg {leg!r}.", status_code=400)

    row = (
        db.query(ThreeTwoOneOneZeroLegSetting)
        .filter(ThreeTwoOneOneZeroLegSetting.customer_id == customer_id, ThreeTwoOneOneZeroLegSetting.leg == leg)
        .one_or_none()
    )
    if row is None:
        row = ThreeTwoOneOneZeroLegSetting(
            customer_id=customer_id, leg=leg, applicable=body.applicable, updated_by_user_id=user.user_id
        )
        db.add(row)
    else:
        row.applicable = body.applicable
        row.updated_by_user_id = user.user_id

    db.add(
        AuditEvent(
            actor_user_id=user.user_id,
            action="leg_applicability_updated",
            target_type="three_two_one_one_zero_leg_setting",
            target_id=str(customer_id),
            correlation_id=getattr(request.state, "correlation_id", None),
            detail={"leg": leg, "applicable": body.applicable},
        )
    )
    db.commit()
    db.refresh(row)
    return _to_out(leg, row, db)
