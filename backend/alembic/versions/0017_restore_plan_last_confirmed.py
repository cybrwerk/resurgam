"""EPIC 9 execution-polling fix: track when a plan's execution was last
successfully confirmed against PVE, so the staleness safety net anchors
on "how long have we been unable to reach PVE", not "how long has it
been running" -- a legitimately long-running restore that PVE keeps
confirming is real must never be force-failed by an arbitrary clock.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-17

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("restore_plans", sa.Column("execution_last_confirmed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("restore_plans", "execution_last_confirmed_at")
