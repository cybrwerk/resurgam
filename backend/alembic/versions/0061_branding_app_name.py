"""White-label app title: `branding_settings.app_name` (shown instead of
'RESURGAM' in the browser tab, top bar, login page and PDF report footer).

Revision ID: 0061
Revises: 0060
Create Date: 2026-10-11

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("branding_settings", sa.Column("app_name", sa.String(60), nullable=True))


def downgrade() -> None:
    op.drop_column("branding_settings", "app_name")
