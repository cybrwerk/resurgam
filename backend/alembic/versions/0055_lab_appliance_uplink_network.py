"""Recovery Lab probe appliance uplink: optional VLAN tag and IPv4 mode
(DHCP or a static range with per-run allocation) on `pve_clusters`, and the
`lab_uplink_ip_allocations` table that hands out one address per active lab
run (unique per cluster + address, so concurrent runs never collide).

Revision ID: 0055
Revises: 0054
Create Date: 2026-10-07

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pve_clusters", sa.Column("lab_appliance_uplink_vlan_tag", sa.Integer(), nullable=True))
    op.add_column(
        "pve_clusters",
        sa.Column("lab_appliance_uplink_ip_mode", sa.String(20), nullable=False, server_default="dhcp"),
    )
    op.add_column("pve_clusters", sa.Column("lab_appliance_uplink_ip_range_start", sa.String(45), nullable=True))
    op.add_column("pve_clusters", sa.Column("lab_appliance_uplink_ip_range_end", sa.String(45), nullable=True))
    op.add_column("pve_clusters", sa.Column("lab_appliance_uplink_ip_prefix_length", sa.Integer(), nullable=True))
    op.add_column("pve_clusters", sa.Column("lab_appliance_uplink_gateway", sa.String(45), nullable=True))
    op.add_column(
        "pve_clusters",
        sa.Column("lab_appliance_uplink_dns_servers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        "ck_pve_clusters_lab_uplink_vlan_tag",
        "pve_clusters",
        "lab_appliance_uplink_vlan_tag IS NULL OR lab_appliance_uplink_vlan_tag BETWEEN 1 AND 4094",
    )
    op.create_check_constraint(
        "ck_pve_clusters_lab_uplink_ip_mode",
        "pve_clusters",
        "lab_appliance_uplink_ip_mode IN ('dhcp', 'static_range')",
    )

    op.create_table(
        "lab_uplink_ip_allocations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pve_cluster_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pve_clusters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ip_address", sa.String(45), nullable=False),
        sa.Column("purpose", sa.String(20), nullable=False),
        sa.Column(
            "restore_test_plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("restore_test_plans.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("pve_cluster_id", "ip_address", name="uq_lab_uplink_ip_allocation_address"),
        sa.UniqueConstraint("restore_test_plan_id", name="uq_lab_uplink_ip_allocation_plan"),
    )


def downgrade() -> None:
    op.drop_table("lab_uplink_ip_allocations")
    op.drop_constraint("ck_pve_clusters_lab_uplink_ip_mode", "pve_clusters", type_="check")
    op.drop_constraint("ck_pve_clusters_lab_uplink_vlan_tag", "pve_clusters", type_="check")
    for column in (
        "lab_appliance_uplink_dns_servers",
        "lab_appliance_uplink_gateway",
        "lab_appliance_uplink_ip_prefix_length",
        "lab_appliance_uplink_ip_range_end",
        "lab_appliance_uplink_ip_range_start",
        "lab_appliance_uplink_ip_mode",
        "lab_appliance_uplink_vlan_tag",
    ):
        op.drop_column("pve_clusters", column)
