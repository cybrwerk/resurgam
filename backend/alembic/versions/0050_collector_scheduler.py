"""Collector scheduler (docs/adr/0006): a configurable poll interval on
the platform-wide `instance_settings` singleton (defaulting to 15
minutes, editable in System Settings), and a `last_collector_sync_at`
timestamp on `pbs_instances` -- set by both the manual "Sync" button and
the new scheduled pass, giving the frontend a staleness signal to show
next to each instance's data.

Revision ID: 0050
Revises: 0049
Create Date: 2026-09-27

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "instance_settings",
        sa.Column("collector_poll_interval_minutes", sa.Integer, nullable=False, server_default="15"),
    )
    op.add_column(
        "pbs_instances",
        sa.Column("last_collector_sync_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pbs_instances", "last_collector_sync_at")
    op.drop_column("instance_settings", "collector_poll_interval_minutes")
