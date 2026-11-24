import json
from datetime import UTC, datetime
from urllib.parse import urlencode

import webauthn
from authlib.integrations.starlette_client import OAuthError
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from webauthn.helpers.exceptions import InvalidAuthenticationResponse
from webauthn.helpers.structs import PublicKeyCredentialDescriptor

from resurgam_api.auth import attempt_limit
from resurgam_api.auth.audit import record_login_audit_event
from resurgam_api.auth.breakglass import get_or_create_breakglass_user, verify_credentials
from resurgam_api.auth.dependency import get_current_user, get_session_user
from resurgam_api.auth.local import hash_password, verify_password
from resurgam_api.auth.mfa import verify_recovery_code, verify_totp_code
from resurgam_api.auth.mfa_enrollment import confirmed_factor_ids, enrollment_token_usable
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.oidc import (
    OidcProvisioningError,
    get_oauth_client,
    provision_user_from_claims,
    resolve_effective_oidc_config,
)
from resurgam_api.auth.sessions import (
    InvalidSessionTokenError,
    create_mfa_challenge_token,
    create_mfa_enrollment_token,
    create_session_token,
    create_webauthn_ceremony_token,
    verify_mfa_challenge_token,
    verify_mfa_enrollment_token,
    verify_webauthn_ceremony_token,
)
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.instance_settings import mfa_required_for
from resurgam_api.models.identity import User
from resurgam_api.models.mfa import RecoveryCode, TotpCredential, WebauthnCredential
from resurgam_api.models.secrets import SecretReference
from resurgam_api.schemas.auth import (
    BreakglassLoginRequest,
    BreakglassLoginResponse,
    ChangePasswordRequest,
    LocalLoginRequest,
    LocalLoginResponse,
    MfaEnrollmentCompleteRequest,
    RoleAssignmentOut,
    VerifyRecoveryCodeRequest,
    VerifyTotpRequest,
    WhoAmIResponse,
)
from resurgam_api.schemas.mfa import (
    WebauthnAuthenticateOptionsRequest,
    WebauthnAuthenticateRequest,
    WebauthnCeremonyOptionsOut,
)
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

router = APIRouter(prefix="/auth", tags=["auth"])


def _login_failed(
    db: Session,
    message: str,
    code: str,
    limit: attempt_limit.Limit,
    *,
    locked_code: str = "login_locked",
    **key,
) -> ResurgamApiError:
    """The error for a wrong password or code that was just audited: 401
    with `attempts_left`, or 429 `locked_code` when this failure used up the
    last attempt (see `auth.attempt_limit`)."""
    left = attempt_limit.attempts_left(db, limit, **key)
    if left == 0:
        _count, oldest = attempt_limit.recent_failures(db, limit, **key)
        return attempt_limit.locked_error(locked_code, oldest)
    return ResurgamApiError(code=code, message=message, status_code=401, details={"attempts_left": left})


def _confirmed_totp(db: Session, user_id) -> TotpCredential | None:
    return (
        db.query(TotpCredential)
        .filter(TotpCredential.user_id == user_id, TotpCredential.confirmed_at.isnot(None))
        .one_or_none()
    )


