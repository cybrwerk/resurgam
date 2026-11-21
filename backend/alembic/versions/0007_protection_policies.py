"""Protection policies (manifest §9.5)

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "protection_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("expected_interval_hours", sa.Integer, nullable=False),
        sa.Column("max_time_since_last_backup_hours", sa.Integer, nullable=False),
        sa.Column("expected_verify_deadline_hours", sa.Integer, nullable=True),
        sa.Column("min_snapshot_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("sync_dr_required", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("customer_id", "name", name="uq_protection_policy_customer_name"),
    )

    op.add_column(
        "backup_groups",
        sa.Column(
            "protection_policy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("protection_policies.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("backup_groups", "protection_policy_id")
    op.drop_table("protection_policies")
