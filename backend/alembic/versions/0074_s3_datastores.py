"""S3-backed PBS datastores (ADR 0033): each datastore's backend type and S3
bucket, the PBS S3 endpoints with an audited "on-site" mark, and per
measurement the bucket's data size and the S3 request/traffic counters.

Revision ID: 0074
Revises: 0073
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0074"
down_revision: str | None = "0073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_S3_COUNTERS = (
    "s3_get_requests",
    "s3_put_requests",
    "s3_post_requests",
    "s3_head_requests",
    "s3_delete_requests",
    "s3_uploaded_bytes",
    "s3_downloaded_bytes",
)


def upgrade() -> None:
    op.add_column("datastores", sa.Column("backend_type", sa.String(20), nullable=True))
    op.add_column("datastores", sa.Column("s3_endpoint_name", sa.String(255), nullable=True))
    op.add_column("datastores", sa.Column("s3_bucket", sa.String(255), nullable=True))

    op.create_table(
        "pbs_s3_endpoints",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pbs_instance_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pbs_instances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("endpoint", sa.String(500), nullable=False),
        sa.Column("port", sa.Integer, nullable=True),
        sa.Column("region", sa.String(100), nullable=True),
        sa.Column("path_style", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("on_site", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("on_site_reason", sa.Text, nullable=True),
        sa.Column("on_site_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "on_site_changed_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("removed_from_pbs_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("pbs_instance_id", "name", name="uq_pbs_s3_endpoint_name"),
    )

    op.add_column("usage_measurements", sa.Column("s3_data_bytes", sa.BigInteger, nullable=True))
    for column in _S3_COUNTERS:
        op.add_column("usage_measurements", sa.Column(column, sa.BigInteger, nullable=True))


def downgrade() -> None:
    for column in reversed(_S3_COUNTERS):
        op.drop_column("usage_measurements", column)
    op.drop_column("usage_measurements", "s3_data_bytes")
    op.drop_table("pbs_s3_endpoints")
    op.drop_column("datastores", "s3_bucket")
    op.drop_column("datastores", "s3_endpoint_name")
    op.drop_column("datastores", "backend_type")
