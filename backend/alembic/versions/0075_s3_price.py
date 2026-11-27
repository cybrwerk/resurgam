"""Pricing plans: an own, optional monthly price per GB for data in S3-backed
datastores (ADR 0033). Null = S3 data is not charged per GB (e.g. the S3
provider is paid some other way).

Revision ID: 0075
Revises: 0074
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0075"
down_revision: str | None = "0074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pricing_plans", sa.Column("price_per_s3_gb_month", sa.Numeric(10, 4), nullable=True))


def downgrade() -> None:
    op.drop_column("pricing_plans", "price_per_s3_gb_month")
