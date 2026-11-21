"""PBS instance and PVE cluster connection management

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-12

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "local_secret_values",
        sa.Column(
            "reference_key",
            sa.String(255),
            sa.ForeignKey("secret_references.reference_key", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("ciphertext", sa.LargeBinary, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "pbs_instances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("location", sa.String(200), nullable=True),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer, nullable=False, server_default="8007"),
        sa.Column("tls_trust_mode", sa.String(20), nullable=False),
        sa.Column("tls_fingerprint_sha256", sa.String(64), nullable=True),
        sa.Column(
            "api_token_secret_ref_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("secret_references.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.String(100), nullable=True),
        sa.Column("capabilities", postgresql.JSONB, nullable=True),
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_health_status", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("disabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "pve_clusters",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer, nullable=False, server_default="8006"),
        sa.Column("tls_trust_mode", sa.String(20), nullable=False),
        sa.Column("tls_fingerprint_sha256", sa.String(64), nullable=True),
        sa.Column(
            "api_token_secret_ref_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("secret_references.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("allowed_target_storages", postgresql.JSONB, nullable=True),
        sa.Column("restore_network_rules", postgresql.JSONB, nullable=True),
        sa.Column("version", sa.String(100), nullable=True),
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_health_status", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("disabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "pve_cluster_pbs_instances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pve_cluster_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pve_clusters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "pbs_instance_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pbs_instances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.UniqueConstraint("pve_cluster_id", "pbs_instance_id", name="uq_pve_cluster_pbs_instance"),
    )


def downgrade() -> None:
    op.drop_table("pve_cluster_pbs_instances")
    op.drop_table("pve_clusters")
    op.drop_table("pbs_instances")
    op.drop_table("local_secret_values")
