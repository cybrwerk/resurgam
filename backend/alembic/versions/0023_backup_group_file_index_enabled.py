"""File Indexing (manifest §13, ADR 0016) -- per-VM opt-in, raised
directly by the user in place of the customer-wide-only switch. A backup
group is actually indexed only when this is true *and* its effective
customer's customer_file_index_settings.enabled is also true.

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-17

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "backup_groups",
        sa.Column("file_index_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("backup_groups", "file_index_enabled")
