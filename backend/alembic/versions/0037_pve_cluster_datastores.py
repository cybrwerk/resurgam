"""Datastore-scoped PVE cluster provenance links, for workload-name
resolution -- raised directly by the user: a PVE cluster should be
mappable to multiple datastores (e.g. multi-tenant hosting), so falling
back to a real guest name via a cluster's `list_guests()` must be scoped
per-datastore, not via the existing `pve_cluster_pbs_instances` table
(that's a restore-capability link at the PBS-instance level, and VMIDs
are only unique within one PVE cluster -- reusing it risks resolving the
wrong guest's name across tenants sharing one PBS instance).

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-20

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pve_cluster_datastores",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pve_cluster_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pve_clusters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "datastore_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("datastores.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint("pve_cluster_id", "datastore_id", name="uq_pve_cluster_datastore"),
    )


def downgrade() -> None:
    op.drop_table("pve_cluster_datastores")
