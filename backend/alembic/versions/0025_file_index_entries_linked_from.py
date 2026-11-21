"""File Indexing (manifest §13, ADR 0016) -- "reuse and link", raised
directly by the user after watching a real VM re-index 250k+ largely
unchanged entries on every snapshot. When a newly discovered entry's
(type, size_bytes, mtime) exactly matches the most recently indexed
entry at the same path for the same group, the worker now links the new
row to that source instead of duplicating its `size_bytes`/`mtime` --
`linked_from_entry_id` always points at a *root* (never-linked) entry,
one hop, never chained.

`ON DELETE RESTRICT`, deliberately different from every other FK on this
table (which CASCADE, since an entry is normally pure derived data safe
to drop with its snapshot/group/customer) -- a row other rows still link
to for their own data is *not* safe to drop silently. Both purge paths
that can delete `file_index_entries` (the worker's age-based retention
sweep, and the collector's orphaned-snapshot cleanup) are updated
alongside this migration to skip any row still referenced by a newer
link, so RESTRICT should only ever fire as a backstop against a bug in
that skip logic, never in normal operation.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "file_index_entries",
        sa.Column("linked_from_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_file_index_entries_linked_from_entry_id",
        "file_index_entries",
        "file_index_entries",
        ["linked_from_entry_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_file_index_entries_linked_from",
        "file_index_entries",
        ["linked_from_entry_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_file_index_entries_linked_from", table_name="file_index_entries")
    op.drop_constraint("fk_file_index_entries_linked_from_entry_id", "file_index_entries", type_="foreignkey")
    op.drop_column("file_index_entries", "linked_from_entry_id")