@router.post(
    "/breakglass-login",
    response_model=BreakglassLoginResponse,
    responses={
        401: {"description": "`invalid_credentials`; `details.attempts_left` says how many tries remain."},
        429: {
            "description": "`login_locked`: 5 wrong passwords within 15 minutes -- refused, even with the right password, "
            "until `details.retry_after_seconds` have passed."
        },
    },
)
def breakglass_login(
    body: BreakglassLoginRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> BreakglassLoginResponse:
    """Signs in with the single break-glass account from the appliance's
    `.env` (403 `breakglass_disabled` unless enabled there). Wrong passwords
    are audited and limited like every login (`auth.attempt_limit`)."""
    correlation_id = getattr(request.state, "correlation_id", None)

    if not settings.breakglass_enabled:
        raise ResurgamApiError(code="breakglass_disabled", message="Break-glass login is disabled.", status_code=403)

    attempt_limit.refuse_if_locked(db, attempt_limit.BREAKGLASS_LOGIN, code="login_locked")
    if not verify_credentials(settings, body.username, body.password):
        record_login_audit_event(
            db,
            action="breakglass_login_failed",
            correlation_id=correlation_id,
            detail={"username": body.username},
        )
        db.commit()
        raise _login_failed(
            db, "Invalid break-glass credentials.", "invalid_credentials", attempt_limit.BREAKGLASS_LOGIN
        )

    user = get_or_create_breakglass_user(db, settings)
    token, expires_in = create_session_token(user.id, settings)
    record_login_audit_event(
        db,
        action="breakglass_login",
        actor_user_id=user.id,
        correlation_id=correlation_id,
        detail={"username": body.username},
    )
    db.commit()

    return BreakglassLoginResponse(access_token=token, expires_in=expires_in)


@router.post(
    "/local-login",
    response_model=LocalLoginResponse,
    responses={
        401: {
            "description": "`invalid_credentials` (unknown address or wrong password); `details.attempts_left` says how many tries remain."
        },
        429: {
            "description": "`login_locked`: 5 wrong passwords for this address within 15 minutes -- refused, even with the right password, "
            "until `details.retry_after_seconds` have passed."
        },
    },
)
def local_login(
    body: LocalLoginRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> LocalLoginResponse:
    """Not from the manifest — real local-user accounts (`POST /users`),
    distinct from the single break-glass credential above. Same error
    message for "no such email" and "wrong password" (`invalid_credentials`),
    same as break-glass — nothing here should let a caller distinguish
    the two and enumerate valid emails. Wrong passwords are limited per
    address (`auth.attempt_limit`); an unknown address is limited the same
    way, so the limit does not reveal accounts either.
    """
    correlation_id = getattr(request.state, "correlation_id", None)

    attempt_limit.refuse_if_locked(db, attempt_limit.LOCAL_LOGIN, code="login_locked", email=body.email)
    user = db.query(User).filter_by(email=body.email).one_or_none()
    invalid = user is None or user.disabled or user.password_hash is None
    if not invalid and not verify_password(body.password, user.password_hash):
        invalid = True

    if invalid:
        record_login_audit_event(
            db, action="local_login_failed", correlation_id=correlation_id, detail={"email": body.email}
        )
        db.commit()
        raise _login_failed(
            db, "Invalid email or password.", "invalid_credentials", attempt_limit.LOCAL_LOGIN, email=body.email
        )

    totp = _confirmed_totp(db, user.id)
    webauthn_count = db.query(WebauthnCredential).filter_by(user_id=user.id).count()

    available_methods: list[str] = []
    if totp is not None:
        available_methods.append("totp")
    if webauthn_count > 0:
        available_methods.append("webauthn")
    if available_methods and db.query(RecoveryCode).filter_by(user_id=user.id, used_at=None).count() > 0:
        available_methods.append("recovery_code")

    if not available_methods and mfa_required_for(db, user):
        # An admin made MFA mandatory and there is no second factor yet:
        # no session, only a token for setting one up.
        enrollment_token, expires_in = create_mfa_enrollment_token(user.id, settings)
        record_login_audit_event(
            db,
            action="local_login_mfa_enrollment_required",
            actor_user_id=user.id,
            correlation_id=correlation_id,
            detail={"email": body.email},
        )
        db.commit()
        return LocalLoginResponse(
            mfa_required=False,
            mfa_enrollment_required=True,
            enrollment_token=enrollment_token,
            expires_in=expires_in,
        )

    if not available_methods:
        token, expires_in = create_session_token(user.id, settings)
        record_login_audit_event(
            db,
            action="local_login",
            actor_user_id=user.id,
            correlation_id=correlation_id,
            detail={"email": body.email},
        )
        db.commit()
        return LocalLoginResponse(mfa_required=False, access_token=token, expires_in=expires_in)

    challenge_token = create_mfa_challenge_token(user.id, settings)
    record_login_audit_event(
        db,
        action="local_login_password_ok",
        actor_user_id=user.id,
        correlation_id=correlation_id,
        detail={"email": body.email},
    )
    db.commit()
    return LocalLoginResponse(mfa_required=True, challenge_token=challenge_token, available_methods=available_methods)


@router.post("/mfa/enrollment/complete", response_model=BreakglassLoginResponse)
def complete_mfa_enrollment(
    body: MfaEnrollmentCompleteRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> BreakglassLoginResponse:
    """Forced MFA enrollment, last step: once the account behind
    `enrollment_token` has a confirmed authenticator app or a registered
    security key (each proven when it was added), the enrollment token is
    exchanged for a normal session. 400 `mfa_not_enrolled` before that.

    Single use: the exchange is recorded (audit `local_login` carrying the
    token's `jti`), and a second call with the same token gets 401. A token
    is also refused once the account has a factor that was not added with
    this very token -- e.g. a stale token from before someone else finished
    enrollment (ADR 0028)."""
    invalid = ResurgamApiError(
        code="invalid_enrollment_token",
        message="The MFA setup session is invalid or expired. Sign in again.",
        status_code=401,
    )
    try:
        claims = verify_mfa_enrollment_token(body.enrollment_token, settings)
    except InvalidSessionTokenError as exc:
        raise invalid from exc
    # Row lock: two concurrent exchanges of the same token serialise here,
    # and the second sees the first's audit record.
    user = db.query(User).filter(User.id == claims.user_id).with_for_update().one_or_none()
    if user is None or user.disabled:
        raise invalid
    if not enrollment_token_usable(db, user.id, claims.jti):
        raise invalid
    if not confirmed_factor_ids(db, user.id):
        raise ResurgamApiError(
            code="mfa_not_enrolled",
            message="Set up an authenticator app or a security key first.",
            status_code=400,
        )
    token, expires_in = create_session_token(user.id, settings)
    record_login_audit_event(
        db,
        action="local_login",
        actor_user_id=user.id,
        correlation_id=getattr(request.state, "correlation_id", None),
        detail={"email": user.email, "mfa": "enrolled_at_login", "enrollment_jti": claims.jti},
    )
    db.commit()
    return BreakglassLoginResponse(access_token=token, expires_in=expires_in)


def _resolve_mfa_challenge(token: str, settings: Settings, db: Session) -> User:
    try:
        user_id = verify_mfa_challenge_token(token, settings)
    except InvalidSessionTokenError as exc:
        raise ResurgamApiError(
            code="invalid_challenge", message="MFA challenge is invalid or expired.", status_code=401
        ) from exc
    user = db.get(User, user_id)
    if user is None or user.disabled:
        raise ResurgamApiError(
            code="invalid_challenge", message="MFA challenge is invalid or expired.", status_code=401
        )
    return user


@router.post(
    "/mfa/verify-totp",
    response_model=BreakglassLoginResponse,
    responses={
        401: {"description": "`invalid_code`; `details.attempts_left` says how many tries remain."},
        429: {
            "description": "`login_locked`: 5 wrong authenticator or recovery codes for this account within 15 minutes -- refused, even with the right code, "
            "until `details.retry_after_seconds` have passed."
        },
    },
)
def verify_totp(
    body: VerifyTotpRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> BreakglassLoginResponse:
    """Second login step: exchanges the `challenge_token` from
    `/auth/local-login` and a current authenticator code for a session. Wrong
    codes are audited and limited per account, together with recovery codes
    (`auth.attempt_limit`)."""
    correlation_id = getattr(request.state, "correlation_id", None)
    user = _resolve_mfa_challenge(body.challenge_token, settings, db)
    attempt_limit.refuse_if_locked(db, attempt_limit.SECOND_FACTOR, code="login_locked", actor_user_id=user.id)
    totp = _confirmed_totp(db, user.id)
    if totp is None:
        raise ResurgamApiError(
            code="totp_not_enabled", message="TOTP is not enabled for this account.", status_code=400
        )

    secret_ref = db.get(SecretReference, totp.secret_ref_id)
    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    secret = provider.retrieve(secret_ref.reference_key)

    if not verify_totp_code(secret, body.code):
        record_login_audit_event(
            db, action="mfa_totp_failed", actor_user_id=user.id, correlation_id=correlation_id, detail={}
        )
        db.commit()
        raise _login_failed(
            db, "Invalid TOTP code.", "invalid_code", attempt_limit.SECOND_FACTOR, actor_user_id=user.id
        )

    token, expires_in = create_session_token(user.id, settings)
    record_login_audit_event(db, action="mfa_totp_ok", actor_user_id=user.id, correlation_id=correlation_id, detail={})
    db.commit()
    return BreakglassLoginResponse(access_token=token, expires_in=expires_in)


@router.post(
    "/mfa/verify-recovery-code",
    response_model=BreakglassLoginResponse,
    responses={
        401: {"description": "`invalid_code`; `details.attempts_left` says how many tries remain."},
        429: {
            "description": "`login_locked`: 5 wrong authenticator or recovery codes for this account within 15 minutes -- refused, even with the right code, "
            "until `details.retry_after_seconds` have passed."
        },
    },
)
def verify_recovery_code_endpoint(
    body: VerifyRecoveryCodeRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> BreakglassLoginResponse:
    """Second login step with a one-time recovery code instead of the
    authenticator; the code is used up. Wrong codes count towards the same
    per-account limit as authenticator codes (`auth.attempt_limit`)."""
    correlation_id = getattr(request.state, "correlation_id", None)
    user = _resolve_mfa_challenge(body.challenge_token, settings, db)
    attempt_limit.refuse_if_locked(db, attempt_limit.SECOND_FACTOR, code="login_locked", actor_user_id=user.id)

    candidates = db.query(RecoveryCode).filter_by(user_id=user.id, used_at=None).all()
    match = next((c for c in candidates if verify_recovery_code(body.code, c.code_hash)), None)
    if match is None:
        record_login_audit_event(
            db, action="mfa_recovery_code_failed", actor_user_id=user.id, correlation_id=correlation_id, detail={}
        )
        db.commit()
        raise _login_failed(
            db,
            "Invalid or already-used recovery code.",
            "invalid_code",
            attempt_limit.SECOND_FACTOR,
            actor_user_id=user.id,
        )

    match.used_at = datetime.now(UTC)
    token, expires_in = create_session_token(user.id, settings)
    record_login_audit_event(
        db, action="mfa_recovery_code_used", actor_user_id=user.id, correlation_id=correlation_id, detail={}
    )
    db.commit()
    return BreakglassLoginResponse(access_token=token, expires_in=expires_in)


@router.post("/mfa/webauthn/authenticate-options", response_model=WebauthnCeremonyOptionsOut)
def webauthn_authenticate_options(
    body: WebauthnAuthenticateOptionsRequest,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> WebauthnCeremonyOptionsOut:
    user = _resolve_mfa_challenge(body.challenge_token, settings, db)
    credentials = db.query(WebauthnCredential).filter_by(user_id=user.id).all()
    if not credentials:
        raise ResurgamApiError(
            code="webauthn_not_enabled", message="No security key is registered for this account.", status_code=400
        )

    options = webauthn.generate_authentication_options(
        rp_id=settings.webauthn_rp_id,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=webauthn.base64url_to_bytes(c.credential_id)) for c in credentials
        ],
    )
    ceremony_token = create_webauthn_ceremony_token(
        user_id=user.id, challenge=options.challenge, action="authenticate", settings=settings
    )
    return WebauthnCeremonyOptionsOut(
        options=json.loads(webauthn.options_to_json(options)), ceremony_token=ceremony_token
    )


@router.post("/mfa/webauthn/authenticate", response_model=BreakglassLoginResponse)
def webauthn_authenticate(
    body: WebauthnAuthenticateRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> BreakglassLoginResponse:
    correlation_id = getattr(request.state, "correlation_id", None)
    try:
        user_id, challenge, _nickname = verify_webauthn_ceremony_token(
            body.ceremony_token, action="authenticate", settings=settings
        )
    except InvalidSessionTokenError as exc:
        raise ResurgamApiError(
            code="invalid_challenge", message="WebAuthn ceremony is invalid or expired.", status_code=401
        ) from exc

    user = db.get(User, user_id)
    if user is None or user.disabled:
        raise ResurgamApiError(
            code="invalid_challenge", message="WebAuthn ceremony is invalid or expired.", status_code=401
        )

    credential_id = body.credential.get("id", "")
    stored = db.query(WebauthnCredential).filter_by(user_id=user.id, credential_id=credential_id).one_or_none()
    if stored is None:
        raise ResurgamApiError(code="invalid_credential", message="Unknown security key.", status_code=401)

    try:
        result = webauthn.verify_authentication_response(
            credential=body.credential,
            expected_challenge=challenge,
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origin,
            credential_public_key=webauthn.base64url_to_bytes(stored.public_key),
            credential_current_sign_count=stored.sign_count,
        )
    except InvalidAuthenticationResponse as exc:
        record_login_audit_event(
            db,
            action="mfa_webauthn_failed",
            actor_user_id=user.id,
            correlation_id=correlation_id,
            detail={"error": str(exc)},
        )
        db.commit()
        raise ResurgamApiError(
            code="invalid_credential", message="Security key verification failed.", status_code=401
        ) from exc

    stored.sign_count = result.new_sign_count
    token, expires_in = create_session_token(user.id, settings)
    record_login_audit_event(
        db, action="mfa_webauthn_ok", actor_user_id=user.id, correlation_id=correlation_id, detail={}
    )
    db.commit()
    return BreakglassLoginResponse(access_token=token, expires_in=expires_in)


@router.post(
    "/change-password",
    status_code=204,
    responses={
        401: {
            "description": "`invalid_credentials` (current password wrong); `details.attempts_left` says how many tries remain."
        },
        429: {
            "description": "`password_change_locked`: 5 wrong current passwords (or key-download confirmations) for this account within 15 minutes -- refused, even with the right password, "
            "until `details.retry_after_seconds` have passed."
        },
    },
)
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    auth_user: AuthenticatedUser = Depends(get_session_user),
) -> None:
    """Changes the signed-in account's local password (session only, not
    with API tokens). The current password is checked first; wrong ones are
    audited (`password_change_failed`) and limited together with key-download
    confirmations (`auth.attempt_limit`)."""
    user = db.get(User, auth_user.user_id)
    if user is None or user.password_hash is None:
        raise ResurgamApiError(
            code="no_local_password",
            message="This account has no local password to change (break-glass and SSO accounts aren't affected).",
            status_code=400,
        )
    attempt_limit.refuse_if_locked(
        db, attempt_limit.SESSION_PASSWORD, code="password_change_locked", actor_user_id=user.id
    )
    if not verify_password(body.current_password, user.password_hash):
        record_login_audit_event(
            db,
            action="password_change_failed",
            actor_user_id=user.id,
            correlation_id=getattr(request.state, "correlation_id", None),
            detail={},
        )
        db.commit()
        raise _login_failed(
            db,
            "Current password is incorrect.",
            "invalid_credentials",
            attempt_limit.SESSION_PASSWORD,
            locked_code="password_change_locked",
            actor_user_id=user.id,
        )

    user.password_hash = hash_password(body.new_password)
    db.commit()


@router.get("/oidc/login")
async def oidc_login(request: Request, settings: Settings = Depends(get_settings), db: Session = Depends(get_db)):
    """Redirects the browser to authentik. PKCE verifier and nonce are
    stored by Authlib in the signed session cookie (SessionMiddleware,
    main.py) and read back in the callback below.
    """
    config = resolve_effective_oidc_config(db, settings)
    oauth_client = get_oauth_client(config).authentik
    return await oauth_client.authorize_redirect(request, config.redirect_uri)


def _login_error_redirect(settings: Settings, code: str) -> RedirectResponse:
    """A failed OIDC login is a full-page browser navigation (the browser
    was sent here by authentik, not by frontend JS), so an error can't be
    returned as a JSON body the way other endpoints do — the user would see
    raw JSON in their browser tab. Redirect back to the frontend's login
    page with an error code instead.
    """
    query = urlencode({"oidc_error": code})
    return RedirectResponse(f"{settings.frontend_origin}/login?{query}", status_code=302)


@router.get("/oidc/callback")
async def oidc_callback(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    correlation_id = getattr(request.state, "correlation_id", None)
    config = resolve_effective_oidc_config(db, settings)
    oauth_client = get_oauth_client(config).authentik

    try:
        token = await oauth_client.authorize_access_token(request)
    except OAuthError as exc:
        record_login_audit_event(
            db, action="oidc_login_failed", correlation_id=correlation_id, detail={"error": str(exc)}
        )
        db.commit()
        return _login_error_redirect(settings, "oidc_login_failed")

    claims = dict(token.get("userinfo") or {})
    try:
        userinfo = await oauth_client.userinfo(token=token)
        claims.update(userinfo)
    except OAuthError:
        pass  # fall back to whatever claims the ID token itself carried

    try:
        user = provision_user_from_claims(db, claims)
    except OidcProvisioningError as exc:
        record_login_audit_event(
            db,
            action="oidc_login_failed",
            correlation_id=correlation_id,
            detail={"error": str(exc), "sub": claims.get("sub"), "email": claims.get("email")},
        )
        db.commit()
        return _login_error_redirect(settings, "oidc_provisioning_failed")

    session_token, expires_in = create_session_token(user.id, settings)
    record_login_audit_event(
        db,
        action="oidc_login",
        actor_user_id=user.id,
        correlation_id=correlation_id,
        detail={"email": user.email},
    )
    db.commit()

    query = urlencode({"access_token": session_token, "token_type": "bearer", "expires_in": expires_in})
    return RedirectResponse(f"{settings.frontend_success_url}#{query}", status_code=302)


@router.get("/whoami", response_model=WhoAmIResponse)
def whoami(user: AuthenticatedUser = Depends(get_current_user)) -> WhoAmIResponse:
    return WhoAmIResponse(
        user_id=str(user.user_id),
        email=user.email,
        display_name=user.display_name,
        is_breakglass=user.is_breakglass,
        api_token_id=str(user.api_token_id) if user.api_token_id else None,
        assignments=[
            RoleAssignmentOut(
                role=a.role.value,
                scope_type=a.scope_type,
                scope_id=str(a.scope_id) if a.scope_id else None,
            )
            for a in user.assignments
        ],
    )
