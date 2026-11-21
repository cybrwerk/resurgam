"""CISO audit finding (2026-09-23): `AuditEvent.actor_user_id` is
`ON DELETE SET NULL`, so offboarding a user silently anonymized their
entire historical audit trail. Adds `actor_label`/`actor_is_breakglass`,
snapshotted at write time by a `before_insert` ORM listener (models/audit.py)
rather than threaded through every `_record_audit` call site -- both
nullable/defaulted, inert for existing rows (the immutability trigger
blocks backfilling them, same as `customer_id` before it).

Revision ID: 0045
Revises: 0044
Create Date: 2026-09-23

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audit_events", sa.Column("actor_label", sa.String(length=300), nullable=True))
    op.add_column(
        "audit_events", sa.Column("actor_is_breakglass", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.alter_column("audit_events", "actor_is_breakglass", server_default=None)


def downgrade() -> None:
    op.drop_column("audit_events", "actor_is_breakglass")
    op.drop_column("audit_events", "actor_label")
