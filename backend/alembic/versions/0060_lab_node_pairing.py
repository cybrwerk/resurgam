"""Lab node pairing and agent-run template builds (docs/adr/0027):
`lab_node_pairings` (one-time codes, hashed), `lab_agent_setup_jobs`, and
`pve_clusters.lab_agent_node`.

Revision ID: 0060
Revises: 0059
Create Date: 2026-10-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)
_JSONB = postgresql.JSONB(astext_type=sa.Text())


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "pve_cluster_id", _UUID, sa.ForeignKey("pve_clusters.id", ondelete="CASCADE"), nullable=False, index=True
        ),
    ]


def upgrade() -> None:
    op.add_column("pve_clusters", sa.Column("lab_agent_node", sa.String(64), nullable=True))
    op.create_table(
        "lab_node_pairings",
        *_base_columns(),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("node", sa.String(64), nullable=False),
        sa.Column("overlay_storage_id", sa.String(64), nullable=False),
        sa.Column("overlay_dir", sa.String(255), nullable=False),
        sa.Column("script", sa.Text(), nullable=False),
        sa.Column("script_sha256", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pbs_token_provided", sa.Boolean(), nullable=True),
        sa.Column("created_by_user_id", _UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_table(
        "lab_agent_setup_jobs",
        *_base_columns(),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("node", sa.String(64), nullable=False),
        sa.Column("spec", _JSONB, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("steps", _JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("result", _JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("uplink_address", sa.String(45), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", _UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.CheckConstraint(
            "kind IN ('template_build', 'share_helper_template_build')", name="ck_lab_agent_setup_jobs_kind"
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name="ck_lab_agent_setup_jobs_status"
        ),
    )


def downgrade() -> None:
    op.drop_table("lab_agent_setup_jobs")
    op.drop_table("lab_node_pairings")
    op.drop_column("pve_clusters", "lab_agent_node")
