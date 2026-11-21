"""Guest-exec File Indexing (ADR 0021) follow-on: the target node/storage
a "guest_exec_ephemeral" job's disposable clone boots on -- job creation
(manual/bulk/scheduled) has no per-job place to ask for these the way
Recovery Lab's own plan-creation request does, so they're a per-customer
default alongside the cluster field, same "operator sets it once"
precedent.

Revision ID: 0042
Revises: 0041
Create Date: 2026-09-22

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "customer_file_index_settings", sa.Column("guest_exec_target_node", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "customer_file_index_settings", sa.Column("guest_exec_target_storage", sa.String(length=255), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("customer_file_index_settings", "guest_exec_target_storage")
    op.drop_column("customer_file_index_settings", "guest_exec_target_node")
