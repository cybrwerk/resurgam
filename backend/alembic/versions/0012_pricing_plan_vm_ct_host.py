"""Split PricingPlan's per-unit rate into VM/CT/Host (not from the
manifest, requested directly). Existing rows get 0 for the two new
rates, not the existing VM rate -- silently charging for a previously
-free unit type would be a real billing surprise.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-14

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pricing_plans",
        sa.Column("price_per_ct_month", sa.Numeric(10, 2), nullable=False, server_default="0"),
    )
    op.add_column(
        "pricing_plans",
        sa.Column("price_per_host_month", sa.Numeric(10, 2), nullable=False, server_default="0"),
    )
    op.alter_column("pricing_plans", "price_per_ct_month", server_default=None)
    op.alter_column("pricing_plans", "price_per_host_month", server_default=None)


def downgrade() -> None:
    op.drop_column("pricing_plans", "price_per_host_month")
    op.drop_column("pricing_plans", "price_per_ct_month")
