"""Guest-exec File Indexing (ADR 0021) follow-on: `FileIndexJob.
is_full_reindex`, decided once at claim time from the group's guest-exec
job history and `full_reindex_every_n_runs`. Governs the `since` cutoff
handed to the agent and whether entry-completion carries forward
unchanged rows from the prior run (skipped on a full run, which is what
actually reconciles deletions/renames an incremental pass can't see).

Revision ID: 0044
Revises: 0043
Create Date: 2026-09-22

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "file_index_jobs", sa.Column("is_full_reindex", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.alter_column("file_index_jobs", "is_full_reindex", server_default=None)


def downgrade() -> None:
    op.drop_column("file_index_jobs", "is_full_reindex")
