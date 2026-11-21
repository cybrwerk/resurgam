"""Track role assignment source (manual vs oidc)

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-13

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_role_assignments",
        sa.Column("source", sa.String(20), nullable=False, server_default="manual"),
    )


def downgrade() -> None:
    op.drop_column("user_role_assignments", "source")
