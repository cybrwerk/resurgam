"""File Indexing (manifest §13, ADR 0016) -- per-VM regex exclusion
patterns, raised directly by the user after watching a real VM's index
job spend most of its time walking an irrelevant kernel-headers tree
(`/usr/src/...`). Matched against each discovered entry's path during the
worker's walk (VM) or as a post-filter over the bulk catalog dump
(CT/host); a match skips the entry (and, for VM, its whole subtree)
entirely -- never inserted, never descended into.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "backup_groups",
        sa.Column(
            "file_index_exclude_patterns",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("backup_groups", "file_index_exclude_patterns")
