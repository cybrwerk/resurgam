from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from resurgam_api.auth.dependency import require_role
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_db
from resurgam_api.mail import MailNotConfiguredError, MailSendError, get_mail_settings, send_email
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.mail_settings import MailSettings
from resurgam_api.models.secrets import SecretPurpose, SecretReference
from resurgam_api.schemas.mail_settings import (
    MailSettingsOut,
    SendTestEmailRequest,
    SendTestEmailResponse,
    UpdateMailSettingsRequest,
)
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

router = APIRouter(prefix="/mail-settings", tags=["mail-settings"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)

# Singleton secret, same reasoning `integrations.py`'s own
# `_OIDC_SECRET_REFERENCE_KEY` uses -- this is the only place the
# well-known key is needed; `mail.py`'s own read path follows the FK to
# the `SecretReference` row instead of hardcoding it a second place.
_SMTP_PASSWORD_REFERENCE_KEY = "mail-smtp-password"


def _to_out(settings: MailSettings | None) -> MailSettingsOut:
    if settings is None:
        return MailSettingsOut(
            smtp_host="",
            smtp_port=587,
            smtp_username=None,
            smtp_password_configured=False,
            use_tls=True,
            from_address="",
            from_name=None,
            enabled=False,
        )
    return MailSettingsOut(
        smtp_host=settings.smtp_host,
        smtp_port=settings.smtp_port,
        smtp_username=settings.smtp_username,
        smtp_password_configured=settings.smtp_password_ref_id is not None,
        use_tls=settings.use_tls,
        from_address=settings.from_address,
        from_name=settings.from_name,
        enabled=settings.enabled,
    )


@router.get("", response_model=MailSettingsOut)
def get_mail_settings_endpoint(db: Session = Depends(get_db), user=Depends(_require_platform_admin)) -> MailSettingsOut:
    """`platform_admin` only -- unlike branding, mail settings have no
    reason to be visible pre-login, and the host/port/username are
    still operational detail worth keeping admin-only.
    """
    return _to_out(get_mail_settings(db))


@router.put("", response_model=MailSettingsOut)
def update_mail_settings(
    body: UpdateMailSettingsRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> MailSettingsOut:
    """Creates or updates the one `MailSettings` row. `smtp_password`
    follows the same three-way convention as `report_retention_days`
    elsewhere in this session: omitted from the request keeps the
    current password, an explicit `null` clears it, a real string
    rotates it -- checked via `model_fields_set`, not `is not None`,
    since `None` is itself a meaningful value here.
    """
    row = get_mail_settings(db)
    if row is None:
        row = MailSettings(
            smtp_host=body.smtp_host,
            smtp_port=body.smtp_port,
            smtp_username=body.smtp_username,
            use_tls=body.use_tls,
            from_address=body.from_address,
            from_name=body.from_name,
            enabled=body.enabled,
        )
        db.add(row)
    else:
        row.smtp_host = body.smtp_host
        row.smtp_port = body.smtp_port
        row.smtp_username = body.smtp_username
        row.use_tls = body.use_tls
        row.from_address = body.from_address
        row.from_name = body.from_name
        row.enabled = body.enabled

    if "smtp_password" in body.model_fields_set:
        provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
        # Any reference row with the well-known key, also one orphaned by an
        # older version that cleared the password without deleting it (a new
        # password then failed on the reference key's unique constraint).
        existing_ref = db.query(SecretReference).filter_by(reference_key=_SMTP_PASSWORD_REFERENCE_KEY).one_or_none()
        if body.smtp_password is None:
            if existing_ref is not None:
                provider.delete(_SMTP_PASSWORD_REFERENCE_KEY)
                row.smtp_password_ref_id = None
                db.flush()
                db.delete(existing_ref)
        else:
            if existing_ref is None:
                existing_ref = SecretReference(
                    reference_key=_SMTP_PASSWORD_REFERENCE_KEY,
                    description="SMTP password",
                    purpose=SecretPurpose.SMTP_PASSWORD,
                )
                db.add(existing_ref)
                db.flush()
            row.smtp_password_ref_id = existing_ref.id
            provider.store(_SMTP_PASSWORD_REFERENCE_KEY, body.smtp_password)

    db.add(
        AuditEvent(
            actor_user_id=user.user_id,
            action="mail_settings_updated",
            target_type="mail_settings",
            target_id=None,
            correlation_id=getattr(request.state, "correlation_id", None),
            detail={"enabled": body.enabled, "password_changed": "smtp_password" in body.model_fields_set},
        )
    )
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.post("/test", response_model=SendTestEmailResponse)
def send_test_email(
    body: SendTestEmailRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> SendTestEmailResponse:
    """Sends a real email using the *currently saved* settings -- a
    post-save connectivity test, mirroring PBS/PVE's own "test
    connection" pattern (rather than OIDC discovery's pre-save preview,
    since there is nothing meaningful to preview here without actually
    opening an SMTP connection).
    """
    try:
        send_email(
            db,
            settings,
            to=body.to,
            subject="RESURGAM test email",
            body_text="This is a test email from RESURGAM's mail adapter. If you received this, outbound mail is working.",
        )
    except (MailNotConfiguredError, MailSendError) as exc:
        return SendTestEmailResponse(sent=False, error=str(exc))
    return SendTestEmailResponse(sent=True, error=None)
