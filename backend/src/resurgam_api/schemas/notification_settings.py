import re

from pydantic import BaseModel, Field, field_validator, model_validator

from resurgam_api.alert_mailer import parse_recipients, validate_recipients

# Same basic shape check as `schemas/mail_settings.py` -- deliberately
# not `pydantic[email]`'s `EmailStr`, avoids a new dependency for a
# check this simple.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email_format(value: str) -> str:
    if not _EMAIL_RE.match(value):
        raise ValueError(f"{value!r} does not look like a valid email address")
    return value


class UpdateNotificationSettingsRequest(BaseModel):
    report_notification_email: str | None = Field(
        default=None, description="Recipient for this customer's scheduled Recovery Evidence reports."
    )
    report_notifications_enabled: bool = Field(
        default=False,
        description="Whether report emails are actually sent. Requires report_notification_email to be set.",
    )
    alarm_notification_email: str | None = Field(
        default=None,
        description="Alarm recipients, comma-separated. Omit to leave unchanged. Prefer "
        "PUT /customers/{id}/alert-mailer, which also sets severity, reminders and resolved mails.",
    )
    alarm_notifications_enabled: bool = Field(
        default=False,
        description="Whether alarm emails are actually sent. Omit to leave unchanged. Requires "
        "alarm_notification_email to be set.",
    )
    billing_notification_email: str | None = Field(
        default=None, description="Recipient for this customer's billing notifications."
    )
    billing_notifications_enabled: bool = Field(
        default=False,
        description="Whether billing emails are actually sent. Requires billing_notification_email to be set.",
    )

    @field_validator("report_notification_email", "billing_notification_email")
    @classmethod
    def _email_format(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_email_format(value)

    @field_validator("alarm_notification_email")
    @classmethod
    def _alarm_recipients(cls, value: str | None) -> str | None:
        if value is None:
            return None
        recipients = validate_recipients(parse_recipients(value))
        return ", ".join(recipients) or None

    @model_validator(mode="after")
    def _enabled_requires_email(self) -> "UpdateNotificationSettingsRequest":
        for enabled, email, name in (
            (self.report_notifications_enabled, self.report_notification_email, "report_notification_email"),
            (self.alarm_notifications_enabled, self.alarm_notification_email, "alarm_notification_email"),
            (self.billing_notifications_enabled, self.billing_notification_email, "billing_notification_email"),
        ):
            if enabled and not email:
                raise ValueError(f"{name} must be set to enable this notification category")
        return self


class NotificationSettingsOut(BaseModel):
    report_notification_email: str | None = Field(
        description="Recipient for this customer's scheduled Recovery Evidence reports. Null if never configured."
    )
    report_notifications_enabled: bool = Field(description="Whether report emails are actually sent.")
    alarm_notification_email: str | None = Field(
        description="This customer's alarm recipients, comma-separated (see /customers/{id}/alert-mailer). "
        "Null if never configured."
    )
    alarm_notifications_enabled: bool = Field(description="Whether alarm emails are actually sent.")
    billing_notification_email: str | None = Field(
        description="Recipient for this customer's billing notifications. Null if never configured."
    )
    billing_notifications_enabled: bool = Field(description="Whether billing emails are actually sent.")
