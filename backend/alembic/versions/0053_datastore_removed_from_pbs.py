"""Datastore removal detection: `removed_from_pbs_at` on `datastores`,
mirroring `backup_groups.removed_from_pbs_at` (0051). The Collector now
discovers datastores automatically on every sync (new ones are imported,
path/comment refreshed); one that PBS no longer lists is marked here
instead of being deleted, and un-marked if it reappears.

Revision ID: 0053
Revises: 0052
Create Date: 2026-10-06

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("datastores", sa.Column("removed_from_pbs_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("datastores", "removed_from_pbs_at")
