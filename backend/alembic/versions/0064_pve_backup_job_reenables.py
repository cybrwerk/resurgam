"""Timed automatic re-enable for disabled PVE backup jobs: one pending
row per (cluster, job) in `pve_backup_job_reenables` ("disable for N
hours", ADR 0018 addendum 2026-11-11).

Revision ID: 0064
Revises: 0063
Create Date: 2026-11-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pve_backup_job_reenables",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pve_cluster_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pve_clusters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("job_id", sa.String(100), nullable=False),
        sa.Column("reenable_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "requested_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("pve_cluster_id", "job_id", name="uq_pve_backup_job_reenable"),
    )
    op.create_index("ix_pve_backup_job_reenables_reenable_at", "pve_backup_job_reenables", ["reenable_at"])


def downgrade() -> None:
    op.drop_index("ix_pve_backup_job_reenables_reenable_at", table_name="pve_backup_job_reenables")
    op.drop_table("pve_backup_job_reenables")
