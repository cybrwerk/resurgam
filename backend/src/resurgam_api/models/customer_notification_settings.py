import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class CustomerNotificationSettings(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Per-customer notification recipients and on/off toggles, raised
    directly by the user: a Report and an Alarms receiver email per
    customer, plus Billing as a third independent category, each with
    its own enabled switch (not just "an address on file" -- a customer
    can have an address set up in advance without notifications for that
    category actually being live yet, same reasoning
    `ThreeTwoOneOneZeroLegSetting` already uses for its own on/off
    toggle).

    Singleton per customer: `customer_id` is a plain non-null UNIQUE
    column, not a partial-unique-index-enforced optional override --
    same shape as `CustomerFileIndexSettings`, but deliberately its own
    table rather than folded into that one (notifications have nothing
    to do with file indexing, and must be configurable for every
    customer regardless of whether they've opted into indexing at all --
    the same reasoning `Customer.report_retention_days` was moved off
    `CustomerFileIndexSettings` for earlier this session).

    Row absence means "nothing configured, everything off" -- mirrors
    `CustomerFileIndexSettings`'s "row absence = never opted in", handled
    by `api/v1/notification_settings.py` returning defaults rather than
    a DB constraint.
    """

    __tablename__ = "customer_notification_settings"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    report_notification_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    report_notifications_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Alert mailer (`notifications/alert_mailer.py`): one or more
    # comma-separated recipients, hence wider than the other two.
    alarm_notification_email: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    alarm_notifications_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # "warning" (every alarm) or "critical" (critical alarms only).
    alarm_min_severity: Mapped[str] = mapped_column(String(16), nullable=False, default="warning")
    # Also mail when a previously mailed alarm clears.
    alarm_notify_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Re-send still-open alarms every N hours; null = mail each alarm once.
    alarm_reminder_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Delivery status, shown in the Alert Mailer panel.
    alarm_last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    alarm_last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    alarm_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    billing_notification_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    billing_notifications_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class CustomerAlarmNotificationState(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One row per alarm a customer's alert mailer has already mailed
    about -- what turns the computed-on-read alarm list
    (`inventory/alarms.py`, never stored) into "new since last mail".
    Keyed by the alarmed entity, not the alarm type, so warning ->
    critical on the same backup group is an escalation of one alarm,
    not a second one. Deleted when the alarm clears (after the resolved
    mail, if that is on) or when the customer's mailer is switched off.
    """

    __tablename__ = "customer_alarm_notification_states"
    __table_args__ = (
        UniqueConstraint("customer_id", "entity_type", "entity_id", name="uq_customer_alarm_notification_state"),
    )

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    alarm_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    # Last mailed text -- what the "resolved" mail quotes once the alarm
    # itself no longer exists to describe its entity.
    message: Mapped[str] = mapped_column(Text, nullable=False)
    first_notified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_notified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
