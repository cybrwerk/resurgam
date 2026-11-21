"""PVE backup job customer-scope override (docs/adr/0023-backup-and-
maintenance-job-administration): a vzdump job's default, in-design
customer scope is inherited from its target datastore/namespace (a
`Datastore` is customer-scoped per this project's design), but a job's
real targeting doesn't always line up with that (`all_guests` spanning
multiple customers' storage, or a job intentionally targeting a
different customer's VMs). This table lets an operator set an explicit
override per job. A vzdump job has no RESURGAM-owned row of its own --
it lives entirely in PVE's own config, fetched live -- so the job is
identified by `(pve_cluster_id, job_id)`, not a RESURGAM UUID FK.
Mirrors `datastore_customer_assignments`' shape exactly.

Revision ID: 0049
Revises: 0048
Create Date: 2026-09-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pve_backup_job_customer_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("pve_cluster_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", sa.String(length=100), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["pve_cluster_id"], ["pve_clusters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("pve_cluster_id", "job_id", name="uq_pve_backup_job_customer_assignment"),
    )


def downgrade() -> None:
    op.drop_table("pve_backup_job_customer_assignments")
