import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from resurgam_api.auth.dependency import require_role
from resurgam_api.auth.local import hash_password
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.instance_settings import get_or_create_instance_settings, mfa_required_for_all
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.identity import Role, RoleAssignmentSource, RoleKey, User, UserRoleAssignment
from resurgam_api.models.mfa import RecoveryCode, TotpCredential, WebauthnCredential
from resurgam_api.models.secrets import SecretReference
from resurgam_api.schemas.user import (
    AssignRoleRequest,
    MfaPolicyOut,
    MfaPolicyUpdate,
    SetMfaRequirementRequest,
    SetUserPasswordRequest,
    UnassignRoleRequest,
    UserCreateRequest,
    UserOut,
    UserRoleAssignmentOut,
)
from resurgam_api.secrets.local_provider import LocalEncryptedSecretsProvider

router = APIRouter(prefix="/users", tags=["users"])
policy_router = APIRouter(prefix="/mfa-policy", tags=["users"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)


def _record_audit(db: Session, request: Request, *, actor_user_id, action: str, target_id, detail=None) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            target_type="user",
            target_id=str(target_id),
            correlation_id=getattr(request.state, "correlation_id", None),
            detail=detail,
        )
    )


def _roles_by_user_id(db: Session, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[UserRoleAssignmentOut]]:
    """A real UX-audit finding (2026-09-23): "Benutzer & Rollen" (Users &
    Roles) showed no role at all in its own list -- answering "who has
    platform_admin" meant opening every user one by one. One batched
    query for the whole page, not one per row.
    """
    if not user_ids:
        return {}
    rows = (
        db.query(UserRoleAssignment, Role)
        .join(Role, Role.id == UserRoleAssignment.role_id)
        .filter(UserRoleAssignment.user_id.in_(user_ids))
        .all()
    )
    by_user: dict[uuid.UUID, list[UserRoleAssignmentOut]] = {uid: [] for uid in user_ids}
    for assignment, role in rows:
        by_user[assignment.user_id].append(
            UserRoleAssignmentOut(
                id=assignment.id,
                role=RoleKey(role.key),
                scope_type=assignment.scope_type,
                scope_id=assignment.scope_id,
                source=assignment.source,
            )
        )
    return by_user


def _mfa_state(db: Session, user_ids: list[uuid.UUID]) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """(enrolled, resettable) for these users, in three batched queries:
    enrolled = a confirmed TOTP or a security key; resettable = anything
    `POST /users/{id}/reset-mfa` would remove (also an unconfirmed TOTP or
    leftover recovery codes)."""
    if not user_ids:
        return set(), set()
    totp_rows = (
        db.query(TotpCredential.user_id, TotpCredential.confirmed_at).filter(TotpCredential.user_id.in_(user_ids)).all()
    )
    keys = {
        row[0] for row in db.query(WebauthnCredential.user_id).filter(WebauthnCredential.user_id.in_(user_ids)).all()
    }
    codes = {row[0] for row in db.query(RecoveryCode.user_id).filter(RecoveryCode.user_id.in_(user_ids)).all()}
    enrolled = {uid for uid, confirmed_at in totp_rows if confirmed_at is not None} | keys
    resettable = {uid for uid, _ in totp_rows} | keys | codes
    return enrolled, resettable


def _out(db: Session, user: User) -> UserOut:
    enrolled, resettable = _mfa_state(db, [user.id])
    return _to_out(
        user,
        _roles_by_user_id(db, [user.id]).get(user.id),
        user.id in enrolled,
        mfa_required_for_all(db),
        mfa_reset_possible=user.id in resettable,
    )


def _to_out(
    user: User,
    roles: list[UserRoleAssignmentOut] | None = None,
    mfa_enrolled: bool = False,
    mfa_global: bool = False,
    *,
    mfa_reset_possible: bool = False,
) -> UserOut:
    local = not user.is_breakglass and user.password_hash is not None
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_breakglass=user.is_breakglass,
        disabled=user.disabled,
        has_password=user.password_hash is not None,
        created_at=user.created_at,
        roles=roles or [],
        mfa_required=user.mfa_required,
        mfa_required_effective=local and (user.mfa_required or mfa_global),
        mfa_enrolled=mfa_enrolled,
        mfa_reset_possible=local and mfa_reset_possible,
    )


