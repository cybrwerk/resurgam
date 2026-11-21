"""Usage measurements (manifest §9.3) + widen snapshot size to bigint

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usage_measurements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "datastore_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("datastores.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("used_bytes", sa.BigInteger, nullable=False),
        sa.Column("available_bytes", sa.BigInteger, nullable=False),
        sa.Column("total_bytes", sa.BigInteger, nullable=False),
        sa.Column("logical_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("group_count_vm", sa.Integer, nullable=False, server_default="0"),
        sa.Column("group_count_ct", sa.Integer, nullable=False, server_default="0"),
        sa.Column("group_count_host", sa.Integer, nullable=False, server_default="0"),
        sa.Column("snapshot_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("oldest_snapshot_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("newest_snapshot_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_successful_gc_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_usage_measurements_datastore_id", "usage_measurements", ["datastore_id"])

    # A single VM/CT backup routinely exceeds INT4's ~2.1GB ceiling —
    # confirmed necessary against real PBS usage data during this
    # milestone's live verification pass (see docs/milestone-1-pbs-pve-api-assumptions.md).
    op.alter_column("backup_snapshots", "size_bytes", type_=sa.BigInteger, existing_type=sa.Integer)


def downgrade() -> None:
    op.alter_column("backup_snapshots", "size_bytes", type_=sa.Integer, existing_type=sa.BigInteger)
    op.drop_index("ix_usage_measurements_datastore_id", table_name="usage_measurements")
    op.drop_table("usage_measurements")
