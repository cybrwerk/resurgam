"""Administration -> Integrations, manifest §6.3 -- OIDC config becomes
admin-editable and DB-backed, replacing `.env` as the source of truth when
a row is present. Singleton table: at most one row is ever expected
(enforced in application code, not a DB constraint -- no existing
precedent for a constrained-to-one-row table in this codebase, and a
unique constraint on a constant would be a strange way to express that).
`client_secret_ref_id` follows the existing `SecretReference` pattern
(ADR 0005), stored under a fixed, well-known reference key rather than a
new owning-row shape, since this is the first *singleton* secret in this
codebase (every other one is per-row: PBS instance, PVE cluster, per-user
TOTP).

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oidc_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("issuer", sa.String(500), nullable=False),
        sa.Column("client_id", sa.String(255), nullable=False),
        sa.Column(
            "client_secret_ref_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("secret_references.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("redirect_uri", sa.String(500), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("oidc_settings")
