"""EPIC 9 (manifest §11.2): automated restore orchestration -- execution
state and four-eyes approval columns on restore_plans, plus a per-cluster
approval-required toggle on pve_clusters.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pve_clusters", sa.Column("require_restore_approval", sa.Boolean, nullable=False, server_default=sa.false())
    )

    op.add_column("restore_plans", sa.Column("execution_status", sa.String(30), nullable=True))
    op.add_column("restore_plans", sa.Column("upid", sa.String(255), nullable=True))
    op.add_column("restore_plans", sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "restore_plans",
        sa.Column(
            "execution_started_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("restore_plans", sa.Column("execution_exit_status", sa.String(255), nullable=True))
    op.add_column("restore_plans", sa.Column("execution_completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("restore_plans", sa.Column("execution_error", sa.Text, nullable=True))

    op.add_column("restore_plans", sa.Column("approval_status", sa.String(20), nullable=True))
    op.add_column(
        "restore_plans",
        sa.Column(
            "approved_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("restore_plans", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("restore_plans", sa.Column("approval_notes", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("restore_plans", "approval_notes")
    op.drop_column("restore_plans", "approved_at")
    op.drop_column("restore_plans", "approved_by_user_id")
    op.drop_column("restore_plans", "approval_status")

    op.drop_column("restore_plans", "execution_error")
    op.drop_column("restore_plans", "execution_completed_at")
    op.drop_column("restore_plans", "execution_exit_status")
    op.drop_column("restore_plans", "execution_started_by_user_id")
    op.drop_column("restore_plans", "executed_at")
    op.drop_column("restore_plans", "upid")
    op.drop_column("restore_plans", "execution_status")

    op.drop_column("pve_clusters", "require_restore_approval")