def _get_user_or_404(db: Session, user_id: uuid.UUID) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise ResurgamApiError(code="not_found", message="User not found.", status_code=404)
    return user


@router.post("", response_model=UserOut)
def create_user(
    body: UserCreateRequest, request: Request, db: Session = Depends(get_db), user=Depends(_require_platform_admin)
) -> UserOut:
    """Not from the manifest — a real local-user account (email +
    individually-settable password), distinct from the single
    env-configured break-glass credential and from OIDC users (who have
    no password at all). An admin sets the initial password directly;
    the new user changes it themselves via `POST /auth/change-password`.
    A fresh account has no role assignments — see `assign_role` below —
    so it can log in but reach nothing until one is granted.
    """
    if db.query(User).filter_by(email=body.email).one_or_none() is not None:
        raise ResurgamApiError(code="email_taken", message=f"Email {body.email!r} is already in use.", status_code=409)

    new_user = User(
        email=body.email,
        display_name=body.display_name,
        password_hash=hash_password(body.password),
        mfa_required=body.mfa_required,
    )
    db.add(new_user)
    db.flush()

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="user_created",
        target_id=new_user.id,
        detail={"email": body.email, "mfa_required": body.mfa_required},
    )
    db.commit()
    db.refresh(new_user)
    return _out(db, new_user)


@router.get("", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db), _user=Depends(_require_platform_admin)) -> list[UserOut]:
    users = db.query(User).order_by(User.created_at).all()
    ids = [u.id for u in users]
    roles_by_user = _roles_by_user_id(db, ids)
    enrolled, resettable = _mfa_state(db, ids)
    mfa_global = mfa_required_for_all(db)
    return [
        _to_out(u, roles_by_user.get(u.id), u.id in enrolled, mfa_global, mfa_reset_possible=u.id in resettable)
        for u in users
    ]


@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: uuid.UUID, db: Session = Depends(get_db), _user=Depends(_require_platform_admin)) -> UserOut:
    return _out(db, _get_user_or_404(db, user_id))


def _active_platform_admin_ids(db: Session) -> set[uuid.UUID]:
    """Enabled, non-break-glass users holding `platform_admin` globally. The
    break-glass account does not count: it only exists while break-glass is
    switched on in the appliance's `.env`."""
    rows = (
        db.query(UserRoleAssignment.user_id, UserRoleAssignment.id)
        .join(Role, Role.id == UserRoleAssignment.role_id)
        .join(User, User.id == UserRoleAssignment.user_id)
        .filter(
            Role.key == RoleKey.PLATFORM_ADMIN.value,
            UserRoleAssignment.scope_type == "global",
            User.disabled.is_(False),
            User.is_breakglass.is_(False),
        )
        .all()
    )
    return {user_id for user_id, _ in rows}


def _refuse_losing_the_last_platform_admin(db: Session, remaining: set[uuid.UUID]) -> None:
    """Found live 2026-11-21: an admin could disable the last platform admin
    (even their own account) -- afterwards only break-glass gets back in."""
    if not remaining:
        raise ResurgamApiError(
            code="last_platform_admin",
            message="This would leave no active platform admin. Give another user the platform admin role first.",
            status_code=409,
        )


@router.post("/{user_id}/disable", response_model=UserOut)
def disable_user(
    user_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user=Depends(_require_platform_admin)
) -> UserOut:
    target = _get_user_or_404(db, user_id)
    if target.id == user.user_id:
        raise ResurgamApiError(
            code="cannot_disable_self",
            message="You can't disable your own account. Ask another platform admin.",
            status_code=409,
        )
    admins = _active_platform_admin_ids(db)
    if target.id in admins:
        _refuse_losing_the_last_platform_admin(db, admins - {target.id})
    target.disabled = True
    _record_audit(db, request, actor_user_id=user.user_id, action="user_disabled", target_id=target.id)
    db.commit()
    db.refresh(target)
    return _out(db, target)


