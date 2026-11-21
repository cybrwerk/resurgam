"""Personal API tokens (docs/adr/0029).

Every endpoint here needs a login session (`get_session_user`): a token can
never create, list or revoke tokens itself. Users manage their own tokens;
a platform admin can additionally list and revoke everyone's.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from resurgam_api.auth.api_tokens import (
    MAX_LIFETIME_DAYS,
    Grant,
    can_grant_anything,
    expiry_from_days,
    generate_token,
    grant_is_covered,
    normalize_cidrs,
)
from resurgam_api.auth.dependency import get_session_user
from resurgam_api.auth.models import AuthenticatedUser
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.models.api_token import ApiToken
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.identity import RoleKey, ScopeType, User
from resurgam_api.models.tenancy import Customer
from resurgam_api.schemas.api_tokens import (
    ApiTokenCreatedOut,
    ApiTokenCreateRequest,
    ApiTokenGrantableOut,
    ApiTokenGrantOption,
    ApiTokenGrantOut,
    ApiTokenOut,
)

router = APIRouter(prefix="/api-tokens", tags=["api-tokens"])

# Plenty for real use; stops a runaway script from filling the table.
MAX_ACTIVE_TOKENS_PER_USER = 50


def _audit(db: Session, request: Request, user: AuthenticatedUser, action: str, token: ApiToken, **extra) -> None:
    db.add(
        AuditEvent(
            actor_user_id=user.user_id,
            action=action,
            target_type="api_token",
            target_id=str(token.id),
            correlation_id=getattr(request.state, "correlation_id", None),
            detail={
                "name": token.name,
                "token_prefix": token.token_prefix,
                "owner_user_id": str(token.user_id),
                **extra,
            },
        )
    )


def _customer_names(db: Session) -> dict[uuid.UUID, str]:
    return {c.id: c.name for c in db.query(Customer).all()}


def _to_out(row: ApiToken, owner: User, names: dict[uuid.UUID, str], now: datetime) -> ApiTokenOut:
    grants = [
        ApiTokenGrantOut(
            role=g.role,
            scope_type=g.scope_type,
            scope_id=g.scope_id,
            scope_name=names.get(g.scope_id) if g.scope_id else None,
        )
        for g in (Grant.from_json(raw) for raw in row.grants)
    ]
    return ApiTokenOut(
        id=row.id,
        name=row.name,
        token_prefix=row.token_prefix,
        read_only=row.read_only,
        grants=grants,
        allowed_cidrs=row.allowed_cidrs,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        last_used_ip=row.last_used_ip,
        status="active" if row.expires_at > now else "expired",
        user_id=owner.id,
        user_email=owner.email,
        user_display_name=owner.display_name,
    )


def _list(db: Session, user_id: uuid.UUID | None) -> list[ApiTokenOut]:
    query = db.query(ApiToken, User).join(User, User.id == ApiToken.user_id).filter(ApiToken.revoked_at.is_(None))
    if user_id is not None:
        query = query.filter(ApiToken.user_id == user_id)
    names = _customer_names(db)
    now = datetime.now(UTC)
    return [_to_out(token, owner, names, now) for token, owner in query.order_by(ApiToken.created_at.desc()).all()]


@router.get("/mine", response_model=list[ApiTokenOut])
def list_my_api_tokens(
    db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_session_user)
) -> list[ApiTokenOut]:
    """Your own API tokens (active and expired; revoked ones are not listed)."""
    return _list(db, user.user_id)


@router.get("/grantable", response_model=ApiTokenGrantableOut)
def list_grantable_api_token_roles(
    db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_session_user)
) -> ApiTokenGrantableOut:
    """Which roles on which scopes you may put on a new token: your own role
    assignments, or -- as a global platform admin -- every role globally and
    on every customer."""
    names = _customer_names(db)
    if can_grant_anything(user.assignments):
        options = [
            ApiTokenGrantOption(role=r, scope_type=ScopeType.GLOBAL, scope_id=None, scope_name=None) for r in RoleKey
        ]
        options += [
            ApiTokenGrantOption(role=r, scope_type=ScopeType.CUSTOMER, scope_id=cid, scope_name=name)
            for cid, name in sorted(names.items(), key=lambda item: item[1].lower())
            for r in RoleKey
            if r != RoleKey.PLATFORM_ADMIN  # platform admin is a global-only role
        ]
        return ApiTokenGrantableOut(can_grant_any=True, options=options, max_lifetime_days=MAX_LIFETIME_DAYS)
    options = [
        ApiTokenGrantOption(
            role=a.role,
            scope_type=a.scope_type,
            scope_id=a.scope_id,
            scope_name=names.get(a.scope_id) if a.scope_id else None,
        )
        for a in user.assignments
    ]
    return ApiTokenGrantableOut(can_grant_any=False, options=options, max_lifetime_days=MAX_LIFETIME_DAYS)


@router.post("", response_model=ApiTokenCreatedOut)
def create_api_token(
    body: ApiTokenCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_session_user),
) -> ApiTokenCreatedOut:
    """Create a personal API token. The token is in this response only --
    store it safely. Use it as `Authorization: Bearer <token>`. It acts as
    you, limited to `grants` (and to GET requests if `read_only`), and
    stops working when it expires, is revoked, or your account is disabled.
    The break-glass account can't create tokens."""
    if user.is_breakglass:
        raise ResurgamApiError(
            code="breakglass_cannot_create_api_token",
            message="The emergency access account can't create API tokens.",
            status_code=403,
        )

    grants: list[Grant] = []
    for item in body.grants:
        if item.scope_type == ScopeType.GLOBAL and item.scope_id is not None:
            raise ResurgamApiError(
                code="invalid_grant", message="A global grant can't name a scope object.", status_code=422
            )
        if item.scope_type != ScopeType.GLOBAL and item.scope_id is None:
            raise ResurgamApiError(
                code="invalid_grant", message=f"A '{item.scope_type.value}' grant needs a scope_id.", status_code=422
            )
        if item.role == RoleKey.PLATFORM_ADMIN and item.scope_type != ScopeType.GLOBAL:
            raise ResurgamApiError(
                code="invalid_grant", message="platform_admin can only be granted globally.", status_code=422
            )
        if item.scope_type == ScopeType.CUSTOMER and db.get(Customer, item.scope_id) is None:
            raise ResurgamApiError(code="invalid_grant", message="Unknown customer in grant.", status_code=422)
        grant = Grant(role=item.role, scope_type=item.scope_type, scope_id=item.scope_id)
        if not grant_is_covered(grant, user.assignments):
            raise ResurgamApiError(
                code="grant_not_allowed",
                message=f"You can't grant '{item.role.value}' on this scope: you don't hold it yourself.",
                status_code=403,
            )
        if grant not in grants:
            grants.append(grant)

    try:
        cidrs = normalize_cidrs(body.allowed_cidrs or [])
    except ValueError as exc:
        raise ResurgamApiError(code="invalid_cidr", message=str(exc), status_code=422) from exc

    now = datetime.now(UTC)
    active = (
        db.query(ApiToken)
        .filter(ApiToken.user_id == user.user_id, ApiToken.revoked_at.is_(None), ApiToken.expires_at > now)
        .count()
    )
    if active >= MAX_ACTIVE_TOKENS_PER_USER:
        raise ResurgamApiError(
            code="too_many_api_tokens",
            message=f"You already have {MAX_ACTIVE_TOKENS_PER_USER} active tokens; revoke some first.",
            status_code=409,
        )

    token, prefix, digest = generate_token()
    row = ApiToken(
        user_id=user.user_id,
        name=body.name.strip(),
        token_prefix=prefix,
        token_hash=digest,
        read_only=body.read_only,
        grants=[g.to_json() for g in grants],
        allowed_cidrs=cidrs or None,
        expires_at=expiry_from_days(body.expires_in_days, now),
    )
    db.add(row)
    db.flush()
    _audit(
        db,
        request,
        user,
        "api_token_created",
        row,
        read_only=row.read_only,
        grants=row.grants,
        allowed_cidrs=row.allowed_cidrs,
        expires_at=row.expires_at.isoformat(),
    )
    db.commit()
    db.refresh(row)
    owner = db.get(User, user.user_id)
    out = _to_out(row, owner, _customer_names(db), datetime.now(UTC))
    return ApiTokenCreatedOut(**out.model_dump(), token=token)


@router.get("", response_model=list[ApiTokenOut])
def list_all_api_tokens(
    db: Session = Depends(get_db), user: AuthenticatedUser = Depends(get_session_user)
) -> list[ApiTokenOut]:
    """Every user's API tokens (platform admin only), newest first."""
    if not user.has_role(RoleKey.PLATFORM_ADMIN):
        raise ResurgamApiError(code="forbidden", message="Requires role 'platform_admin'.", status_code=403)
    return _list(db, None)


@router.delete("/{token_id}", status_code=204)
def revoke_api_token(
    token_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_session_user),
) -> None:
    """Revoke a token at once. Your own tokens, or -- as a platform admin --
    anyone's. Fails with 404 for an unknown or already revoked token."""
    row = db.get(ApiToken, token_id)
    if row is None or row.revoked_at is not None:
        raise ResurgamApiError(code="not_found", message="API token not found.", status_code=404)
    if row.user_id != user.user_id and not user.has_role(RoleKey.PLATFORM_ADMIN):
        raise ResurgamApiError(code="not_found", message="API token not found.", status_code=404)
    row.revoked_at = datetime.now(UTC)
    row.revoked_by_user_id = user.user_id
    _audit(db, request, user, "api_token_revoked", row)
    db.commit()
