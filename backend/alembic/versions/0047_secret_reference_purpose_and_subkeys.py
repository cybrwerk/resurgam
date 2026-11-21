"""Credential-store hardening (2026-09-24 security review): one Fernet key
protected every secret in `local_secret_values` -- PBS/PVE tokens, every
user's TOTP seed, the OIDC client secret, the SMTP password -- with no
separation between them. Adds `secret_references.purpose` (backfilled from
each row's real owner) and re-encrypts every `local_secret_values` row under
a purpose-derived HKDF subkey instead of the one shared master key, so a
confused-deputy bug (decrypting a `SecretReference` under the wrong assumed
purpose) fails loudly instead of silently handing back the wrong secret.

Does not protect against a leak of `SECRETS_LOCAL_ENCRYPTION_KEY` itself --
every subkey is deterministically re-derivable from it. See
`secrets/key_derivation.py` and the ADR 0005 addendum.

Revision ID: 0047
Revises: 0046
Create Date: 2026-09-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PURPOSES = ("infra_credential", "mfa_totp", "oidc_client_secret", "smtp_password")

_OIDC_REFERENCE_KEY = "oidc-client-secret"
_SMTP_REFERENCE_KEY = "mail-smtp-password"

secret_references = sa.table(
    "secret_references",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("reference_key", sa.String),
    sa.column("purpose", sa.String),
)
local_secret_values = sa.table(
    "local_secret_values",
    sa.column("reference_key", sa.String),
    sa.column("ciphertext", sa.LargeBinary),
)
pbs_instances = sa.table("pbs_instances", sa.column("api_token_secret_ref_id", postgresql.UUID(as_uuid=True)))
pve_clusters = sa.table("pve_clusters", sa.column("api_token_secret_ref_id", postgresql.UUID(as_uuid=True)))
totp_credentials = sa.table("totp_credentials", sa.column("secret_ref_id", postgresql.UUID(as_uuid=True)))


def _master_key() -> str:
    # Imported lazily, inside the function: importing app config at
    # module scope would make this migration file's own import fail in
    # any environment where `resurgam_api` isn't already on the path
    # (matching this file's job -- only ever run via `alembic upgrade`
    # from within the backend package, where it always is).
    from resurgam_api.config import get_settings

    key = get_settings().secrets_local_encryption_key
    if not key:
        raise RuntimeError(
            "SECRETS_LOCAL_ENCRYPTION_KEY is not configured -- required to re-encrypt "
            "existing local_secret_values rows during this migration."
        )
    return key


def _derive_purpose_key(master_key: str, purpose: str) -> bytes:
    from resurgam_api.secrets.key_derivation import derive_purpose_key

    return derive_purpose_key(master_key, purpose)


def upgrade() -> None:
    bind = op.get_bind()

    op.add_column("secret_references", sa.Column("purpose", sa.String(length=30), nullable=True))

    # Backfill from each row's real owner -- never guessed from
    # `reference_key`'s own string shape (see the model's own docstring).
    bind.execute(
        secret_references.update()
        .where(secret_references.c.id.in_(sa.select(pbs_instances.c.api_token_secret_ref_id)))
        .values(purpose="infra_credential")
    )
    bind.execute(
        secret_references.update()
        .where(secret_references.c.id.in_(sa.select(pve_clusters.c.api_token_secret_ref_id)))
        .values(purpose="infra_credential")
    )
    bind.execute(
        secret_references.update()
        .where(secret_references.c.id.in_(sa.select(totp_credentials.c.secret_ref_id)))
        .values(purpose="mfa_totp")
    )
    bind.execute(
        secret_references.update()
        .where(secret_references.c.reference_key == _OIDC_REFERENCE_KEY)
        .values(purpose="oidc_client_secret")
    )
    bind.execute(
        secret_references.update()
        .where(secret_references.c.reference_key == _SMTP_REFERENCE_KEY)
        .values(purpose="smtp_password")
    )

    unresolved = bind.execute(
        sa.select(sa.func.count()).select_from(secret_references).where(secret_references.c.purpose.is_(None))
    ).scalar_one()
    if unresolved:
        raise RuntimeError(
            f"{unresolved} secret_references row(s) matched none of the known owners "
            "(pbs_instances/pve_clusters/totp_credentials/fixed OIDC-or-SMTP reference_key) "
            "-- refusing to guess a purpose for them. Investigate before re-running."
        )

    op.alter_column("secret_references", "purpose", nullable=False)
    allowed = ", ".join(f"'{p}'" for p in _PURPOSES)
    op.create_check_constraint("ck_secret_references_purpose", "secret_references", sa.text(f"purpose IN ({allowed})"))

    # Re-encrypt every existing ciphertext: it was written under the one
    # direct master-key Fernet (every row, before this migration -- no
    # exceptions), and must move to its own purpose-derived subkey to be
    # decryptable by the provider code this migration ships alongside.
    rows = bind.execute(
        sa.select(
            local_secret_values.c.reference_key, local_secret_values.c.ciphertext, secret_references.c.purpose
        ).select_from(
            local_secret_values.join(
                secret_references, local_secret_values.c.reference_key == secret_references.c.reference_key
            )
        )
    ).all()

    # The key is only needed when there is something to re-encrypt: a
    # fresh, empty database (a new install, CI) migrates without it.
    if not rows:
        return

    master_key = _master_key()
    old_fernet = Fernet(master_key.encode("utf-8"))
    new_fernet_by_purpose: dict[str, Fernet] = {
        purpose: Fernet(_derive_purpose_key(master_key, purpose)) for purpose in _PURPOSES
    }

    for reference_key, ciphertext, purpose in rows:
        plaintext = old_fernet.decrypt(bytes(ciphertext))
        new_ciphertext = new_fernet_by_purpose[purpose].encrypt(plaintext)
        bind.execute(
            local_secret_values.update()
            .where(local_secret_values.c.reference_key == reference_key)
            .values(ciphertext=new_ciphertext)
        )


def downgrade() -> None:
    bind = op.get_bind()

    rows = bind.execute(
        sa.select(
            local_secret_values.c.reference_key, local_secret_values.c.ciphertext, secret_references.c.purpose
        ).select_from(
            local_secret_values.join(
                secret_references, local_secret_values.c.reference_key == secret_references.c.reference_key
            )
        )
    ).all()

    # The key is only needed when there is something to re-encrypt.
    if rows:
        master_key = _master_key()
        old_fernet = Fernet(master_key.encode("utf-8"))
        new_fernet_by_purpose: dict[str, Fernet] = {
            purpose: Fernet(_derive_purpose_key(master_key, purpose)) for purpose in _PURPOSES
        }
        for reference_key, ciphertext, purpose in rows:
            plaintext = new_fernet_by_purpose[purpose].decrypt(bytes(ciphertext))
            old_ciphertext = old_fernet.encrypt(plaintext)
            bind.execute(
                local_secret_values.update()
                .where(local_secret_values.c.reference_key == reference_key)
                .values(ciphertext=old_ciphertext)
            )

    op.drop_constraint("ck_secret_references_purpose", "secret_references", type_="check")
    op.drop_column("secret_references", "purpose")