@router.post("/{user_id}/set-password", response_model=UserOut)
def set_user_password(
    user_id: uuid.UUID,
    body: SetUserPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(_require_platform_admin),
) -> UserOut:
    """`platform_admin` only: set (or reset) a user's local-login password,
    making them a local account if they weren't one. Needed after a
    configuration import (`POST /config-backup/import`), which never
    carries password hashes -- imported users either sign in via SSO or
    get a password set here. Not allowed for the break-glass user, whose
    credential is configured on the server (environment), not here.
    """
    target = _get_user_or_404(db, user_id)
    if target.is_breakglass:
        raise ResurgamApiError(
            code="breakglass_password_not_settable",
            message="The break-glass user's password is configured on the server (BREAKGLASS_PASSWORD_HASH), "
            "not via this endpoint.",
            status_code=409,
        )
    target.password_hash = hash_password(body.password)
    _record_audit(db, request, actor_user_id=user.user_id, action="user_password_set", target_id=target.id)
    db.commit()
    db.refresh(target)
    return _out(db, target)


def _require_local_account(target: User, action: str) -> None:
    if target.is_breakglass or target.password_hash is None:
        raise ResurgamApiError(
            code="mfa_not_applicable",
            message=f"Cannot {action} for this account: MFA here only applies to local accounts. The break-glass "
            "credential is configured on the server, and SSO users set up MFA at their identity provider.",
            status_code=409,
        )


@router.post("/{user_id}/mfa-requirement", response_model=UserOut)
def set_mfa_requirement(
    user_id: uuid.UUID,
    body: SetMfaRequirementRequest,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(_require_platform_admin),
) -> UserOut:
    """`platform_admin` only: make MFA mandatory (or optional again) for a
    local account. Takes effect at the user's next login -- a running
    session is not ended. A user without a second factor is then taken
    through setting one up before getting a session (`POST
    /auth/local-login` answers `mfa_enrollment_required`). 409 for the
    break-glass user and SSO users."""
    target = _get_user_or_404(db, user_id)
    _require_local_account(target, "require MFA")
    target.mfa_required = body.required
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="user_mfa_required" if body.required else "user_mfa_optional",
        target_id=target.id,
    )
    db.commit()
    db.refresh(target)
    return _out(db, target)


@router.post("/{user_id}/reset-mfa", response_model=UserOut)
def reset_mfa(
    user_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> UserOut:
    """`platform_admin` only: remove every second factor of a local account
    (authenticator app, security keys, recovery codes) -- for a user who
    lost their phone or key. With MFA required, they set up a new one at
    their next login; otherwise they sign in with the password alone.
    Refused for your own account (use your profile instead)."""
    target = _get_user_or_404(db, user_id)
    _require_local_account(target, "reset MFA")
    if target.id == user.user_id:
        raise ResurgamApiError(
            code="cannot_reset_own_mfa",
            message="You cannot reset your own MFA here -- manage your factors on your profile page.",
            status_code=409,
        )
    provider = LocalEncryptedSecretsProvider(db, settings.secrets_local_encryption_key)
    removed = {"totp": 0, "security_keys": 0, "recovery_codes": 0}
    for totp in db.query(TotpCredential).filter_by(user_id=target.id).all():
        ref = db.get(SecretReference, totp.secret_ref_id)
        db.delete(totp)
        if ref is not None:
            provider.delete(ref.reference_key)
            db.delete(ref)
        removed["totp"] += 1
    removed["security_keys"] = db.query(WebauthnCredential).filter_by(user_id=target.id).delete()
    removed["recovery_codes"] = db.query(RecoveryCode).filter_by(user_id=target.id).delete()
    _record_audit(db, request, actor_user_id=user.user_id, action="user_mfa_reset", target_id=target.id, detail=removed)
    db.commit()
    db.refresh(target)
    return _out(db, target)


@router.post("/{user_id}/enable", response_model=UserOut)
def enable_user(
    user_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user=Depends(_require_platform_admin)
) -> UserOut:
    target = _get_user_or_404(db, user_id)
    target.disabled = False
    _record_audit(db, request, actor_user_id=user.user_id, action="user_enabled", target_id=target.id)
    db.commit()
    db.refresh(target)
    return _out(db, target)


@router.get("/{user_id}/role-assignments", response_model=list[UserRoleAssignmentOut])
def list_role_assignments(
    user_id: uuid.UUID, db: Session = Depends(get_db), _user=Depends(_require_platform_admin)
) -> list[UserRoleAssignmentOut]:
    _get_user_or_404(db, user_id)
    rows = (
        db.query(UserRoleAssignment, Role)
        .join(Role, Role.id == UserRoleAssignment.role_id)
        .filter(UserRoleAssignment.user_id == user_id)
        .all()
    )
    return [
        UserRoleAssignmentOut(
            id=assignment.id,
            role=RoleKey(role.key),
            scope_type=assignment.scope_type,
            scope_id=assignment.scope_id,
            source=assignment.source,
        )
        for assignment, role in rows
    ]


@router.post("/{user_id}/assign-role", response_model=UserRoleAssignmentOut)
def assign_role(
    user_id: uuid.UUID,
    body: AssignRoleRequest,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(_require_platform_admin),
) -> UserRoleAssignmentOut:
    _get_user_or_404(db, user_id)
    role = db.query(Role).filter_by(key=body.role.value).one_or_none()
    if role is None:
        raise ResurgamApiError(code="role_not_found", message=f"Role {body.role.value!r} not found.", status_code=400)

    existing = (
        db.query(UserRoleAssignment)
        .filter_by(user_id=user_id, role_id=role.id, scope_type=body.scope_type.value, scope_id=body.scope_id)
        .one_or_none()
    )
    if existing is not None:
        raise ResurgamApiError(
            code="assignment_exists", message="This role assignment already exists.", status_code=409
        )

    assignment = UserRoleAssignment(
        user_id=user_id,
        role_id=role.id,
        scope_type=body.scope_type.value,
        scope_id=body.scope_id,
        source=RoleAssignmentSource.MANUAL.value,
    )
    db.add(assignment)
    db.flush()

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="user_role_assigned",
        target_id=user_id,
        detail={
            "role": body.role.value,
            "scope_type": body.scope_type.value,
            "scope_id": str(body.scope_id) if body.scope_id else None,
        },
    )
    db.commit()
    db.refresh(assignment)
    return UserRoleAssignmentOut(
        id=assignment.id,
        role=body.role,
        scope_type=body.scope_type,
        scope_id=assignment.scope_id,
        source=assignment.source,
    )


