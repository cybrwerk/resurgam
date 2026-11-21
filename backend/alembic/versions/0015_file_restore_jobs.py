"""File-restore async job queue (ADR 0002, ADR 0013) -- listing and
downloading a snapshot's files now run in a separate worker process
instead of blocking the API request; see
docs/adr/0013-file-restore-job-queue-and-worker-process.md.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "file_restore_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
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
        sa.Column("path", sa.String(4096), nullable=False),
        sa.Column("result", postgresql.JSONB, nullable=True),
        sa.Column("result_filename", sa.String(255), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_file_restore_jobs_status_created_at", "file_restore_jobs", ["status", "created_at"])

    op.create_table(
        "file_restore_job_content",
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("file_restore_jobs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("content", sa.LargeBinary, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("file_restore_job_content")
    op.drop_index("ix_file_restore_jobs_status_created_at", table_name="file_restore_jobs")
    op.drop_table("file_restore_jobs")
