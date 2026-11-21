from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from resurgam_api.auth.api_tokens import MAX_LIFETIME_DAYS
from resurgam_api.models.identity import RoleKey, ScopeType


class ApiTokenGrantIn(BaseModel):
    """One role the token may use, on one scope."""

    role: RoleKey = Field(description="Role the token may use, e.g. `viewer` or `restore_operator`.")
    scope_type: ScopeType = Field(
        description="`global` for the role everywhere, or `customer` (etc.) to limit it to one object."
    )
    scope_id: UUID | None = Field(
        default=None, description="The object for a non-global scope (e.g. the customer id). Null for `global`."
    )


class ApiTokenGrantOut(ApiTokenGrantIn):
    scope_name: str | None = Field(
        default=None, description="Readable name of the scope object (e.g. the customer name), if it still exists."
    )


class ApiTokenCreateRequest(BaseModel):
    name: str = Field(
        min_length=1, max_length=100, description="What the token is for, e.g. 'Zabbix' or 'Nightly export script'."
    )
    expires_in_days: int = Field(
        ge=1,
        le=MAX_LIFETIME_DAYS,
        description=f"Lifetime in days, 1-{MAX_LIFETIME_DAYS} (18 months). Every token expires.",
    )
    read_only: bool = Field(
        default=True,
        description=(
            "Read-only tokens can only make GET requests, whatever roles they carry. Turn off only for "
            "automation that has to change something."
        ),
    )
    grants: list[ApiTokenGrantIn] = Field(
        min_length=1,
        max_length=20,
        description=(
            "Roles the token may use. Each must be one you hold yourself (that role globally, or exactly that "
            "role on that scope); a platform admin may grant any role on any scope. Re-checked on every "
            "request against your current roles."
        ),
    )
    allowed_cidrs: list[str] | None = Field(
        default=None,
        max_length=20,
        description=(
            'Optional client IP allowlist, e.g. `["192.0.2.10", "10.20.0.0/24"]`. Requests from other '
            "addresses are refused. Null or empty: any address."
        ),
    )


ApiTokenStatus = Literal["active", "expired"]


class ApiTokenOut(BaseModel):
    id: UUID = Field(description="Token id (not the secret).")
    name: str = Field(description="What the token is for.")
    token_prefix: str = Field(description="The first characters of the token, to recognise it. Not usable on its own.")
    read_only: bool = Field(description="True: the token can only make GET requests.")
    grants: list[ApiTokenGrantOut] = Field(
        description="Roles the token may use (as long as the owner still holds them)."
    )
    allowed_cidrs: list[str] | None = Field(description="Client IP allowlist; null means any address.")
    created_at: datetime = Field(description="When the token was created.")
    expires_at: datetime = Field(description="When the token stops working.")
    last_used_at: datetime | None = Field(description="Last successful use (updated at most once a minute).")
    last_used_ip: str | None = Field(description="Client address of the last successful use.")
    status: ApiTokenStatus = Field(description="`active` or `expired`. Revoked tokens are not listed.")
    user_id: UUID = Field(description="The token's owner.")
    user_email: str = Field(description="Owner's email.")
    user_display_name: str = Field(description="Owner's display name.")


class ApiTokenCreatedOut(ApiTokenOut):
    token: str = Field(
        description="The token itself. Shown only in this response -- RESURGAM stores only a hash. "
        "Send it as `Authorization: Bearer <token>`."
    )


class ApiTokenGrantOption(BaseModel):
    role: RoleKey = Field(description="A role the caller may put on a token.")
    scope_type: ScopeType = Field(description="Scope type for that role.")
    scope_id: UUID | None = Field(description="Scope object, null for global.")
    scope_name: str | None = Field(description="Readable name of the scope object, if any.")


class ApiTokenGrantableOut(BaseModel):
    can_grant_any: bool = Field(
        description="True for a global platform admin: any role on any scope (listed in `options`) can be granted."
    )
    options: list[ApiTokenGrantOption] = Field(description="Every role/scope combination the caller may grant.")
    max_lifetime_days: int = Field(description="Longest allowed token lifetime in days (18 months).")
