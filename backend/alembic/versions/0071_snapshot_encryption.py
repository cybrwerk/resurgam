"""Client-side encryption per snapshot (ADR 0031, stage 1): the crypt mode PBS
reports for a snapshot's data archives and the key fingerprint of an encrypted
snapshot. Filled by the next inventory sync; NULL until then.

Revision ID: 0071
Revises: 0070
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backup_snapshots", sa.Column("crypt_mode", sa.String(length=20), nullable=True))
    op.add_column("backup_snapshots", sa.Column("key_fingerprint", sa.String(length=100), nullable=True))
    op.create_index(
        "ix_backup_snapshots_key_fingerprint",
        "backup_snapshots",
        ["key_fingerprint"],
        postgresql_where=sa.text("key_fingerprint IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_backup_snapshots_key_fingerprint", table_name="backup_snapshots")
    op.drop_column("backup_snapshots", "key_fingerprint")
    op.drop_column("backup_snapshots", "crypt_mode")
