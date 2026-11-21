"""Recovery Evidence compliance reporting -- customer-scoped audit trail,
raised directly by the user asking what ISO27001/SOC2/ISO9001 evidence a
Backup-as-a-Service customer would need. `audit_events` has no customer
link today, only a generic `target_type`/`target_id` pair -- this adds a
denormalized `customer_id`, threaded through at write time (not
backfilled: the table's own immutability trigger, `0001`'s
`resurgam_audit_events_immutable()`, unconditionally rejects UPDATE, so
pre-deployment rows stay NULL and are excluded from customer-scoped
reports rather than silently pretending completeness).

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_events",
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_audit_events_customer_id",
        "audit_events",
        "customers",
        ["customer_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_audit_events_customer_id", "audit_events", ["customer_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_customer_id", table_name="audit_events")
    op.drop_constraint("fk_audit_events_customer_id", "audit_events", type_="foreignkey")
    op.drop_column("audit_events", "customer_id")
