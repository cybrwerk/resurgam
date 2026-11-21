import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class ApiToken(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A user's personal API token for machine access (scripts, monitoring,
    CI) -- docs/adr/0029. Acts as its user, but only with the role grants
    stored here, each re-checked against the user's *current* roles on
    every request (`auth/api_tokens.py`).

    Only a SHA-256 hash of the token is stored; the token itself is shown
    once at creation. `token_prefix` (the first characters, not secret on
    their own) lets people recognise a token in lists and logs.

    `grants`: `[{"role": "<RoleKey>", "scope_type": "<ScopeType>",
    "scope_id": "<uuid>" | null}, ...]`. `allowed_cidrs`: optional client
    IP allowlist (`["192.0.2.10/32", ...]`), `None` = any address.
    Revoking sets `revoked_at` rather than deleting, so the audit trail can
    still name the token.
    """

    __tablename__ = "api_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    grants: Mapped[list] = mapped_column(JSONB, nullable=False)
    allowed_cidrs: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
