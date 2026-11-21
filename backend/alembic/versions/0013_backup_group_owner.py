"""BackupGroup.owner (not from the manifest): PBS's own reported owner
(e.g. "tenant-a@pbs"), confirmed live earlier but not previously stored.
Read-only hint surfaced next to the manual customer picker -- no
auto-assignment logic.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-14

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backup_groups", sa.Column("owner", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("backup_groups", "owner")
