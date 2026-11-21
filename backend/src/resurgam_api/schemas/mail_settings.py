import re

from pydantic import BaseModel, Field, field_validator

# Deliberately not `pydantic[email]`'s `EmailStr` -- avoids a new
# dependency for a check this simple; a basic shape check catches typos
# without pretending to fully validate RFC 5322 addresses.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email_format(value: str) -> str:
    if not _EMAIL_RE.match(value):
        raise ValueError(f"{value!r} does not look like a valid email address")
    return value


class UpdateMailSettingsRequest(BaseModel):
    smtp_host: str = Field(
        min_length=1,
        max_length=255,
        description="Hostname or IP of the SMTP relay/MTA to send through, e.g. a local Postfix on the same host.",
    )
    smtp_port: int = Field(
        ge=1,
        le=65535,
        description="SMTP port. Common values: 587 (STARTTLS submission), 25 (plain relay, often for a local MTA), 465 (implicit TLS, not supported here -- use STARTTLS on 587 instead).",
    )
    smtp_username: str | None = Field(
        default=None,
        description="SMTP auth username. Omit/null if the relay needs no authentication (common for a local MTA).",
    )
    smtp_password: str | None = Field(
        default=None,
        description="SMTP auth password. Omit to keep the currently stored password unchanged; send null explicitly to clear it.",
    )
    use_tls: bool = Field(
        default=True,
        description="Whether to issue STARTTLS after connecting. Turn off only for a local, unencrypted relay you trust.",
    )
    from_address: str = Field(
        max_length=255, description="Envelope/header From address notification emails are sent as."
    )
    from_name: str | None = Field(
        default=None,
        max_length=255,
        description="Optional display name shown alongside from_address, e.g. 'RESURGAM Backup Center'.",
    )
    enabled: bool = Field(
        default=False,
        description="Whether outbound mail is actually active. False keeps the configuration stored but sends nothing.",
    )

    @field_validator("from_address")
    @classmethod
    def _from_address_format(cls, value: str) -> str:
        return _validate_email_format(value)


class MailSettingsOut(BaseModel):
    smtp_host: str = Field(description="Hostname or IP of the configured SMTP relay/MTA.")
    smtp_port: int = Field(description="Configured SMTP port.")
    smtp_username: str | None = Field(
        description="Configured SMTP auth username. Null if the relay needs no authentication."
    )
    smtp_password_configured: bool = Field(
        description="Whether a password is currently stored. The password itself is never returned."
    )
    use_tls: bool = Field(description="Whether STARTTLS is issued after connecting.")
    from_address: str = Field(description="Configured From address for notification emails.")
    from_name: str | None = Field(description="Configured display name shown alongside from_address, if any.")
    enabled: bool = Field(description="Whether outbound mail is actually active.")


class SendTestEmailRequest(BaseModel):
    to: str = Field(description="Address to send a real test email to, using the currently saved settings.")

    @field_validator("to")
    @classmethod
    def _to_format(cls, value: str) -> str:
        return _validate_email_format(value)


class SendTestEmailResponse(BaseModel):
    sent: bool = Field(description="Whether the test email was accepted by the SMTP server.")
    error: str | None = Field(description="Error message if sending failed. Null on success.")
