"""Per-customer on/off toggle for individual 3-2-1-1-0 legs -- raised
directly by the user, not every leg is contractually relevant to every
customer (e.g. a deliberately single-site customer has no "offsite"
obligation). Absence of a row means "applicable" (the honest default).
Ordinary mutable settings row, not append-only -- this is a scoping
decision the operator makes, not compliance evidence the customer
vouches for (see `ComplianceAttestation` for that).

Revision ID: 0031
Revises: 0030
Create Date: 2026-09-20

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "three_two_one_one_zero_leg_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("leg", sa.String(20), nullable=False),
        sa.Column("applicable", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "updated_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("customer_id", "leg", name="uq_321_leg_setting_customer_leg"),
    )


def downgrade() -> None:
    op.drop_table("three_two_one_one_zero_leg_settings")
