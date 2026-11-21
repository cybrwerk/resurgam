"""Guest-exec File Indexing (ADR 0021) follow-on: "guest_exec_live"'s
hookscript must run on whichever PVE cluster actually hosts a group's
*production* VM -- deliberately distinct from any lab-compute cluster
Recovery Lab uses for disposable clones. `BackupGroup` otherwise has no
PVE linkage at all (populated purely from PBS-side data by
`collector.sync`), so this is a new, guest_exec_live-only column, set
together with `file_index_method` via `set-file-index-method`.

Revision ID: 0043
Revises: 0042
Create Date: 2026-09-22

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "backup_groups",
        sa.Column("guest_exec_live_pve_cluster_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_backup_groups_guest_exec_live_pve_cluster_id",
        "backup_groups",
        "pve_clusters",
        ["guest_exec_live_pve_cluster_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_backup_groups_guest_exec_live_pve_cluster_id", "backup_groups", type_="foreignkey")
    op.drop_column("backup_groups", "guest_exec_live_pve_cluster_id")
