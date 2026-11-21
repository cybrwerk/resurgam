import bcrypt
from sqlalchemy.orm import Session

from resurgam_api.config import Settings
from resurgam_api.models.identity import Role, RoleKey, ScopeType, User, UserRoleAssignment

_BREAKGLASS_EMAIL_SUFFIX = "@breakglass.local"


def verify_credentials(settings: Settings, username: str, password: str) -> bool:
    if not settings.breakglass_enabled:
        return False
    if username != settings.breakglass_username:
        return False
    if not settings.breakglass_password_hash:
        return False
    return bcrypt.checkpw(password.encode("utf-8"), settings.breakglass_password_hash.encode("utf-8"))


def get_or_create_breakglass_user(db: Session, settings: Settings) -> User:
    email = f"{settings.breakglass_username}{_BREAKGLASS_EMAIL_SUFFIX}"
    user = db.query(User).filter_by(email=email, is_breakglass=True).one_or_none()
    if user is None:
        user = User(
            email=email,
            display_name=f"Break-glass ({settings.breakglass_username})",
            is_breakglass=True,
        )
        db.add(user)
        db.flush()
    _ensure_platform_admin(db, user)
    return user


def _ensure_platform_admin(db: Session, user: User) -> None:
    """The break-glass account is the emergency way in (enabled only on the
    server, `BREAKGLASS_ENABLED` + a password hash): it must be able to fix
    what locked everyone else out -- e.g. "Reset MFA" for an admin who lost
    their second factor (ADR 0028). Without a role it could sign in but do
    nothing, so it always holds a global platform_admin assignment, granted
    again at every break-glass login if someone removed it."""
    role = db.query(Role).filter_by(key=RoleKey.PLATFORM_ADMIN.value).one_or_none()
    if role is None:
        return
    exists = (
        db.query(UserRoleAssignment)
        .filter_by(user_id=user.id, role_id=role.id, scope_type=ScopeType.GLOBAL.value)
        .filter(UserRoleAssignment.scope_id.is_(None))
        .first()
    )
    if exists is None:
        db.add(UserRoleAssignment(user_id=user.id, role_id=role.id, scope_type=ScopeType.GLOBAL.value))
        db.flush()
