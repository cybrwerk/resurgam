"""Billing foundations: datastore role, per-group customer override, pricing plans

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "datastore_customer_assignments",
        sa.Column("role", sa.String(20), nullable=False, server_default="primary"),
    )

    op.add_column(
        "backup_groups",
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    op.create_table(
        "pricing_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("price_per_vm_month", sa.Numeric(10, 2), nullable=False),
        sa.Column("price_per_gb_month", sa.Numeric(10, 4), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="CHF"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    # Partial unique indexes, not a plain UNIQUE constraint: standard SQL
    # treats every NULL as distinct, so a plain unique constraint on
    # customer_id would happily allow multiple global (NULL) plans.
    op.create_index(
        "uq_pricing_plans_customer",
        "pricing_plans",
        ["customer_id"],
        unique=True,
        postgresql_where=sa.text("customer_id IS NOT NULL"),
    )
    op.create_index(
        "uq_pricing_plans_global",
        "pricing_plans",
        [sa.text("(true)")],
        unique=True,
        postgresql_where=sa.text("customer_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_pricing_plans_global", table_name="pricing_plans")
    op.drop_index("uq_pricing_plans_customer", table_name="pricing_plans")
    op.drop_table("pricing_plans")
    op.drop_column("backup_groups", "customer_id")
    op.drop_column("datastore_customer_assignments", "role")
