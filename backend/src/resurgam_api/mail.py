"""The SMTP adapter itself -- resolves `MailSettings`, decrypts the
stored password via the same `SecretsProvider` seam every other
credential in this codebase uses, and sends mail via Python's stdlib
`smtplib`/`email` (no new dependency: this app already avoids adding a
library where the standard library does the job, same reasoning
`reporting/timeline_svg.py` avoided a charting library).

Every future sender (the report/alarm/billing notification scheduler,
`POST /mail-settings/test`) goes through `send_email` here -- one path,
one place that knows how to actually talk SMTP.
"""

import smtplib
from email.message import EmailMessage

from sqlalchemy.orm import Session

from resurgam_api.config import Settings
from resurgam_api.models.mail_settings import MailSettings
from resurgam_api.models.secrets import SecretReference
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider
from resurgam_api.secrets.provider import SecretNotFoundError


class MailNotConfiguredError(Exception):
    """No `MailSettings` row, or `enabled=False` -- there is nothing to
    send with. Callers (the notification scheduler) should treat this as
    "silently skip", not an error worth alerting on -- an operator who
    hasn't set up mail yet hasn't opted into notifications at all.
    """


class MailSendError(Exception):
    """The SMTP conversation itself failed (connect, auth, send) --
    distinct from `MailNotConfiguredError` so callers can tell "nothing
    to send with" apart from "tried to send, and it broke".
    """


def get_mail_settings(db: Session) -> MailSettings | None:
    """The singleton mail-settings row, or `None` if never configured."""
    return db.query(MailSettings).first()


def _resolve_password(db: Session, settings: Settings, mail_settings: MailSettings) -> str | None:
    """Follows `smtp_password_ref_id` to the actual `SecretReference`
    row and retrieves by its own `reference_key` -- mirrors
    `resolve_effective_oidc_config`'s exact pattern (`auth/oidc.py`)
    rather than hardcoding the well-known key a second place.
    """
    if mail_settings.smtp_password_ref_id is None:
        return None
    secret_ref = db.get(SecretReference, mail_settings.smtp_password_ref_id)
    if secret_ref is None:
        return None
    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    try:
        return provider.retrieve(secret_ref.reference_key)
    except SecretNotFoundError:
        return None


def send_email(
    db: Session, settings: Settings, *, to: str, subject: str, body_text: str, body_html: str | None = None
) -> None:
    """Sends one email through the configured MTA. Raises
    `MailNotConfiguredError` if mail isn't set up/enabled, or
    `MailSendError` if the SMTP conversation itself fails -- never
    silently swallows a failure, matching this codebase's "never hide a
    real failure behind a no-op" discipline (e.g. `restore_test_plans.py`'s
    cleanup-failure fix earlier this session).
    """
    mail_settings = get_mail_settings(db)
    if mail_settings is None or not mail_settings.enabled:
        raise MailNotConfiguredError("Mail is not configured or is disabled.")

    message = EmailMessage()
    message["Subject"] = subject
    message["To"] = to
    message["From"] = (
        f"{mail_settings.from_name} <{mail_settings.from_address}>"
        if mail_settings.from_name
        else mail_settings.from_address
    )
    message.set_content(body_text)
    if body_html:
        message.add_alternative(body_html, subtype="html")

    password = _resolve_password(db, settings, mail_settings)
    try:
        with smtplib.SMTP(
            mail_settings.smtp_host, mail_settings.smtp_port, timeout=settings.connectivity_timeout_seconds
        ) as client:
            if mail_settings.use_tls:
                client.starttls()
            if mail_settings.smtp_username and password:
                client.login(mail_settings.smtp_username, password)
            client.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise MailSendError(str(exc)) from exc
