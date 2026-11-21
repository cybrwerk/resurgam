import uuid
from dataclasses import dataclass

from resurgam_api.models.identity import RoleKey, ScopeType


@dataclass(frozen=True)
class RoleAssignment:
    role: RoleKey
    scope_type: ScopeType
    scope_id: uuid.UUID | None


@dataclass(frozen=True)
class AuthenticatedUser:
    """The stable shape every auth backend (break-glass today, OIDC later)
    must produce. Business endpoints depend on this type, never on how it
    was constructed — that is what lets real OIDC replace the break-glass
    stub later without touching call sites (see docs/adr/0004).
    """

    user_id: uuid.UUID
    email: str
    display_name: str
    is_breakglass: bool
    assignments: list[RoleAssignment]
    # Set when the request authenticated with a personal API token
    # (docs/adr/0029) instead of a login session; `assignments` are then
    # already narrowed to the token's grants.
    api_token_id: uuid.UUID | None = None

    def has_role(
        self, role: RoleKey, *, scope_type: ScopeType | None = None, scope_id: uuid.UUID | None = None
    ) -> bool:
        for assignment in self.assignments:
            if assignment.role != role:
                continue
            if assignment.scope_type == ScopeType.GLOBAL:
                return True
            if scope_type is not None and assignment.scope_type == scope_type and assignment.scope_id == scope_id:
                return True
        return False
