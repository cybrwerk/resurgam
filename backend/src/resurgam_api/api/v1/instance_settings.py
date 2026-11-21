from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from resurgam_api.auth.dependency import get_current_user, require_role
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.db import get_db
from resurgam_api.instance_settings import (
    effective_collector_poll_interval_minutes,
    effective_min_report_retention_days,
    get_instance_settings,
)
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.instance_settings import InstanceSettings
from resurgam_api.schemas.instance_settings import InstanceSettingsOut, UpdateInstanceSettingsRequest

router = APIRouter(prefix="/instance-settings", tags=["instance-settings"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)


@router.get("", response_model=InstanceSettingsOut)
def get_instance_settings_endpoint(
    db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> InstanceSettingsOut:
    """Any authenticated caller may read the platform-wide report-
    retention floor -- it's referenced when setting a customer's own
    retention override, not a secret.
    """
    return InstanceSettingsOut(
        min_report_retention_days=effective_min_report_retention_days(db),
        collector_poll_interval_minutes=effective_collector_poll_interval_minutes(db),
    )


@router.put("", response_model=InstanceSettingsOut)
def update_instance_settings(
    body: UpdateInstanceSettingsRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(_require_platform_admin),
) -> InstanceSettingsOut:
    """`platform_admin` only. Raising the floor does not retroactively
    shrink any customer's already-stored `report_retention_days` below
    it -- that's only enforced going forward, the next time a customer's
    own setting is written (`PATCH /customers/{id}`).
    """
    settings = get_instance_settings(db)
    if settings is None:
        settings = InstanceSettings(
            min_report_retention_days=body.min_report_retention_days,
            collector_poll_interval_minutes=body.collector_poll_interval_minutes,
        )
        db.add(settings)
    else:
        settings.min_report_retention_days = body.min_report_retention_days
        settings.collector_poll_interval_minutes = body.collector_poll_interval_minutes

    db.add(
        AuditEvent(
            actor_user_id=user.user_id,
            action="instance_settings_updated",
            target_type="instance_settings",
            target_id=None,
            correlation_id=getattr(request.state, "correlation_id", None),
            detail={
                "min_report_retention_days": body.min_report_retention_days,
                "collector_poll_interval_minutes": body.collector_poll_interval_minutes,
            },
        )
    )
    db.commit()
    db.refresh(settings)
    return InstanceSettingsOut(
        min_report_retention_days=settings.min_report_retention_days,
        collector_poll_interval_minutes=settings.collector_poll_interval_minutes,
    )
