import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from resurgam_api.alert_mailer import SEVERITY_RANK, parse_recipients, send_test_alert
from resurgam_api.api.v1.customers import get_customer_in_scope_or_404
from resurgam_api.auth.dependency import get_current_user
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.scope import require_customer_admin_access_for_customer
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.inventory.alarms import compute_alarms
from resurgam_api.mail import MailNotConfiguredError, MailSendError
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.customer_notification_settings import (
    CustomerAlarmNotificationState,
    CustomerNotificationSettings,
)
from resurgam_api.schemas.alert_mailer import (
    AlertMailerSettingsOut,
    SendTestAlertRequest,
    SendTestAlertResponse,
    UpdateAlertMailerSettingsRequest,
)

router = APIRouter(tags=["alert-mailer"])


def _to_out(db: Session, customer_id: uuid.UUID, row: CustomerNotificationSettings | None) -> AlertMailerSettingsOut:
    min_severity = row.alarm_min_severity if row is not None else "warning"
    min_rank = SEVERITY_RANK.get(min_severity, 1)
    open_keys = {
        (a.entity_type, a.entity_id)
        for a in compute_alarms(db, {customer_id})
        if a.customer_id == customer_id and SEVERITY_RANK.get(a.severity, 0) >= min_rank
    }
    notified = sum(
        1
        for s in db.query(CustomerAlarmNotificationState).filter_by(customer_id=customer_id).all()
        if (s.entity_type, s.entity_id) in open_keys
    )
    if row is None:
        return AlertMailerSettingsOut(
            enabled=False,
            recipients=[],
            min_severity="warning",
            notify_resolved=True,
            reminder_hours=None,
            open_alarm_count=len(open_keys),
            notified_alarm_count=0,
            last_attempt_at=None,
            last_sent_at=None,
            last_error=None,
        )
    return AlertMailerSettingsOut(
        enabled=row.alarm_notifications_enabled,
        recipients=parse_recipients(row.alarm_notification_email),
        min_severity=min_severity,
        notify_resolved=row.alarm_notify_resolved,
        reminder_hours=row.alarm_reminder_hours,
        open_alarm_count=len(open_keys),
        notified_alarm_count=notified,
        last_attempt_at=row.alarm_last_attempt_at,
        last_sent_at=row.alarm_last_sent_at,
        last_error=row.alarm_last_error,
    )


@router.get("/customers/{customer_id}/alert-mailer", response_model=AlertMailerSettingsOut)
def get_alert_mailer(
    customer_id: uuid.UUID, db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_current_user)
) -> AlertMailerSettingsOut:
    """A customer's alert mailer settings and delivery status. Visible to
    anyone who can see the customer; defaults (off, no recipients) when
    never configured."""
    get_customer_in_scope_or_404(db, customer_id, user)
    row = db.query(CustomerNotificationSettings).filter_by(customer_id=customer_id).one_or_none()
    return _to_out(db, customer_id, row)


@router.put("/customers/{customer_id}/alert-mailer", response_model=AlertMailerSettingsOut)
def update_alert_mailer(
    customer_id: uuid.UUID,
    body: UpdateAlertMailerSettingsRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> AlertMailerSettingsOut:
    """Set up who receives this customer's alarm mails and when.
    `platform_admin` or the customer's own `customer_admin`.

    The mailer checks for alarm changes every few minutes (after each
    collector pass) and sends one summary mail per customer listing new,
    escalated, still-open and resolved alarms. Each alarm is mailed once;
    again only when it escalates, when `reminder_hours` is due, or (with
    `notify_resolved`) when it clears. Changing the recipients resets the
    last error so delivery is retried right away."""
    customer = get_customer_in_scope_or_404(db, customer_id, user)
    require_customer_admin_access_for_customer(user, customer.id)

    row = db.query(CustomerNotificationSettings).filter_by(customer_id=customer_id).one_or_none()
    if row is None:
        row = CustomerNotificationSettings(customer_id=customer_id)
        db.add(row)

    new_recipients = ", ".join(body.recipients) or None
    if new_recipients != row.alarm_notification_email or body.enabled != row.alarm_notifications_enabled:
        row.alarm_last_error = None
    row.alarm_notification_email = new_recipients
    row.alarm_notifications_enabled = body.enabled
    row.alarm_min_severity = body.min_severity
    row.alarm_notify_resolved = body.notify_resolved
    row.alarm_reminder_hours = body.reminder_hours

    db.add(
        AuditEvent(
            actor_user_id=user.user_id,
            action="customer_alert_mailer_updated",
            target_type="customer_notification_settings",
            target_id=str(customer_id),
            correlation_id=getattr(request.state, "correlation_id", None),
            detail={
                "enabled": body.enabled,
                "recipient_count": len(body.recipients),
                "min_severity": body.min_severity,
                "notify_resolved": body.notify_resolved,
                "reminder_hours": body.reminder_hours,
            },
        )
    )
    db.commit()
    db.refresh(row)
    return _to_out(db, customer_id, row)


@router.post("/customers/{customer_id}/alert-mailer/test", response_model=SendTestAlertResponse)
def send_alert_mailer_test(
    customer_id: uuid.UUID,
    body: SendTestAlertRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: AuthenticatedUser = Depends(get_current_user),
) -> SendTestAlertResponse:
    """Send a real test mail, marked as a test, listing the customer's
    currently open alarms -- to the saved recipients, or to `recipients`
    if given. Does not mark anything as mailed. Same access as editing
    the mailer. Delivery problems come back as `sent: false` with the
    reason, not as an HTTP error."""
    customer = get_customer_in_scope_or_404(db, customer_id, user)
    require_customer_admin_access_for_customer(user, customer.id)

    recipients = body.recipients
    if recipients is None:
        row = db.query(CustomerNotificationSettings).filter_by(customer_id=customer_id).one_or_none()
        recipients = parse_recipients(row.alarm_notification_email if row else None)
    if not recipients:
        raise ResurgamApiError(
            code="alert_mailer_no_recipients",
            message="No recipients: save at least one recipient or pass `recipients`.",
            status_code=422,
        )

    error: str | None = None
    try:
        send_test_alert(db, settings, customer, recipients)
    except (MailNotConfiguredError, MailSendError) as exc:
        error = str(exc)

    db.add(
        AuditEvent(
            actor_user_id=user.user_id,
            action="customer_alert_mailer_test_sent",
            target_type="customer_notification_settings",
            target_id=str(customer_id),
            correlation_id=getattr(request.state, "correlation_id", None),
            detail={"recipient_count": len(recipients), "sent": error is None},
        )
    )
    db.commit()
    return SendTestAlertResponse(sent=error is None, recipients=recipients, error=error)
