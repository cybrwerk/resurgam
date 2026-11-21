"""File-restore content moves from Postgres to files on disk; folder/multi-selection ZIP jobs.

Drops `file_restore_job_content` (the bytes of a completed download, ADR 0013) -- content is now
written by the worker to `Settings.file_restore_output_dir` and streamed from there. Its only
users were the worker and the download endpoint. Rows still in it are throwaway by design
(TTL-only, default 10 minutes), so nothing is migrated: a download completed right before the
upgrade answers 410 `content_expired` and is simply started again.

New `file_restore_jobs` columns: `paths` (archive jobs' selection), `result_size_bytes`,
`result_storage_name`, `archive_report`, `progress`.

Revision ID: 0056
Revises: 0055
Create Date: 2026-10-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("file_restore_jobs", sa.Column("paths", postgresql.JSONB(), nullable=True))
    op.add_column("file_restore_jobs", sa.Column("result_size_bytes", sa.BigInteger(), nullable=True))
    op.add_column("file_restore_jobs", sa.Column("result_storage_name", sa.String(100), nullable=True))
    op.add_column("file_restore_jobs", sa.Column("archive_report", postgresql.JSONB(), nullable=True))
    op.add_column("file_restore_jobs", sa.Column("progress", postgresql.JSONB(), nullable=True))
    op.drop_table("file_restore_job_content")


def downgrade() -> None:
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
    op.drop_column("file_restore_jobs", "progress")
    op.drop_column("file_restore_jobs", "archive_report")
    op.drop_column("file_restore_jobs", "result_storage_name")
    op.drop_column("file_restore_jobs", "result_size_bytes")
    op.drop_column("file_restore_jobs", "paths")
