from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from resurgam_api.auth.dependency import get_current_user
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.scope import visible_customer_ids
from resurgam_api.db import get_db
from resurgam_api.inventory.alarms import compute_alarms
from resurgam_api.schemas.alarm import AlarmOut

router = APIRouter(prefix="/alarms", tags=["alarms"])


@router.get("", response_model=list[AlarmOut])
def list_alarms(db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)) -> list[AlarmOut]:
    """Manifest §9.2's Dashboard alarm states, scope-filtered the same way
    as every other customer-scoped endpoint — a platform_admin sees every
    alarm, anyone else only ones whose effective customer they hold a
    role at.
    """
    return compute_alarms(db, visible_customer_ids(user))
