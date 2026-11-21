"""File Indexing (manifest §13) -- Explorer-style directory browsing over
the persistent index (`GET /file-index/browse`), raised directly by the
user after the feature shipped. Adds a `text_pattern_ops` index on
`(backup_snapshot_id, path)` so the new one-level `path LIKE 'prefix%'`
query the browse endpoint runs is index-backed -- a plain btree on `path`
doesn't support prefix-`LIKE` index scans under a non-C collation (this
database's default), only an explicit `text_pattern_ops` index does.

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-17

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_file_index_entries_snapshot_path_pattern "
        "ON file_index_entries (backup_snapshot_id, path text_pattern_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_file_index_entries_snapshot_path_pattern")
