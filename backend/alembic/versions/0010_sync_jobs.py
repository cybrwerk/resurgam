"""Sync jobs (manifest §8's sync_jobs entity)

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sync_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pbs_instance_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pbs_instances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("job_id", sa.String(255), nullable=False),
        sa.Column("remote_name", sa.String(255), nullable=False),
        sa.Column("remote_store", sa.String(200), nullable=False),
        sa.Column("local_store_name", sa.String(200), nullable=False),
        sa.Column(
            "local_datastore_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("datastores.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("schedule", sa.String(255), nullable=True),
        sa.Column("remove_vanished", sa.Boolean, nullable=True),
        sa.Column("comment", sa.String(500), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("pbs_instance_id", "job_id", name="uq_sync_job_identity"),
    )


def downgrade() -> None:
    op.drop_table("sync_jobs")
