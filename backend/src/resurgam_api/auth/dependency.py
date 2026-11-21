from dataclasses import replace
from datetime import UTC, datetime

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from resurgam_api.auth.api_tokens import (
    LAST_USED_WRITE_INTERVAL,
    READ_ONLY_METHODS,
    SESSION_INFO_KEY,
    Grant,
    client_ip,
    effective_assignments,
    hash_token,
    ip_allowed,
    looks_like_api_token,
)
from resurgam_api.auth.mfa_enrollment import enrollment_token_usable
from resurgam_api.auth.models import AuthenticatedUser, RoleAssignment
from resurgam_api.auth.sessions import InvalidSessionTokenError, verify_mfa_enrollment_token, verify_session_token
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.api_token import ApiToken
from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment


def extract_bearer_token(authorization: str | None) -> str | None:
    """Parses an `Authorization: Bearer <token>` header case-insensitively
    (per RFC 7235, the scheme name is case-insensitive), returning `None`
    on a missing/malformed header rather than raising -- every caller
    (user sessions here, `resurgam-lab-agent`'s cluster token, and a Lab
    run's appliance-callback token) wants its own domain-specific error
    code on failure, not this shared parsing logic's.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization.split(" ", 1)[1].strip()


def _invalid_api_token() -> ResurgamApiError:
    # One answer for unknown, revoked and expired tokens: telling them
    # apart would help someone holding a stolen or guessed token.
    return ResurgamApiError(
        code="invalid_api_token", message="API token is invalid, expired or revoked.", status_code=401
    )


def _authenticate_api_token(token: str, request: Request, settings: Settings, db: Session) -> AuthenticatedUser:
    row = db.query(ApiToken).filter_by(token_hash=hash_token(token)).one_or_none()
    now = datetime.now(UTC)
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        raise _invalid_api_token()

    ip = client_ip(request, settings)
    if not ip_allowed(ip, row.allowed_cidrs):
        raise ResurgamApiError(
            code="api_token_ip_not_allowed",
            message="This API token may not be used from this address.",
            status_code=403,
        )
    if row.read_only and request.method.upper() not in READ_ONLY_METHODS:
        raise ResurgamApiError(
            code="api_token_read_only",
            message="This API token is read-only; it can only make GET requests.",
            status_code=403,
        )

    user = load_authenticated_user(db, row.user_id)  # 401 for a disabled/deleted user
    grants = [Grant.from_json(g) for g in row.grants]
    narrowed = replace(user, assignments=effective_assignments(grants, user.assignments), api_token_id=row.id)

    if row.last_used_at is None or now - row.last_used_at >= LAST_USED_WRITE_INTERVAL or row.last_used_ip != ip:
        row.last_used_at = now
        row.last_used_ip = ip
        db.commit()
    # Read by the audit hook (models/audit.py): actions taken with a token
    # are recorded as "<user> via API token <name>".
    db.info[SESSION_INFO_KEY] = f"{row.name} ({row.token_prefix}…)"
    return narrowed


def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> AuthenticatedUser:
    """The caller, from a login session token or a personal API token
    (`rsg_...`, docs/adr/0029). An API token's user carries only the
    token's grants, re-checked against the user's current roles."""
    token = extract_bearer_token(authorization)
    if token is None:
        raise ResurgamApiError(
            code="not_authenticated",
            message="Missing or malformed Authorization header.",
            status_code=401,
        )

    db.info.pop(SESSION_INFO_KEY, None)
    if looks_like_api_token(token):
        return _authenticate_api_token(token, request, settings, db)

    try:
        user_id = verify_session_token(token, settings)
    except InvalidSessionTokenError as exc:
        raise ResurgamApiError(
            code="invalid_session", message="Session token is invalid or expired.", status_code=401
        ) from exc

    return load_authenticated_user(db, user_id)


def get_session_user(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    """Like `get_current_user`, but refuses API tokens: for actions that
    must stay with a person at a login session -- managing API tokens,
    changing the own password (docs/adr/0029)."""
    if user.api_token_id is not None:
        raise ResurgamApiError(
            code="session_required",
            message="This action needs a signed-in user; API tokens can't be used for it.",
            status_code=403,
        )
    return user


def get_session_or_enrollment_user(
    request: Request,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> AuthenticatedUser:
    """Like `get_current_user`, but also accepts an MFA enrollment token
    (`create_mfa_enrollment_token`) -- used only by the endpoints that add
    a second factor (`api/v1/mfa.py`), so a user whose admin made MFA
    mandatory can set one up before getting a session. Nothing else
    accepts that token.

    An enrollment token is accepted only while it is not spent and every
    confirmed factor of the account was added with it
    (`auth/mfa_enrollment.enrollment_token_usable`, ADR 0028) -- so it can
    finish its own enrollment but never touch an account that already has
    a factor from elsewhere. Its `jti` is left on
    `request.state.mfa_enrollment_jti` (None for a session) for endpoints
    that bind what they create to the enrollment."""
    request.state.mfa_enrollment_jti = None
    token = extract_bearer_token(authorization)
    if token is None:
        raise ResurgamApiError(
            code="not_authenticated",
            message="Missing or malformed Authorization header.",
            status_code=401,
        )
    try:
        user_id = verify_session_token(token, settings)
    except InvalidSessionTokenError:
        try:
            claims = verify_mfa_enrollment_token(token, settings)
        except InvalidSessionTokenError as exc:
            raise ResurgamApiError(
                code="invalid_session", message="Session token is invalid or expired.", status_code=401
            ) from exc
        if not enrollment_token_usable(db, claims.user_id, claims.jti):
            raise ResurgamApiError(
                code="invalid_enrollment_token",
                message="The MFA setup session is no longer valid. Sign in again.",
                status_code=401,
            )
        user_id = claims.user_id
        request.state.mfa_enrollment_jti = claims.jti
    return load_authenticated_user(db, user_id)


def current_enrollment_jti(request: Request) -> str | None:
    """The enrollment token's `jti` when `get_session_or_enrollment_user`
    authenticated this request with one, else None (a real session)."""
    return getattr(request.state, "mfa_enrollment_jti", None)


def load_authenticated_user(db: Session, user_id) -> AuthenticatedUser:
    """The user + role assignments behind an already-verified token's subject -- shared by
    `get_current_user` (session tokens) and the file-restore download-ticket check."""
    user = db.get(User, user_id)
    if user is None or user.disabled:
        raise ResurgamApiError(code="invalid_session", message="User not found or disabled.", status_code=401)

    rows = (
        db.query(UserRoleAssignment, Role)
        .join(Role, Role.id == UserRoleAssignment.role_id)
        .filter(UserRoleAssignment.user_id == user.id)
        .all()
    )
    assignments = [
        RoleAssignment(
            role=RoleKey(role.key),
            scope_type=ScopeType(assignment.scope_type),
            scope_id=assignment.scope_id,
        )
        for assignment, role in rows
    ]

    return AuthenticatedUser(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_breakglass=user.is_breakglass,
        assignments=assignments,
    )


def require_role(role: RoleKey, scope_type: ScopeType | None = None):
    """FastAPI dependency factory enforcing manifest §7: every backend query
    must check the effective scope server-side. `scope_type=None` requires a
    global assignment; endpoints needing per-object scope checks (customer,
    pbs_instance, ...) should perform the object-specific check themselves
    using the AuthenticatedUser returned by get_current_user, using this
    factory only for the global-admin case.
    """

    def _dependency(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
        if not user.has_role(role, scope_type=scope_type):
            raise ResurgamApiError(
                code="forbidden",
                message=f"Requires role '{role.value}'.",
                status_code=403,
            )
        return user

    return _dependency
