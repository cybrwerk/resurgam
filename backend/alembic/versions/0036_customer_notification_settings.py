"""Per-customer notification recipients (report/alarm/billing) and their
on/off toggles -- raised directly by the user: a Report and an Alarms
receiver email per customer, plus Billing as a third category, each
independently enable-able. Own table, same "customer_id UNIQUE, row
absence = never configured" shape as `customer_file_index_settings`,
deliberately separate from it since notifications have nothing to do
with file indexing.

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-20

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customer_notification_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("report_notification_email", sa.String(255), nullable=True),
        sa.Column("report_notifications_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("alarm_notification_email", sa.String(255), nullable=True),
        sa.Column("alarm_notifications_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("billing_notification_email", sa.String(255), nullable=True),
        sa.Column("billing_notifications_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("customer_notification_settings")
