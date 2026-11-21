"""File Indexing (manifest §13, reopened into scope) -- job orchestration
table. One row per (group, snapshot) index attempt; see
docs/adr/0016-persistent-file-indexing.md for the full design.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "file_index_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("trigger", sa.String(20), nullable=False),
        sa.Column(
            "backup_group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backup_groups.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "backup_snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backup_snapshots.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("entries_indexed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("current_path", sa.String(4096), nullable=True),
        sa.Column("pending_paths", postgresql.JSONB, nullable=True),
        sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_file_index_jobs_status_created_at", "file_index_jobs", ["status", "created_at"])
    op.create_index("ix_file_index_jobs_snapshot_status", "file_index_jobs", ["backup_snapshot_id", "status"])
    op.create_index("ix_file_index_jobs_group_created_at", "file_index_jobs", ["backup_group_id", "created_at"])
    op.create_index("ix_file_index_jobs_heartbeat", "file_index_jobs", ["status", "last_heartbeat_at"])


def downgrade() -> None:
    op.drop_index("ix_file_index_jobs_heartbeat", table_name="file_index_jobs")
    op.drop_index("ix_file_index_jobs_group_created_at", table_name="file_index_jobs")
    op.drop_index("ix_file_index_jobs_snapshot_status", table_name="file_index_jobs")
    op.drop_index("ix_file_index_jobs_status_created_at", table_name="file_index_jobs")
    op.drop_table("file_index_jobs")
