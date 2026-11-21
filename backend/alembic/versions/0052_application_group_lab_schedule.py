"""Recurring Lab runs: one cron-like schedule per Application Group,
mirroring `CustomerFileIndexSettings`' own schedule_enabled/
schedule_interval_hours/next_scheduled_run_at shape, plus the target
cluster/node/storage a scheduled run has no human present to choose.
Raised directly by the user ("the lab Scheduler (Cron Like is not
there)").

Revision ID: 0052
Revises: 0051
Create Date: 2026-09-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "application_groups", sa.Column("lab_schedule_enabled", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column("application_groups", sa.Column("lab_schedule_interval_hours", sa.Integer(), nullable=True))
    op.add_column(
        "application_groups", sa.Column("lab_next_scheduled_run_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "application_groups",
        sa.Column("lab_schedule_pve_cluster_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_application_groups_lab_schedule_pve_cluster_id",
        "application_groups",
        "pve_clusters",
        ["lab_schedule_pve_cluster_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("application_groups", sa.Column("lab_schedule_target_node", sa.String(length=255), nullable=True))
    op.add_column("application_groups", sa.Column("lab_schedule_target_storage", sa.String(length=255), nullable=True))
    op.alter_column("application_groups", "lab_schedule_enabled", server_default=None)


def downgrade() -> None:
    op.drop_constraint("fk_application_groups_lab_schedule_pve_cluster_id", "application_groups", type_="foreignkey")
    op.drop_column("application_groups", "lab_schedule_target_storage")
    op.drop_column("application_groups", "lab_schedule_target_node")
    op.drop_column("application_groups", "lab_schedule_pve_cluster_id")
    op.drop_column("application_groups", "lab_next_scheduled_run_at")
    op.drop_column("application_groups", "lab_schedule_interval_hours")
    op.drop_column("application_groups", "lab_schedule_enabled")
