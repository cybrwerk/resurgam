"""Backup inventory and task history (Collector)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backup_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "datastore_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("datastores.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("namespace", sa.String(255), nullable=False, server_default=""),
        sa.Column("backup_type", sa.String(20), nullable=False),
        sa.Column("backup_id", sa.String(100), nullable=False),
        sa.Column("resolved_name", sa.String(255), nullable=True),
        sa.Column("last_backup_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verify_status", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("datastore_id", "namespace", "backup_type", "backup_id", name="uq_backup_group_identity"),
    )

    op.create_table(
        "backup_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "backup_group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backup_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snapshot_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verify_status", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("size_bytes", sa.Integer, nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("backup_group_id", "snapshot_time", name="uq_backup_snapshot_identity"),
    )

    op.create_table(
        "task_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pbs_instance_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pbs_instances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("upid", sa.String(255), nullable=False),
        sa.Column("task_type", sa.String(30), nullable=False),
        sa.Column("worker_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("task_user", sa.String(255), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("pbs_instance_id", "upid", name="uq_task_run_upid"),
    )


def downgrade() -> None:
    op.drop_table("task_runs")
    op.drop_table("backup_snapshots")
    op.drop_table("backup_groups")
