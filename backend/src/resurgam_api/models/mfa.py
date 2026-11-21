import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class TotpCredential(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One TOTP enrollment per local user. The secret itself is never
    stored here — only a pointer into the secrets provider
    (`secret_ref_id`), the same pattern already used for PBS/PVE API
    tokens, since a TOTP seed is just as sensitive as either. `confirmed_at`
    is null until the user has proven they can generate a valid code with
    it once — an enrolled-but-unconfirmed secret doesn't count as "MFA
    enabled" for login purposes, so a mis-scanned QR code can't silently
    lock a future login out.
    """

    __tablename__ = "totp_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    secret_ref_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("secret_references.id", ondelete="RESTRICT"), nullable=False
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WebauthnCredential(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One registered FIDO2/WebAuthn authenticator (security key or
    platform passkey). `credential_id`/`public_key` are base64url text,
    not raw bytes — that's the shape the `webauthn` library already
    hands back and expects, so no re-encoding at the boundary.
    `sign_count` is the authenticator's own signature counter; a login
    that reports a count not strictly greater than the stored value is
    rejected as a possible cloned-authenticator replay, per the WebAuthn
    spec's own recommendation.
    """

    __tablename__ = "webauthn_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    nickname: Mapped[str] = mapped_column(String(100), nullable=False)
    credential_id: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    public_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    sign_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class RecoveryCode(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One single-use MFA recovery code. Only a bcrypt hash is stored —
    same as a password — the plaintext is shown to the user exactly once,
    at generation time, and never again. `used_at` marks it spent;
    generating a new batch (`POST /mfa/recovery-codes/generate`) deletes
    every prior row for the user outright rather than leaving stale
    used/unused ones to accumulate.
    """

    __tablename__ = "recovery_codes"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(100), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
