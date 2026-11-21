from datetime import datetime
from enum import Enum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class SecretPurpose(str, Enum):
    """What kind of secret a `SecretReference` points to -- drives which
    HKDF-derived subkey `LocalEncryptedSecretsProvider` uses (see
    `secrets/key_derivation.py`). Set explicitly by every call site that
    creates a `SecretReference`, never inferred from `reference_key`'s
    own string shape: a typo'd prefix should fail loudly (a wrong
    subkey, so decryption raises), not silently fall back to a shared
    default.
    """

    INFRA_CREDENTIAL = "infra_credential"
    MFA_TOTP = "mfa_totp"
    OIDC_CLIENT_SECRET = "oidc_client_secret"
    SMTP_PASSWORD = "smtp_password"
    PBS_ENCRYPTION_KEY = "pbs_encryption_key"
    UPDATE_SOURCE_TOKEN = "update_source_token"


class SecretReference(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A pointer into the secrets provider — never the secret itself.

    Manifest §14: "keine API-Token oder Passwörter direkt in PostgreSQL
    speichern". This table is provider-agnostic by design: today the only
    provider is the interim `LocalEncryptedSecretsProvider` (see
    `LocalSecretValue` below and docs/adr/0005), storing ciphertext in a
    separate table. Swapping to a real provider (OpenBao) later means
    swapping which table/service `reference_key` resolves against, not
    changing this table or anything that references it (e.g. `pbs_instances`).
    """

    __tablename__ = "secret_references"

    reference_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    # A real gap found live (2026-09-24, UX audit): the Credentials
    # page's own description promised "wann er zuletzt rotiert wurde"
    # but no such column existed -- `LocalSecretValue.created_at` can't
    # stand in for it, since `LocalEncryptedSecretsProvider.store()`
    # updates that row's ciphertext in place on rotation rather than
    # inserting a new one. Deliberately kept here (provider-agnostic),
    # not on `LocalSecretValue`: "when was this reference's value last
    # changed" is a real, provider-independent concept -- a future
    # OpenBao-backed provider would want to expose the same fact via its
    # own versioning, not reinvent a parallel timestamp. Defaults to
    # `created_at`'s own moment (the value in place right now was set
    # then); every call site that changes the underlying value (the two
    # PATCH endpoints on PbsInstance/PveCluster, and this router's own
    # dedicated rotate endpoint) updates it explicitly alongside the
    # `provider.store()` call.
    last_rotated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # A real gap found live (2026-09-24, credential-store review): one
    # Fernet key protected every secret in `local_secret_values` --
    # PBS/PVE tokens, every user's TOTP seed, the OIDC client secret, the
    # SMTP password -- with no separation between them. `purpose` (one of
    # `SecretPurpose`) drives a per-purpose HKDF-derived subkey instead
    # (see `secrets/key_derivation.py`), so a confused-deputy bug -- code
    # that resolves the wrong `SecretReference` for a given operation --
    # fails decryption loudly instead of silently handing back the wrong
    # kind of secret. This does NOT protect against a leak of the master
    # `SECRETS_LOCAL_ENCRYPTION_KEY` itself -- every subkey is
    # deterministically re-derivable from it (that's what makes it
    # storage-free); see the ADR 0005 addendum for what this is and
    # isn't.
    purpose: Mapped[str] = mapped_column(String(30), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "purpose IN ('infra_credential', 'mfa_totp', 'oidc_client_secret', 'smtp_password', 'pbs_encryption_key', "
            "'update_source_token')",
            name="ck_secret_references_purpose",
        ),
    )


class LocalSecretValue(Base, CreatedAtMixin):
    """Ciphertext for the interim local secrets provider (docs/adr/0005).

    Deliberately a separate table from `secret_references`, not a column on
    it — `secret_references` stays a pure, provider-agnostic pointer table.
    """

    __tablename__ = "local_secret_values"

    reference_key: Mapped[str] = mapped_column(
        String(255), ForeignKey("secret_references.reference_key", ondelete="CASCADE"), primary_key=True
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
