"""First-boot setup wizard (manifest §5.1/§17 Milestone 5), raised
directly by the user ("a guiding wizard on first boot would be great!")
as part of shaping the appliance's deployment story. Both endpoints are
deliberately unauthenticated -- a fresh install has no user to
authenticate as yet -- but `complete_setup` re-checks server-side that
setup genuinely hasn't happened already before doing anything, the same
"never trust frontend state alone" principle every other auth check in
this app follows. Once a real platform admin exists, this endpoint can
never create another one.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from resurgam_api.auth.local import hash_password
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.host_capabilities import read_kvm_status
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.identity import (
    Role,
    RoleAssignmentSource,
    RoleKey,
    ScopeType,
    User,
    UserRoleAssignment,
)
from resurgam_api.schemas.setup import SetupCompleteRequest, SetupStatusOut
from resurgam_api.schemas.user import UserOut

router = APIRouter(prefix="/setup", tags=["setup"])


def _has_platform_admin(db: Session) -> bool:
    return (
        db.query(UserRoleAssignment)
        .join(Role, Role.id == UserRoleAssignment.role_id)
        .filter(Role.key == RoleKey.PLATFORM_ADMIN.value, UserRoleAssignment.scope_type == ScopeType.GLOBAL.value)
        .first()
        is not None
    )


@router.get("/status", response_model=SetupStatusOut)
def get_setup_status(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> SetupStatusOut:
    state_file = Path(settings.host_capabilities_file) if settings.host_capabilities_file else None
    kvm = read_kvm_status(state_file)
    return SetupStatusOut(needs_setup=not _has_platform_admin(db), kvm_available=kvm.available, kvm_detail=kvm.detail)


@router.post("/complete", response_model=UserOut)
def complete_setup(body: SetupCompleteRequest, request: Request, db: Session = Depends(get_db)) -> UserOut:
    if _has_platform_admin(db):
        raise ResurgamApiError(
            code="setup_already_complete", message="Setup has already been completed.", status_code=409
        )
    if db.query(User).filter_by(email=body.email).one_or_none() is not None:
        raise ResurgamApiError(code="email_taken", message=f"Email {body.email!r} is already in use.", status_code=409)

    user = User(email=body.email, display_name=body.display_name, password_hash=hash_password(body.password))
    db.add(user)
    db.flush()

    role = db.query(Role).filter_by(key=RoleKey.PLATFORM_ADMIN.value).one()
    db.add(
        UserRoleAssignment(
            user_id=user.id,
            role_id=role.id,
            scope_type=ScopeType.GLOBAL.value,
            source=RoleAssignmentSource.MANUAL.value,
        )
    )

    db.add(
        AuditEvent(
            actor_user_id=user.id,
            action="setup_completed",
            target_type="user",
            target_id=str(user.id),
            correlation_id=getattr(request.state, "correlation_id", None),
            detail={"email": body.email},
        )
    )
    db.commit()
    db.refresh(user)
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_breakglass=False,
        disabled=False,
        has_password=True,
        created_at=user.created_at,
        roles=[],
    )
