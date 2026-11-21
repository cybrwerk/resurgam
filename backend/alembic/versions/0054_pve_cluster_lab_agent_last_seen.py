"""Lab Compute setup wizard: `lab_agent_last_seen_at` on `pve_clusters` --
when `resurgam-lab-agent` last authenticated with the cluster's token, so
`GET /pve-clusters/{id}/lab-readiness` can tell whether the agent is
installed and checking in.

Revision ID: 0054
Revises: 0053
Create Date: 2026-10-06

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pve_clusters", sa.Column("lab_agent_last_seen_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("pve_clusters", "lab_agent_last_seen_at")