@router.post("/{user_id}/unassign-role", status_code=204)
def unassign_role(
    user_id: uuid.UUID,
    body: UnassignRoleRequest,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(_require_platform_admin),
) -> None:
    _get_user_or_404(db, user_id)
    assignment = db.query(UserRoleAssignment).filter_by(id=body.assignment_id, user_id=user_id).one_or_none()
    if assignment is None:
        raise ResurgamApiError(code="not_found", message="Role assignment not found.", status_code=404)
    role = db.get(Role, assignment.role_id)
    if role is not None and role.key == RoleKey.PLATFORM_ADMIN.value and assignment.scope_type == "global":
        other_global_admin_grants = (
            db.query(UserRoleAssignment.id)
            .filter(
                UserRoleAssignment.user_id == user_id,
                UserRoleAssignment.role_id == assignment.role_id,
                UserRoleAssignment.scope_type == "global",
                UserRoleAssignment.id != assignment.id,
            )
            .count()
        )
        admins = _active_platform_admin_ids(db)
        if not other_global_admin_grants and user_id in admins:
            _refuse_losing_the_last_platform_admin(db, admins - {user_id})

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="user_role_unassigned",
        target_id=user_id,
        detail={"assignment_id": str(body.assignment_id)},
    )
    db.delete(assignment)
    db.commit()


@policy_router.get("", response_model=MfaPolicyOut)
def get_mfa_policy(db: Session = Depends(get_db), _user=Depends(_require_platform_admin)) -> MfaPolicyOut:
    """`platform_admin` only. Whether MFA is required for every local account."""
    return MfaPolicyOut(require_mfa_all_local_users=mfa_required_for_all(db))


@policy_router.put("", response_model=MfaPolicyOut)
def update_mfa_policy(
    body: MfaPolicyUpdate, request: Request, db: Session = Depends(get_db), user=Depends(_require_platform_admin)
) -> MfaPolicyOut:
    """`platform_admin` only. Requires (or stops requiring) a second factor for
    every local account, enforced like the per-user flag at the next sign-in:
    without one, a user is taken through setting it up; the last factor
    cannot be removed. Running sessions are not ended. Audited."""
    settings = get_or_create_instance_settings(db)
    settings.require_mfa_all_local_users = body.require_mfa_all_local_users
    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="mfa_policy_updated",
        target_id="global",
        detail={"require_mfa_all_local_users": body.require_mfa_all_local_users},
    )
    db.commit()
    return MfaPolicyOut(require_mfa_all_local_users=settings.require_mfa_all_local_users)
