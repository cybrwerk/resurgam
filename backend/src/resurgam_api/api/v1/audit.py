import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from resurgam_api.auth.dependency import require_role
from resurgam_api.db import get_db
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.identity import RoleKey
from resurgam_api.schemas.audit import AuditEventOut

router = APIRouter(prefix="/audit-events", tags=["audit"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)


@router.get("", response_model=list[AuditEventOut])
def list_audit_events(
    action: str | None = Query(default=None),
    target_type: str | None = Query(default=None),
    actor_user_id: str | None = Query(default=None),
    customer_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
    db: Session = Depends(get_db),
    _user=Depends(_require_platform_admin),
) -> list[AuditEventOut]:
    """Manifest §8: the append-only audit trail. `platform_admin`-only —
    most of `AuditEvent`'s targets still have no customer at all (a PVE
    cluster, a user, a PBS instance, ...), so this stays a platform-wide
    administrative view by default, same as every other cross-cutting
    admin surface in this codebase. `customer_id` (Recovery Evidence
    compliance reporting, ADR-pending) is a real, denormalized column now
    for the subset of actions that represent restore/access activity
    (file-restore/file-index jobs, restore plans) — NULL for every event
    recorded before that column existed or for actions with no customer
    to attribute, so filtering by it silently excludes both, it doesn't
    error on them.

    Filters apply *before* `limit`, same reasoning as the task-history
    endpoint (`GET /pbs-instances/{id}/tasks`) — this is an unbounded,
    ever-growing log (229 rows already in this dev environment after one
    milestone's worth of testing), so a bounded default with filters
    narrowing *before* truncation, not after, is the same real lesson
    learned there.
    """
    query = db.query(AuditEvent)
    if action:
        query = query.filter(AuditEvent.action == action)
    if target_type:
        query = query.filter(AuditEvent.target_type == target_type)
    if actor_user_id:
        query = query.filter(AuditEvent.actor_user_id == actor_user_id)
    if customer_id:
        query = query.filter(AuditEvent.customer_id == customer_id)
    events = query.order_by(AuditEvent.occurred_at.desc()).limit(limit).all()
    return [AuditEventOut.model_validate(e) for e in events]
