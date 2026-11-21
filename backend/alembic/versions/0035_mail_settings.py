"""SMTP adapter settings for report/alarm/billing notification emails --
raised directly by the user: relay through whatever MTA the operator
already runs. Singleton table, same convention as
`oidc_settings`/`branding_settings`. `smtp_password_ref_id` points into
`secret_references`, mirroring `oidc_settings.client_secret_ref_id`
exactly -- never a plaintext password column.

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-20

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mail_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("smtp_host", sa.String(255), nullable=False),
        sa.Column("smtp_port", sa.Integer, nullable=False, server_default="587"),
        sa.Column("smtp_username", sa.String(255), nullable=True),
        sa.Column(
            "smtp_password_ref_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("secret_references.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("use_tls", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("from_address", sa.String(255), nullable=False),
        sa.Column("from_name", sa.String(255), nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("mail_settings")
