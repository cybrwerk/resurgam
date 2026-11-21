"""Mandatory MFA per user: `users.mfa_required` -- set by a platform_admin,
enforced at the user's next local login (forced enrollment when no second
factor exists yet).

Revision ID: 0059
Revises: 0058
Create Date: 2026-10-11

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("mfa_required", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("users", "mfa_required")
