"""Global MFA requirement: `instance_settings.require_mfa_all_local_users`
(Users & Roles -> "Require MFA for all local users").

Revision ID: 0062
Revises: 0061
Create Date: 2026-10-11

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "instance_settings",
        sa.Column("require_mfa_all_local_users", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("instance_settings", "require_mfa_all_local_users")
