"""Add debitor_number and match_code to customers

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-13

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("debitor_number", sa.String(50), nullable=False))
    op.add_column("customers", sa.Column("match_code", sa.String(3), nullable=False))
    op.create_unique_constraint("uq_customers_debitor_number", "customers", ["debitor_number"])
    op.create_unique_constraint("uq_customers_match_code", "customers", ["match_code"])


def downgrade() -> None:
    op.drop_constraint("uq_customers_match_code", "customers", type_="unique")
    op.drop_constraint("uq_customers_debitor_number", "customers", type_="unique")
    op.drop_column("customers", "match_code")
    op.drop_column("customers", "debitor_number")
