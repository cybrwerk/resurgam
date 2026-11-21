import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from resurgam_api.api.v1.customers import get_customer_in_scope_or_404
from resurgam_api.auth.dependency import get_current_user
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.scope import require_customer_admin_access_for_customer
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.customer_notification_settings import CustomerNotificationSettings
from resurgam_api.schemas.notification_settings import NotificationSettingsOut, UpdateNotificationSettingsRequest

router = APIRouter(tags=["notification-settings"])


def _to_out(settings: CustomerNotificationSettings | None) -> NotificationSettingsOut:
    if settings is None:
        return NotificationSettingsOut(
            report_notification_email=None,
            report_notifications_enabled=False,
            alarm_notification_email=None,
            alarm_notifications_enabled=False,
            billing_notification_email=None,
            billing_notifications_enabled=False,
        )
    return NotificationSettingsOut(
        report_notification_email=settings.report_notification_email,
        report_notifications_enabled=settings.report_notifications_enabled,
        alarm_notification_email=settings.alarm_notification_email,
        alarm_notifications_enabled=settings.alarm_notifications_enabled,
        billing_notification_email=settings.billing_notification_email,
        billing_notifications_enabled=settings.billing_notifications_enabled,
    )


@router.get("/customers/{customer_id}/notification-settings", response_model=NotificationSettingsOut)
def get_customer_notification_settings(
    customer_id: uuid.UUID, db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> NotificationSettingsOut:
    """Any caller who can see this customer may view its notification
    settings -- same visibility rule as the customer record itself.
    Defaults (all null/disabled) are returned when nothing has ever been
    configured, rather than a 404.
    """
    get_customer_in_scope_or_404(db, customer_id, user)
    row = db.query(CustomerNotificationSettings).filter_by(customer_id=customer_id).one_or_none()
    return _to_out(row)


@router.put("/customers/{customer_id}/notification-settings", response_model=NotificationSettingsOut)
def update_customer_notification_settings(
    customer_id: uuid.UUID,
    body: UpdateNotificationSettingsRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> NotificationSettingsOut:
    """`platform_admin` OR that customer's own `customer_admin` -- same
    access rule as recording a compliance attestation for this customer
    (`api/v1/compliance_attestations.py`): the customer's own admin
    knows who should actually receive their notifications.
    """
    customer = get_customer_in_scope_or_404(db, customer_id, user)
    require_customer_admin_access_for_customer(user, customer.id)

    row = db.query(CustomerNotificationSettings).filter_by(customer_id=customer_id).one_or_none()
    if row is None:
        row = CustomerNotificationSettings(customer_id=customer_id)
        db.add(row)

    row.report_notification_email = body.report_notification_email
    row.report_notifications_enabled = body.report_notifications_enabled
    # The alarm recipient is owned by the Alert Mailer panel now
    # (`api/v1/alert_mailer.py`); a client that leaves the two alarm
    # fields out must not wipe what was set up there.
    if "alarm_notification_email" in body.model_fields_set:
        row.alarm_notification_email = body.alarm_notification_email
    if "alarm_notifications_enabled" in body.model_fields_set:
        row.alarm_notifications_enabled = body.alarm_notifications_enabled
    if row.alarm_notifications_enabled and not row.alarm_notification_email:
        raise ResurgamApiError(
            code="validation_error",
            message="alarm_notification_email must be set to enable this notification category",
            status_code=422,
        )
    row.billing_notification_email = body.billing_notification_email
    row.billing_notifications_enabled = body.billing_notifications_enabled

    db.add(
        AuditEvent(
            actor_user_id=user.user_id,
            action="customer_notification_settings_updated",
            target_type="customer_notification_settings",
            target_id=str(customer_id),
            correlation_id=getattr(request.state, "correlation_id", None),
            detail={
                "report_notifications_enabled": body.report_notifications_enabled,
                "alarm_notifications_enabled": row.alarm_notifications_enabled,
                "billing_notifications_enabled": body.billing_notifications_enabled,
            },
        )
    )
    db.commit()
    db.refresh(row)
    return _to_out(row)
