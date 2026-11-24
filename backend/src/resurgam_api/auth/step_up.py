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

Wrong passwords and codes are audited (`step_up_confirmation_failed`) and
limited by `attempt_limit` (429 `confirmation_locked`), together with wrong
current passwords on a password change. Without it, a stolen session could
guess the password here as often as it liked.
"""

from __future__ import annotations

import time
import uuid

import jwt
from fastapi import Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from resurgam_api.auth import attempt_limit
from resurgam_api.auth.audit import record_login_audit_event
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
FAILED_ACTION = "step_up_confirmation_failed"


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
    caller just proved who they are, or 429 `confirmation_locked` after too
    many wrong attempts (see the module docstring). Returns how ("password",
    "totp", "recent_login") for the audit log. A wrong password or code is
    committed to the audit log before the error is raised."""
    confirmation = confirmation or StepUpConfirmation()
    record = db.get(User, user.user_id)
    totp_secret = _confirmed_totp_secret(db, settings, user.user_id)

    if confirmation.password or confirmation.totp_code:
        attempt_limit.refuse_if_locked(
            db, attempt_limit.SESSION_PASSWORD, code="confirmation_locked", actor_user_id=user.user_id
        )

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
    record_login_audit_event(
        db,
        action=FAILED_ACTION,
        actor_user_id=user.user_id,
        correlation_id=getattr(request.state, "correlation_id", None),
        detail={"method": "password" if confirmation.password else "totp", "path": request.url.path},
    )
    db.commit()
    left = attempt_limit.attempts_left(db, attempt_limit.SESSION_PASSWORD, actor_user_id=user.user_id)
    if left == 0:
        _count, oldest = attempt_limit.recent_failures(db, attempt_limit.SESSION_PASSWORD, actor_user_id=user.user_id)
        raise attempt_limit.locked_error("confirmation_locked", oldest)
    raise ResurgamApiError(
        code="confirmation_failed",
        message=f"That password or code is not correct. {left} attempt{'s' if left != 1 else ''} left "
        f"before confirmations are blocked for {attempt_limit.WINDOW_SECONDS // 60} minutes.",
        status_code=403,
        details={"attempts_left": left},
    )
