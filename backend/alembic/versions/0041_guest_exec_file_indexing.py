"""Guest-exec File Indexing (ADR 0021): two opt-in methods alongside
today's external-mount indexer -- "guest_exec_live" (a PVE hookscript
fires guest-exec against the already-running production VM at its own
real backup's completion) and "guest_exec_ephemeral" (boots a
disposable snapshot clone via Recovery Lab's zero-copy map/boot
machinery, indexes that, then destroys it). Both solve encrypted disks
(the guest has already decrypted everything) and enable true
incremental indexing (find -newermt / LastWriteTime filtering, run
inside the guest itself). All columns nullable or defaulted -- inert
until a group opts in.

Revision ID: 0041
Revises: 0040
Create Date: 2026-09-22

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "backup_groups",
        sa.Column("file_index_method", sa.String(length=20), nullable=False, server_default="external_mount"),
    )
    op.alter_column("backup_groups", "file_index_method", server_default=None)

    op.add_column(
        "customer_file_index_settings",
        sa.Column("guest_exec_pve_cluster_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_customer_file_index_settings_guest_exec_pve_cluster_id",
        "customer_file_index_settings",
        "pve_clusters",
        ["guest_exec_pve_cluster_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("customer_file_index_settings", sa.Column("full_reindex_every_n_runs", sa.Integer(), nullable=True))

    op.add_column(
        "file_index_jobs",
        sa.Column("file_index_method", sa.String(length=20), nullable=False, server_default="external_mount"),
    )
    op.alter_column("file_index_jobs", "file_index_method", server_default=None)
    op.add_column("file_index_jobs", sa.Column("guest_exec_target_vmid", sa.Integer(), nullable=True))
    op.add_column("file_index_jobs", sa.Column("guest_exec_target_node", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("file_index_jobs", "guest_exec_target_node")
    op.drop_column("file_index_jobs", "guest_exec_target_vmid")
    op.drop_column("file_index_jobs", "file_index_method")

    op.drop_column("customer_file_index_settings", "full_reindex_every_n_runs")
    op.drop_constraint(
        "fk_customer_file_index_settings_guest_exec_pve_cluster_id", "customer_file_index_settings", type_="foreignkey"
    )
    op.drop_column("customer_file_index_settings", "guest_exec_pve_cluster_id")

    op.drop_column("backup_groups", "file_index_method")
