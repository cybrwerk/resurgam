import json
import uuid
from datetime import UTC, datetime

import webauthn
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from webauthn.helpers.exceptions import InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
)

from resurgam_api.auth.dependency import current_enrollment_jti, get_current_user, get_session_or_enrollment_user
from resurgam_api.auth.mfa import (
    generate_recovery_codes,
    generate_totp_secret,
    hash_recovery_code,
    totp_uri,
    verify_totp_code,
)
from resurgam_api.auth.mfa_enrollment import (
    FACTOR_ADDED_ACTION,
    RECOVERY_CODES_ACTION,
    record_enrollment_event,
    recovery_codes_generated_with,
)
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.auth.sessions import (
    InvalidSessionTokenError,
    create_webauthn_ceremony_token,
    verify_webauthn_ceremony_token,
)
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.instance_settings import mfa_required_for
from resurgam_api.models.identity import User
from resurgam_api.models.mfa import RecoveryCode, TotpCredential, WebauthnCredential
from resurgam_api.models.secrets import SecretPurpose, SecretReference
from resurgam_api.schemas.mfa import (
    MfaStatusOut,
    RecoveryCodesOut,
    TotpConfirmRequest,
    TotpEnrollResponse,
    WebauthnCeremonyOptionsOut,
    WebauthnCredentialOut,
    WebauthnRegisterOptionsRequest,
    WebauthnRegisterRequest,
)
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

router = APIRouter(prefix="/mfa", tags=["mfa"])


def _require_local_user(db: Session, auth_user: AuthenticatedUser) -> User:
    """MFA enrollment only makes sense for a real local account — the
    break-glass credential is env-configured (no per-user row to attach a
    TOTP secret to that would survive a config change) and OIDC users
    manage MFA in authentik itself, not here.
    """
    user = db.get(User, auth_user.user_id)
    if user is None or user.password_hash is None:
        raise ResurgamApiError(
            code="no_local_account",
            message="MFA is only available for local accounts (not break-glass or SSO).",
            status_code=400,
        )
    return user


def _primary_factor_count(db: Session, user_id) -> int:
    totp = (
        db.query(TotpCredential)
        .filter(TotpCredential.user_id == user_id, TotpCredential.confirmed_at.isnot(None))
        .count()
    )
    return totp + db.query(WebauthnCredential).filter_by(user_id=user_id).count()


def _refuse_removing_last_required_factor(db: Session, user: User) -> None:
    """While an admin requires MFA for this account, its last TOTP/security
    key cannot be removed -- otherwise "mandatory" would end with one click."""
    if mfa_required_for(db, user) and _primary_factor_count(db, user.id) <= 1:
        raise ResurgamApiError(
            code="mfa_required_last_factor",
            message="Your administrator requires MFA for this account, so the last authenticator app or "
            "security key cannot be removed. Add another one first.",
            status_code=409,
        )


@router.get("/status", response_model=MfaStatusOut)
def mfa_status(
    db: Session = Depends(get_db), auth_user: AuthenticatedUser = Depends(get_session_or_enrollment_user)
) -> MfaStatusOut:
    user = _require_local_user(db, auth_user)
    totp = (
        db.query(TotpCredential)
        .filter(TotpCredential.user_id == user.id, TotpCredential.confirmed_at.isnot(None))
        .one_or_none()
    )
    webauthn_creds = db.query(WebauthnCredential).filter_by(user_id=user.id).all()
    remaining = db.query(RecoveryCode).filter_by(user_id=user.id, used_at=None).count()
    return MfaStatusOut(
        totp_enabled=totp is not None,
        webauthn_credentials=[
            WebauthnCredentialOut(id=c.id, nickname=c.nickname, created_at=c.created_at) for c in webauthn_creds
        ],
        recovery_codes_remaining=remaining,
        mfa_required=mfa_required_for(db, user),
    )


