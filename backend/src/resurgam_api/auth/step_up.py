"""Fresh confirmation before the most sensitive actions (ADR 0031: downloading
or exporting encryption keys).

The caller proves, right at the action, that they are the person behind the
session:

- a local account: its password;
- an account with a confirmed authenticator app: a current TOTP code instead;
- the break-glass account: the break-glass password;
- an SSO-only account (no local password, no authenticator): a sign-in no
  older than `RECENT_LOGIN_SECONDS` -- the identity provider did the proof.

Never satisfiable with an API token: `get_session_user` refuses those first.
"""

from __future__ import annotations

import time
import uuid

import jwt
from fastapi import Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from resurgam_api.auth.breakglass import verify_credentials
from resurgam_api.auth.local import verify_password
from resurgam_api.auth.mfa import verify_totp_code
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.config import Settings
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.identity import User
from resurgam_api.models.mfa import TotpCredential
from resurgam_api.models.secrets import SecretReference
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

RECENT_LOGIN_SECONDS = 10 * 60


class StepUpConfirmation(BaseModel):
    password: str | None = Field(
        default=None,
        max_length=1024,
        description="Your account password (local and break-glass accounts).",
    )
    totp_code: str | None = Field(
        default=None,
        max_length=16,
        description="A current code from your authenticator app, instead of the password.",
    )


def _session_issued_at(request: Request, settings: Settings) -> int | None:
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else ""
    if not token or not settings.session_secret:
        return None
    try:
        payload = jwt.decode(token, settings.session_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    iat = payload.get("iat")
    return int(iat) if isinstance(iat, int | float) else None


def _confirmed_totp_secret(db: Session, settings: Settings, user_id: uuid.UUID) -> str | None:
    totp = (
        db.query(TotpCredential)
        .filter(TotpCredential.user_id == user_id, TotpCredential.confirmed_at.isnot(None))
        .first()
    )
    if totp is None:
        return None
    secret_ref = db.get(SecretReference, totp.secret_ref_id)
    return LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key).retrieve(secret_ref.reference_key)


def require_step_up(
    db: Session,
    settings: Settings,
    request: Request,
    user: AuthenticatedUser,
    confirmation: StepUpConfirmation | None,
) -> str:
    """Raises 403 `confirmation_required` / `confirmation_failed` unless the
    caller just proved who they are. Returns how ("password", "totp",
    "recent_login") for the audit log."""
    confirmation = confirmation or StepUpConfirmation()
    record = db.get(User, user.user_id)
    totp_secret = _confirmed_totp_secret(db, settings, user.user_id)

    if user.is_breakglass:
        if confirmation.password and verify_credentials(settings, settings.breakglass_username, confirmation.password):
            return "password"
    else:
        if (
            confirmation.password
            and record is not None
            and record.password_hash
            and verify_password(confirmation.password, record.password_hash)
        ):
            return "password"
        if confirmation.totp_code and totp_secret and verify_totp_code(totp_secret, confirmation.totp_code.strip()):
            return "totp"

    sso_only = not user.is_breakglass and record is not None and not record.password_hash and totp_secret is None
    if sso_only:
        issued = _session_issued_at(request, settings)
        if issued is not None and time.time() - issued <= RECENT_LOGIN_SECONDS:
            return "recent_login"
        raise ResurgamApiError(
            code="confirmation_required",
            message="Sign in again (at most 10 minutes ago) to confirm this action.",
            status_code=403,
            details={"method": "recent_login"},
        )

    if not confirmation.password and not confirmation.totp_code:
        raise ResurgamApiError(
            code="confirmation_required",
            message="Confirm this action with your password or an authenticator code.",
            status_code=403,
            details={"method": "password_or_totp" if totp_secret else "password"},
        )
    raise ResurgamApiError(
        code="confirmation_failed",
        message="That password or code is not correct.",
        status_code=403,
    )
