"""Per-customer report retention override -- null means "use the
platform-wide floor" (`instance_settings.min_report_retention_days`).
Lives directly on `customers` (not `customer_file_index_settings`,
where an earlier draft of this feature put it) because report
retention has nothing to do with file indexing and must be settable
for every customer, including ones who never opted into file indexing.

Revision ID: 0034
Revises: 0033
Create Date: 2026-09-20

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("customers", sa.Column("report_retention_days", sa.Integer, nullable=True))


def downgrade() -> None:
    op.drop_column("customers", "report_retention_days")
