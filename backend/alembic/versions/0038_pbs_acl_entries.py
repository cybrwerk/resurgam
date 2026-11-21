"""Mirror of PBS's native ACL grants (GET /access/acl), per PBS instance
-- raised directly by the user after the Recovery Evidence report's
"Access Control" section was shown to only ever list RESURGAM's own role
assignments, missing real access granted natively in PBS (a real user
with Admin on a customer's whole datastore, invisible to RESURGAM). A
live mirror of PBS's current ACL state, re-synced by delete-then-
reinsert per instance, not an append-only history.

Revision ID: 0038
Revises: 0037
Create Date: 2026-09-20

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pbs_acl_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pbs_instance_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pbs_instances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.String(500), nullable=False),
        sa.Column("role", sa.String(100), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("subject_type", sa.String(20), nullable=False),
        sa.Column("propagate", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_pbs_acl_entries_instance", "pbs_acl_entries", ["pbs_instance_id"])


def downgrade() -> None:
    op.drop_index("ix_pbs_acl_entries_instance", table_name="pbs_acl_entries")
    op.drop_table("pbs_acl_entries")
