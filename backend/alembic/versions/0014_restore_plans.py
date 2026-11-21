"""Restore plans (manifest EPIC 8, §11.1 "MVP: geführter Restore") -- a
reviewed, traceable restore plan plus a generated qmrestore/pct restore
command. Nothing executes; execution belongs to a later EPIC 9 migration.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "restore_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "backup_snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backup_snapshots.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "backup_group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backup_groups.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "pve_cluster_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pve_clusters.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("target_node", sa.String(255), nullable=False),
        sa.Column("target_storage", sa.String(255), nullable=False),
        sa.Column("vmid_intent", sa.String(20), nullable=False),
        sa.Column("target_vmid", sa.Integer, nullable=False),
        sa.Column("temp_name", sa.String(255), nullable=False),
        sa.Column("network_mode", sa.String(20), nullable=False),
        sa.Column("network_bridge", sa.String(100), nullable=True),
        sa.Column("start_after_restore", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("network_danger_acknowledged", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("checks_result", postgresql.JSONB, nullable=False),
        sa.Column("checks_performed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_command", sa.Text, nullable=False),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("restore_plans")
