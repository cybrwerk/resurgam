"""Platform-wide instance settings, currently just the RESURGAM-wide
report-retention floor a customer's own setting can never undercut --
singleton table, same convention as `oidc_settings`/`branding_settings`
(migrations 0027/0029): at most one row, enforced in application code.

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-20

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instance_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("min_report_retention_days", sa.Integer, nullable=False, server_default="365"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("instance_settings")
