import base64
import time
import uuid
from typing import NamedTuple

import jwt

from resurgam_api.config import Settings

_ALGORITHM = "HS256"
_MFA_CHALLENGE_PURPOSE = "mfa_challenge"
_MFA_ENROLLMENT_PURPOSE = "mfa_enrollment"
_WEBAUTHN_CEREMONY_PURPOSE = "webauthn_ceremony"


class InvalidSessionTokenError(Exception):
    pass


def create_session_token(user_id: uuid.UUID, settings: Settings) -> tuple[str, int]:
    if not settings.session_secret:
        raise RuntimeError("SESSION_SECRET is not configured")

    now = int(time.time())
    expires_in = settings.session_ttl_seconds
    payload = {"sub": str(user_id), "iat": now, "exp": now + expires_in}
    token = jwt.encode(payload, settings.session_secret, algorithm=_ALGORITHM)
    return token, expires_in


def verify_session_token(token: str, settings: Settings) -> uuid.UUID:
    if not settings.session_secret:
        raise RuntimeError("SESSION_SECRET is not configured")

    try:
        payload = jwt.decode(token, settings.session_secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise InvalidSessionTokenError(str(exc)) from exc

    if "purpose" in payload:
        # A challenge/ceremony token must never be accepted as a real
        # session — it lets an unauthenticated MFA-in-progress caller
        # reach zero business endpoints, only the MFA-verify ones.
        raise InvalidSessionTokenError("token is not a session token")

    try:
        return uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise InvalidSessionTokenError("token subject is not a valid user id") from exc


def create_mfa_challenge_token(user_id: uuid.UUID, settings: Settings) -> str:
    """Issued after a correct password but before a satisfied second
    factor. Short-lived (`mfa_challenge_ttl_seconds`) and only ever
    checked by the `/auth/mfa/verify-*` endpoints (`verify_mfa_challenge_token`)
    — `get_current_user` explicitly rejects any token carrying a `purpose`
    claim, see `verify_session_token` above.
    """
    if not settings.session_secret:
        raise RuntimeError("SESSION_SECRET is not configured")

    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "purpose": _MFA_CHALLENGE_PURPOSE,
        "iat": now,
        "exp": now + settings.mfa_challenge_ttl_seconds,
    }
    return jwt.encode(payload, settings.session_secret, algorithm=_ALGORITHM)


def verify_mfa_challenge_token(token: str, settings: Settings) -> uuid.UUID:
    if not settings.session_secret:
        raise RuntimeError("SESSION_SECRET is not configured")

    try:
        payload = jwt.decode(token, settings.session_secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise InvalidSessionTokenError(str(exc)) from exc

    if payload.get("purpose") != _MFA_CHALLENGE_PURPOSE:
        raise InvalidSessionTokenError("token is not an MFA challenge token")

    try:
        return uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise InvalidSessionTokenError("token subject is not a valid user id") from exc


class MfaEnrollmentClaims(NamedTuple):
    user_id: uuid.UUID
    jti: str
    issued_at: int


def create_mfa_enrollment_token(user_id: uuid.UUID, settings: Settings) -> tuple[str, int]:
    """Issued after a correct password when the account has `mfa_required`
    set but no second factor yet. Accepted only by the MFA enrollment
    endpoints (`get_session_or_enrollment_user`) and
    `/auth/mfa/enrollment/complete`, which swaps it for a real session once
    a factor is proven -- `verify_session_token` rejects it like every
    other token carrying a `purpose`. Returns (token, lifetime seconds).

    Every token carries a unique `jti`: the factors added with it and its
    one-time exchange for a session are bound to that id
    (`auth/mfa_enrollment.py`), so a token cannot be replayed after the
    enrollment it belongs to, nor used against a later one (ADR 0028).
    """
    if not settings.session_secret:
        raise RuntimeError("SESSION_SECRET is not configured")

    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "purpose": _MFA_ENROLLMENT_PURPOSE,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + settings.mfa_enrollment_ttl_seconds,
    }
    return jwt.encode(payload, settings.session_secret, algorithm=_ALGORITHM), settings.mfa_enrollment_ttl_seconds


