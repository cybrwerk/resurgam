"""Per-customer alert mailer: delivery options on
`customer_notification_settings` plus `customer_alarm_notification_states`
(which alarms a customer was already mailed about, so each alarm is sent
once -- and again only on escalation, reminder or resolution).

`alarm_notification_email` is widened to hold several comma-separated
recipients.

Revision ID: 0063
Revises: 0062
Create Date: 2026-11-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "customer_notification_settings",
        "alarm_notification_email",
        type_=sa.String(1024),
        existing_type=sa.String(255),
        existing_nullable=True,
    )
    op.add_column(
        "customer_notification_settings",
        sa.Column("alarm_min_severity", sa.String(16), nullable=False, server_default="warning"),
    )
    op.add_column(
        "customer_notification_settings",
        sa.Column("alarm_notify_resolved", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "customer_notification_settings",
        sa.Column("alarm_reminder_hours", sa.Integer(), nullable=True),
    )
    op.add_column(
        "customer_notification_settings",
        sa.Column("alarm_last_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "customer_notification_settings",
        sa.Column("alarm_last_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "customer_notification_settings",
        sa.Column("alarm_last_error", sa.Text(), nullable=True),
    )

    op.create_table(
        "customer_alarm_notification_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("alarm_type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("first_notified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("customer_id", "entity_type", "entity_id", name="uq_customer_alarm_notification_state"),
    )
    op.create_index(
        "ix_customer_alarm_notification_states_customer_id", "customer_alarm_notification_states", ["customer_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_customer_alarm_notification_states_customer_id", "customer_alarm_notification_states")
    op.drop_table("customer_alarm_notification_states")
    for column in (
        "alarm_last_error",
        "alarm_last_sent_at",
        "alarm_last_attempt_at",
        "alarm_reminder_hours",
        "alarm_notify_resolved",
        "alarm_min_severity",
    ):
        op.drop_column("customer_notification_settings", column)
    op.alter_column(
        "customer_notification_settings",
        "alarm_notification_email",
        type_=sa.String(255),
        existing_type=sa.String(1024),
        existing_nullable=True,
    )
