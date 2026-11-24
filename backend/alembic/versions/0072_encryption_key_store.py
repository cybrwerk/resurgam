"""Encryption key store by fingerprint (ADR 0031, stage 2) and the per-tenant
switch that lets tenant admins download their own keys. Existing per-datastore
keys (ADR 0022) are adopted into the store by the collector scheduler -- their
fingerprint is inside the encrypted keyfile, which a migration cannot read.

Revision ID: 0072
Revises: 0071
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0072"
down_revision: str | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "encryption_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("fingerprint", sa.String(length=100), nullable=False, unique=True),
        sa.Column("label", sa.String(length=200), nullable=False, server_default=""),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "keyfile_secret_ref_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("secret_references.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kdf", sa.String(length=20), nullable=False, server_default="none"),
        sa.Column("key_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="upload"),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("copy_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "copy_confirmed_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_encryption_keys_customer_id", "encryption_keys", ["customer_id"])
    op.add_column(
        "customers",
        sa.Column("allow_tenant_key_download", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("customers", "allow_tenant_key_download")
    op.drop_index("ix_encryption_keys_customer_id", table_name="encryption_keys")
    op.drop_table("encryption_keys")