@router.post("/totp/enroll", response_model=TotpEnrollResponse)
def totp_enroll(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    auth_user: AuthenticatedUser = Depends(get_session_or_enrollment_user),
) -> TotpEnrollResponse:
    user = _require_local_user(db, auth_user)

    existing = db.query(TotpCredential).filter_by(user_id=user.id).one_or_none()
    if existing is not None and existing.confirmed_at is not None:
        raise ResurgamApiError(
            code="totp_already_enabled",
            message="TOTP is already enabled — remove it first to re-enroll.",
            status_code=409,
        )
    if existing is not None:
        # An unconfirmed enrollment from a previous, abandoned attempt —
        # replace it rather than accumulate orphaned secrets.
        old_ref = db.get(SecretReference, existing.secret_ref_id)
        provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
        provider.delete(old_ref.reference_key)
        db.delete(existing)
        db.delete(old_ref)
        db.flush()

    secret = generate_totp_secret()
    reference_key = f"totp-secret-{uuid.uuid4()}"
    secret_ref = SecretReference(
        reference_key=reference_key,
        description=f"TOTP secret for user '{user.email}'",
        purpose=SecretPurpose.MFA_TOTP,
    )
    db.add(secret_ref)
    db.flush()

    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    provider.store(reference_key, secret)

    db.add(TotpCredential(user_id=user.id, secret_ref_id=secret_ref.id))
    db.commit()

    return TotpEnrollResponse(secret=secret, otpauth_uri=totp_uri(secret, account_email=user.email))


@router.post("/totp/confirm", status_code=204)
def totp_confirm(
    body: TotpConfirmRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    auth_user: AuthenticatedUser = Depends(get_session_or_enrollment_user),
) -> None:
    user = _require_local_user(db, auth_user)
    totp = db.query(TotpCredential).filter_by(user_id=user.id, confirmed_at=None).one_or_none()
    if totp is None:
        raise ResurgamApiError(
            code="no_pending_enrollment", message="No pending TOTP enrollment to confirm.", status_code=400
        )

    secret_ref = db.get(SecretReference, totp.secret_ref_id)
    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    secret = provider.retrieve(secret_ref.reference_key)

    if not verify_totp_code(secret, body.code):
        raise ResurgamApiError(code="invalid_code", message="Invalid TOTP code.", status_code=401)

    totp.confirmed_at = datetime.now(UTC)
    _bind_to_enrollment(db, request, user, factor="totp", credential_id=totp.id)
    db.commit()


def _bind_to_enrollment(db: Session, request: Request, user: User, *, factor: str, credential_id: uuid.UUID) -> None:
    """Added with an enrollment token: record which token, so only that
    token can finish this enrollment (`auth/mfa_enrollment.py`)."""
    jti = current_enrollment_jti(request)
    if jti is not None:
        record_enrollment_event(
            db,
            user_id=user.id,
            action=FACTOR_ADDED_ACTION,
            jti=jti,
            correlation_id=getattr(request.state, "correlation_id", None),
            factor=factor,
            credential_id=str(credential_id),
        )


@router.delete("/totp", status_code=204)
def totp_remove(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    auth_user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    user = _require_local_user(db, auth_user)
    totp = db.query(TotpCredential).filter_by(user_id=user.id).one_or_none()
    if totp is None:
        raise ResurgamApiError(code="not_found", message="TOTP is not enrolled.", status_code=404)
    if totp.confirmed_at is not None:
        _refuse_removing_last_required_factor(db, user)

    secret_ref = db.get(SecretReference, totp.secret_ref_id)
    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    provider.delete(secret_ref.reference_key)
    db.delete(totp)
    db.delete(secret_ref)
    db.commit()


@router.post("/webauthn/register-options", response_model=WebauthnCeremonyOptionsOut)
def webauthn_register_options(
    body: WebauthnRegisterOptionsRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    auth_user: AuthenticatedUser = Depends(get_session_or_enrollment_user),
) -> WebauthnCeremonyOptionsOut:
    user = _require_local_user(db, auth_user)
    existing = db.query(WebauthnCredential).filter_by(user_id=user.id).all()

    options = webauthn.generate_registration_options(
        rp_id=settings.webauthn_rp_id,
        rp_name=settings.webauthn_rp_name,
        user_id=user.id.bytes,
        user_name=user.email,
        user_display_name=user.display_name,
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=webauthn.base64url_to_bytes(c.credential_id)) for c in existing
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.PREFERRED),
    )
    # The nickname is chosen before the ceremony starts and has nowhere
    # else to live until the credential is actually created — carried as
    # its own claim on the same stateless ceremony token as the challenge.
    ceremony_token = create_webauthn_ceremony_token(
        user_id=user.id, challenge=options.challenge, action="register", settings=settings, nickname=body.nickname
    )
    return WebauthnCeremonyOptionsOut(
        options=json.loads(webauthn.options_to_json(options)), ceremony_token=ceremony_token
    )


