from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from resurgam_api.alert_mailer import MAX_RECIPIENTS, validate_recipients


class AlertMailerSettingsOut(BaseModel):
    """A customer's alert mailer: who gets alarm mails and when."""

    enabled: bool = Field(
        description="Whether alarm mails are sent for this customer. Off by default. Needs at least one recipient "
        "and working mail delivery (System Settings -> Mail)."
    )
    recipients: list[str] = Field(
        description="Addresses that receive this customer's alarm mails (one mail, all recipients in To). "
        "Empty if never configured."
    )
    min_severity: Literal["warning", "critical"] = Field(
        description="'warning' mails every alarm; 'critical' only critical ones (overdue past the policy's critical "
        "threshold, datastore at 95% or more)."
    )
    notify_resolved: bool = Field(description="Also mail when a previously mailed alarm clears.")
    reminder_hours: int | None = Field(
        description="Re-send alarms that are still open every this many hours. Null: each alarm is mailed once "
        "(and again only if it escalates to critical)."
    )
    open_alarm_count: int = Field(
        description="This customer's alarms that are open right now and at or above min_severity -- what the next "
        "mail or a test mail lists."
    )
    notified_alarm_count: int = Field(
        description="Open alarms this customer was already mailed about (they are not mailed again unless they "
        "escalate or a reminder is due)."
    )
    last_attempt_at: datetime | None = Field(description="When the mailer last tried to send for this customer.")
    last_sent_at: datetime | None = Field(description="When the last alarm mail was delivered to the mail server.")
    last_error: str | None = Field(
        description="Why the last attempt failed (mail not configured, SMTP error), or null after a successful send. "
        "After a failure the mailer retries every few minutes."
    )


class UpdateAlertMailerSettingsRequest(BaseModel):
    enabled: bool = Field(
        default=False,
        description="Send alarm mails for this customer. Requires at least one recipient.",
    )
    recipients: list[str] = Field(
        default_factory=list,
        description=f"Recipient addresses, at most {MAX_RECIPIENTS}. Duplicates are removed.",
        examples=[["ops@customer.example", "backup-team@msp.example"]],
    )
    min_severity: Literal["warning", "critical"] = Field(
        default="warning", description="'warning' = every alarm, 'critical' = critical alarms only."
    )
    notify_resolved: bool = Field(default=True, description="Also mail when a mailed alarm clears.")
    reminder_hours: int | None = Field(
        default=None,
        ge=1,
        le=720,
        description="Re-send still-open alarms every N hours (1-720). Null or omitted: no reminders.",
    )

    @field_validator("recipients")
    @classmethod
    def _recipients(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for address in value:
            address = address.strip()
            if address and address.lower() not in {c.lower() for c in cleaned}:
                cleaned.append(address)
        return validate_recipients(cleaned)

    @model_validator(mode="after")
    def _enabled_requires_recipient(self) -> "UpdateAlertMailerSettingsRequest":
        if self.enabled and not self.recipients:
            raise ValueError("at least one recipient is required to enable the alert mailer")
        return self


class SendTestAlertRequest(BaseModel):
    recipients: list[str] | None = Field(
        default=None,
        description="Send the test to these addresses instead of the saved recipients (e.g. to try an address "
        "before saving it). Omitted or null: the saved recipients.",
    )

    @field_validator("recipients")
    @classmethod
    def _recipients(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return validate_recipients([a.strip() for a in value if a.strip()])


class SendTestAlertResponse(BaseModel):
    sent: bool = Field(description="True if the mail server accepted the test mail.")
    recipients: list[str] = Field(description="Where the test mail was sent (or was attempted).")
    error: str | None = Field(description="Why sending failed, or null on success.")
