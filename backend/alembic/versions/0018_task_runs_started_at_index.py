"""Jobs page enhancements: index task_runs for the new cross-instance,
date-filterable tasks query -- the (pbs_instance_id, started_at)
combination is now filtered and ordered on at real scale (1472+ rows on
one instance alone, now unioned across several).

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-17

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_task_runs_pbs_instance_id_started_at", "task_runs", ["pbs_instance_id", "started_at"])


def downgrade() -> None:
    op.drop_index("ix_task_runs_pbs_instance_id_started_at", table_name="task_runs")
