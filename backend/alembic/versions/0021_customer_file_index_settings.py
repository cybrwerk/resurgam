"""File Indexing (manifest §13) -- per-customer opt-in, retention,
schedule and time-window configuration.

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customer_file_index_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("retention_days", sa.Integer, nullable=False),
        sa.Column("schedule_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("schedule_interval_hours", sa.Integer, nullable=True),
        sa.Column("window_start_time", sa.Time, nullable=True),
        sa.Column("window_end_time", sa.Time, nullable=True),
        sa.Column("next_scheduled_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("customer_file_index_settings")
