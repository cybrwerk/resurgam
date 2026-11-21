"""PBS client-side backup encryption key support (docs/adr/0022): a
`Datastore` can now have an associated encryption keyfile, stored the same
way every other secret already is (`SecretReference` + `LocalSecretValue`
under a purpose-derived subkey), so RESURGAM's own direct
`proxmox-backup-client`/`proxmox-file-restore` calls (Recovery Lab,
`guest_exec_ephemeral`, `external_mount` indexing, the file-browsing API)
can read an already-encrypted backup once an operator uploads the matching
key. Adds `pbs_encryption_key` to `secret_references.purpose`'s allowed
values and a nullable `datastores.encryption_key_secret_ref_id` FK (SET
NULL, matching `OidcSettings.client_secret_ref_id`'s "optional secret"
shape -- most datastores have no key at all).

Revision ID: 0048
Revises: 0047
Create Date: 2026-09-26

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_PURPOSES = ("infra_credential", "mfa_totp", "oidc_client_secret", "smtp_password")
_NEW_PURPOSES = _OLD_PURPOSES + ("pbs_encryption_key",)


def upgrade() -> None:
    op.drop_constraint("ck_secret_references_purpose", "secret_references", type_="check")
    allowed = ", ".join(f"'{p}'" for p in _NEW_PURPOSES)
    op.create_check_constraint("ck_secret_references_purpose", "secret_references", sa.text(f"purpose IN ({allowed})"))

    op.add_column(
        "datastores",
        sa.Column("encryption_key_secret_ref_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_datastores_encryption_key_secret_ref_id",
        "datastores",
        "secret_references",
        ["encryption_key_secret_ref_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_datastores_encryption_key_secret_ref_id", "datastores", type_="foreignkey")
    op.drop_column("datastores", "encryption_key_secret_ref_id")

    op.drop_constraint("ck_secret_references_purpose", "secret_references", type_="check")
    allowed = ", ".join(f"'{p}'" for p in _OLD_PURPOSES)
    op.create_check_constraint("ck_secret_references_purpose", "secret_references", sa.text(f"purpose IN ({allowed})"))
