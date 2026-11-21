"""Personal API tokens (docs/adr/0029): generation, hashing, permission
narrowing and request-time checks.

A token acts as its user, never as more:

- Its `grants` are a subset of what the user may hold. A grant is
  *covered* when the user has that role globally, or exactly that role on
  exactly that scope -- or when the user is a global `platform_admin`, who
  may already do everything and so may hand out any role on any scope.
- Coverage is re-checked against the user's *current* role assignments on
  every request: removing a role from a user immediately removes it from
  all their tokens, and a disabled user's tokens stop working.
- `read_only` tokens may only make GET/HEAD/OPTIONS requests.
- An optional CIDR allowlist restricts where a token may be used from.

Tokens look like `rsg_<43 url-safe chars>`; the fixed prefix makes a
leaked token easy to recognise (and to match in secret scanners). Only
the SHA-256 hex digest is stored. A plain hash (no salt/KDF) is the
standard choice here: the token is 256 bits of randomness, so there is
nothing to brute-force, and lookups need to be by exact hash.
"""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Request

from resurgam_api.auth.models import RoleAssignment
from resurgam_api.config import Settings
from resurgam_api.models.identity import RoleKey, ScopeType

TOKEN_PREFIX = "rsg_"
# 18 months -- the hard upper limit for any token's lifetime (decided by
# the user, 2026-11-11). Tokens must always expire.
MAX_LIFETIME_DAYS = 548
DISPLAY_PREFIX_LENGTH = 12
# Writing last-used on every request would turn every read into a write;
# once a minute per token is plenty for "when/where was this last used".
LAST_USED_WRITE_INTERVAL = timedelta(seconds=60)
READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Key on `Session.info` carrying the token label for the audit hook
# (`models/audit.py`) -- the session is per request.
SESSION_INFO_KEY = "api_token_label"


def looks_like_api_token(bearer: str) -> bool:
    return bearer.startswith(TOKEN_PREFIX)


def generate_token() -> tuple[str, str, str]:
    """Returns (token, display prefix, sha256 hex)."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return token, token[:DISPLAY_PREFIX_LENGTH], hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Grant:
    role: RoleKey
    scope_type: ScopeType
    scope_id: uuid.UUID | None

    @classmethod
    def from_json(cls, data: dict) -> Grant:
        scope_id = data.get("scope_id")
        return cls(
            role=RoleKey(data["role"]),
            scope_type=ScopeType(data["scope_type"]),
            scope_id=uuid.UUID(scope_id) if scope_id else None,
        )

    def to_json(self) -> dict:
        return {
            "role": self.role.value,
            "scope_type": self.scope_type.value,
            "scope_id": str(self.scope_id) if self.scope_id else None,
        }

    def as_assignment(self) -> RoleAssignment:
        return RoleAssignment(role=self.role, scope_type=self.scope_type, scope_id=self.scope_id)


def can_grant_anything(assignments: list[RoleAssignment]) -> bool:
    return any(a.role == RoleKey.PLATFORM_ADMIN and a.scope_type == ScopeType.GLOBAL for a in assignments)


def grant_is_covered(grant: Grant, assignments: list[RoleAssignment]) -> bool:
    if can_grant_anything(assignments):
        return True
    for a in assignments:
        if a.role != grant.role:
            continue
        if a.scope_type == ScopeType.GLOBAL:
            return True
        if a.scope_type == grant.scope_type and a.scope_id == grant.scope_id:
            return True
    return False


def effective_assignments(grants: list[Grant], user_assignments: list[RoleAssignment]) -> list[RoleAssignment]:
    """The token's grants that the user still covers right now."""
    return [g.as_assignment() for g in grants if grant_is_covered(g, user_assignments)]


def client_ip(request: Request, settings: Settings) -> str | None:
    """The calling client's IP. With `trust_proxy_headers` (appliance:
    Caddy sets `X-Forwarded-For` itself), the right-most entry -- the one
    the nearest, trusted proxy appended -- else the TCP peer."""
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            candidate = forwarded.split(",")[-1].strip()
            if candidate:
                return candidate
    return request.client.host if request.client else None


def normalize_cidrs(values: list[str]) -> list[str]:
    """Validates and normalises CIDRs (a bare address becomes /32 or /128).
    Raises ValueError naming the bad entry."""
    out: list[str] = []
    for raw in values:
        value = raw.strip()
        if not value:
            continue
        try:
            out.append(str(ipaddress.ip_network(value, strict=False)))
        except ValueError as exc:
            raise ValueError(f"'{value}' is not a valid IP address or CIDR range") from exc
    return out


def ip_allowed(ip: str | None, allowed_cidrs: list[str] | None) -> bool:
    if not allowed_cidrs:
        return True
    if ip is None:
        return False
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(address in ipaddress.ip_network(cidr, strict=False) for cidr in allowed_cidrs)


def expiry_from_days(days: int, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) + timedelta(days=days)
