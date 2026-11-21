"""Store each PBS server's own timezone, so scheduled File Indexing can
pick the snapshot PBS prune (`keep-daily`, counted in the server's local
time) will keep, instead of a short-lived intra-day one (ADR 0016).

Revision ID: 0069
Revises: 0068
Create Date: 2026-11-14

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0069"
down_revision: str | None = "0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pbs_instances", sa.Column("server_timezone", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("pbs_instances", "server_timezone")
