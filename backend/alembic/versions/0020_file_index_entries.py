"""File Indexing (manifest §13) -- the actual searchable index. Enables
`pg_trgm` -- the first Postgres extension this codebase installs beyond
whatever ships with the base image -- so filename search can support
substring ("contains") matches via a GIN trigram index, not just
exact/prefix via a plain btree.

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "file_index_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "file_index_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("file_index_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "backup_snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backup_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "backup_group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backup_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("archive", sa.String(255), nullable=False),
        sa.Column("path", sa.String(4096), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=True),
        sa.Column("mtime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("file_index_job_id", "path", name="uq_file_index_entry_job_path"),
    )
    op.create_index("ix_file_index_entries_snapshot", "file_index_entries", ["backup_snapshot_id"])
    op.create_index("ix_file_index_entries_scope", "file_index_entries", ["customer_id", "backup_group_id"])
    op.execute(
        "CREATE INDEX ix_file_index_entries_filename_trgm ON file_index_entries USING gin (filename gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_file_index_entries_filename_trgm")
    op.drop_index("ix_file_index_entries_scope", table_name="file_index_entries")
    op.drop_index("ix_file_index_entries_snapshot", table_name="file_index_entries")
    op.drop_table("file_index_entries")
    # Deliberately not dropping the pg_trgm extension itself on downgrade
    # -- it's a shared, harmless-if-unused database-level object, and
    # dropping it would fail loudly if any other future migration has
    # since come to depend on it too.
