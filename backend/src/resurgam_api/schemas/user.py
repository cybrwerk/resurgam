from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from resurgam_api.models.identity import RoleKey, ScopeType


class UserCreateRequest(BaseModel):
    email: str
    display_name: str
    password: str = Field(min_length=8)
    mfa_required: bool = Field(
        default=False,
        description="Require MFA from the start: the first login goes through setting up an authenticator app "
        "or security key before the user gets a session.",
    )


class SetMfaRequirementRequest(BaseModel):
    required: bool = Field(
        description="True: from the user's next login on, a session needs a second factor -- a user without one "
        "is taken through setting it up first, and the last one cannot be removed. False: MFA becomes optional "
        "again (factors already set up stay and are still asked for)."
    )


class SetUserPasswordRequest(BaseModel):
    password: str = Field(
        min_length=8,
        description="New local-login password for this user (at least 8 characters). Replaces any existing "
        "password; the user can change it afterwards via `POST /auth/change-password`.",
    )


class UserRoleAssignmentOut(BaseModel):
    id: UUID
    role: RoleKey
    scope_type: ScopeType
    scope_id: UUID | None
    source: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    display_name: str
    is_breakglass: bool
    disabled: bool
    has_password: bool
    created_at: datetime
    mfa_required: bool = Field(
        default=False, description="Whether an administrator requires MFA for this account (local accounts only)."
    )
    mfa_required_effective: bool = Field(
        default=False,
        description="Whether a second factor is mandatory for this account at all: its own `mfa_required`, or the "
        "global 'Require MFA for all local users' (GET /mfa-policy). Always false for break-glass and SSO users.",
    )
    mfa_enrolled: bool = Field(
        default=False,
        description="Whether the account has a confirmed authenticator app (TOTP) or a security key. False with "
        "`mfa_required` true means the user is taken through enrollment at the next login.",
    )
    mfa_reset_possible: bool = Field(
        default=False,
        description="Whether POST /users/{user_id}/reset-mfa has anything to remove: a TOTP (also an unconfirmed "
        "one), a security key or recovery codes. Always false for break-glass and SSO users.",
    )
    roles: list[UserRoleAssignmentOut] = Field(
        default_factory=list,
        description="Every role assignment this user currently holds -- resolved here (not just on the "
        "single-user role-assignments endpoint) so the user *list* itself answers 'who has platform_admin' "
        "without opening each user individually.",
    )


class AssignRoleRequest(BaseModel):
    role: RoleKey
    scope_type: ScopeType
    scope_id: UUID | None = None


class UnassignRoleRequest(BaseModel):
    assignment_id: UUID


class MfaPolicyOut(BaseModel):
    require_mfa_all_local_users: bool = Field(
        description="Whether every local account needs a second factor. Users without one are taken through setting "
        "it up at their next sign-in; break-glass and SSO users are not affected."
    )


class MfaPolicyUpdate(BaseModel):
    require_mfa_all_local_users: bool = Field(
        description="True: require MFA for every local account from its next sign-in on (running sessions are not "
        "ended). False: only accounts with their own 'Require MFA' flag need it."
    )
