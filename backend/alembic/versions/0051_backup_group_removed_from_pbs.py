"""Backup group removal detection: `removed_from_pbs_at` on
`backup_groups`, distinct from `superseded_at` (renamed/replaced) --
this means the backup is simply gone from PBS with no successor. Raised
directly by the user after a whole namespace deleted on PBS kept
showing as live in RESURGAM for hours: the Collector was purely
additive and never noticed removal.

Revision ID: 0051
Revises: 0050
Create Date: 2026-09-28

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backup_groups", sa.Column("removed_from_pbs_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("backup_groups", "removed_from_pbs_at")