def verify_mfa_enrollment_token(token: str, settings: Settings) -> MfaEnrollmentClaims:
    """Signature, expiry and purpose only -- whether the token may still be
    used for this account's current state is `enrollment_token_usable`."""
    if not settings.session_secret:
        raise RuntimeError("SESSION_SECRET is not configured")

    try:
        payload = jwt.decode(token, settings.session_secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise InvalidSessionTokenError(str(exc)) from exc

    if payload.get("purpose") != _MFA_ENROLLMENT_PURPOSE:
        raise InvalidSessionTokenError("token is not an MFA enrollment token")

    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti:
        raise InvalidSessionTokenError("enrollment token has no jti")

    try:
        return MfaEnrollmentClaims(user_id=uuid.UUID(payload["sub"]), jti=jti, issued_at=int(payload["iat"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidSessionTokenError("token subject is not a valid user id") from exc


def create_webauthn_ceremony_token(
    *, user_id: uuid.UUID, challenge: bytes, action: str, settings: Settings, nickname: str | None = None
) -> str:
    """Carries a WebAuthn registration/authentication ceremony's server
    challenge back to the client and back again, since RESURGAM has no
    server-side session store to stash it in between the "options" and
    "verify" calls — the same stateless-JWT approach already used for
    login sessions and the MFA challenge token above. `action` is
    `"register"` or `"authenticate"`, checked by the verify endpoint so a
    registration ceremony's token can't be replayed to satisfy a login.
    `nickname` (registration only) rides as its own JSON claim, not
    concatenated onto the challenge bytes — `challenge` is random bytes
    that can legitimately contain any byte value, including whatever
    delimiter a naive packing scheme might pick, silently corrupting the
    split (caught by a real, if intermittent, test failure).
    """
    if not settings.session_secret:
        raise RuntimeError("SESSION_SECRET is not configured")

    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "purpose": _WEBAUTHN_CEREMONY_PURPOSE,
        "action": action,
        "challenge": base64.urlsafe_b64encode(challenge).decode("ascii"),
        "iat": now,
        "exp": now + settings.mfa_challenge_ttl_seconds,
    }
    if nickname is not None:
        payload["nickname"] = nickname
    return jwt.encode(payload, settings.session_secret, algorithm=_ALGORITHM)


def verify_webauthn_ceremony_token(
    token: str, *, action: str, settings: Settings
) -> tuple[uuid.UUID, bytes, str | None]:
    if not settings.session_secret:
        raise RuntimeError("SESSION_SECRET is not configured")

    try:
        payload = jwt.decode(token, settings.session_secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise InvalidSessionTokenError(str(exc)) from exc

    if payload.get("purpose") != _WEBAUTHN_CEREMONY_PURPOSE or payload.get("action") != action:
        raise InvalidSessionTokenError("token is not a matching WebAuthn ceremony token")

    try:
        user_id = uuid.UUID(payload["sub"])
        challenge = base64.urlsafe_b64decode(payload["challenge"].encode("ascii"))
    except (KeyError, ValueError) as exc:
        raise InvalidSessionTokenError("malformed WebAuthn ceremony token") from exc

    return user_id, challenge, payload.get("nickname")


_FILE_RESTORE_DOWNLOAD_PURPOSE = "file_restore_download"


def create_file_restore_download_ticket(user_id: uuid.UUID, job_id: uuid.UUID, settings: Settings) -> tuple[str, int]:
    """A short-lived token that lets a plain browser navigation (no `Authorization` header)
    download one file-restore job's content -- so a multi-GB ZIP streams straight to disk instead
    of being buffered as a Blob in the page. Bound to the job (`jid`) and the user (`sub`); the
    download endpoint still re-runs the user's restore-access check on every use. Carries a
    `purpose` claim, so `verify_session_token` never accepts it as a session (and vice versa).
    Returns (token, expiry as epoch seconds).
    """
    if not settings.session_secret:
        raise RuntimeError("SESSION_SECRET is not configured")
    now = int(time.time())
    expires = now + settings.file_restore_download_ticket_ttl_seconds
    payload = {
        "sub": str(user_id),
        "jid": str(job_id),
        "purpose": _FILE_RESTORE_DOWNLOAD_PURPOSE,
        "iat": now,
        "exp": expires,
    }
    return jwt.encode(payload, settings.session_secret, algorithm=_ALGORITHM), expires


def verify_file_restore_download_ticket(token: str, job_id: uuid.UUID, settings: Settings) -> uuid.UUID:
    """Returns the ticket's user id; raises `InvalidSessionTokenError` when it is expired,
    tampered, not a download ticket, or issued for a different job."""
    if not settings.session_secret:
        raise RuntimeError("SESSION_SECRET is not configured")
    try:
        payload = jwt.decode(token, settings.session_secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise InvalidSessionTokenError(str(exc)) from exc
    if payload.get("purpose") != _FILE_RESTORE_DOWNLOAD_PURPOSE:
        raise InvalidSessionTokenError("token is not a file-restore download ticket")
    if payload.get("jid") != str(job_id):
        raise InvalidSessionTokenError("ticket was issued for a different job")
    try:
        return uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise InvalidSessionTokenError("token subject is not a valid user id") from exc