@router.post("/webauthn/register", response_model=WebauthnCredentialOut)
def webauthn_register(
    body: WebauthnRegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    auth_user: AuthenticatedUser = Depends(get_session_or_enrollment_user),
) -> WebauthnCredentialOut:
    user = _require_local_user(db, auth_user)

    try:
        ceremony_user_id, challenge, nickname = verify_webauthn_ceremony_token(
            body.ceremony_token, action="register", settings=settings
        )
    except InvalidSessionTokenError as exc:
        raise ResurgamApiError(
            code="invalid_challenge", message="WebAuthn ceremony is invalid or expired.", status_code=401
        ) from exc
    if ceremony_user_id != user.id:
        raise ResurgamApiError(
            code="invalid_challenge", message="WebAuthn ceremony does not match this account.", status_code=401
        )
    nickname = nickname or "Security key"

    try:
        result = webauthn.verify_registration_response(
            credential=body.credential,
            expected_challenge=challenge,
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origin,
        )
    except InvalidRegistrationResponse as exc:
        raise ResurgamApiError(
            code="invalid_credential", message="Security key registration failed.", status_code=400
        ) from exc

    credential = WebauthnCredential(
        user_id=user.id,
        nickname=nickname,
        credential_id=webauthn.helpers.bytes_to_base64url(result.credential_id),
        public_key=webauthn.helpers.bytes_to_base64url(result.credential_public_key),
        sign_count=result.sign_count,
    )
    db.add(credential)
    db.flush()
    _bind_to_enrollment(db, request, user, factor="webauthn", credential_id=credential.id)
    db.commit()
    db.refresh(credential)
    return WebauthnCredentialOut(id=credential.id, nickname=credential.nickname, created_at=credential.created_at)


@router.delete("/webauthn/{credential_id}", status_code=204)
def webauthn_remove(
    credential_id: uuid.UUID,
    db: Session = Depends(get_db),
    auth_user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    user = _require_local_user(db, auth_user)
    credential = db.query(WebauthnCredential).filter_by(id=credential_id, user_id=user.id).one_or_none()
    if credential is None:
        raise ResurgamApiError(code="not_found", message="Security key not found.", status_code=404)
    _refuse_removing_last_required_factor(db, user)
    db.delete(credential)
    db.commit()


@router.post("/recovery-codes/generate", response_model=RecoveryCodesOut)
def recovery_codes_generate(
    request: Request,
    db: Session = Depends(get_db),
    auth_user: AuthenticatedUser = Depends(get_session_or_enrollment_user),
) -> RecoveryCodesOut:
    """Replaces every recovery code of the account with a fresh batch (shown
    once). With an enrollment token (forced MFA setup) this works once per
    enrollment, right after its first factor; regenerating later needs a
    real session (403 `recovery_codes_need_session`)."""
    user = _require_local_user(db, auth_user)
    jti = current_enrollment_jti(request)
    if jti is not None:
        # Serialise concurrent calls with the same token on the user row.
        db.query(User).filter(User.id == user.id).with_for_update().one()
        if recovery_codes_generated_with(db, user.id, jti):
            raise ResurgamApiError(
                code="recovery_codes_need_session",
                message="Recovery codes were already created for this setup. Sign in to generate new ones.",
                status_code=403,
            )

    has_totp = (
        db.query(TotpCredential)
        .filter(TotpCredential.user_id == user.id, TotpCredential.confirmed_at.isnot(None))
        .one_or_none()
        is not None
    )
    has_webauthn = db.query(WebauthnCredential).filter_by(user_id=user.id).count() > 0
    if not has_totp and not has_webauthn:
        raise ResurgamApiError(
            code="no_primary_mfa_method",
            message="Recovery codes require an existing TOTP or security-key method to back up.",
            status_code=400,
        )

    db.query(RecoveryCode).filter_by(user_id=user.id).delete()
    codes = generate_recovery_codes()
    for code in codes:
        db.add(RecoveryCode(user_id=user.id, code_hash=hash_recovery_code(code)))
    if jti is not None:
        record_enrollment_event(
            db,
            user_id=user.id,
            action=RECOVERY_CODES_ACTION,
            jti=jti,
            correlation_id=getattr(request.state, "correlation_id", None),
        )
    db.commit()
    return RecoveryCodesOut(codes=codes)
