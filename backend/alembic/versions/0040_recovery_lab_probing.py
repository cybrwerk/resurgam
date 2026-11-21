"""Recovery Lab Phase B: the 3-tier probe hierarchy's supporting config.

Adds the probe appliance's own router/DHCP settings to `ApplicationGroup`
(the appliance itself acts as gateway/DHCP/firewall/SNAT for its run's
isolated Lab VNet -- PVE never provisions a subnet/gateway/DHCP/SNAT
itself, a direct user correction to an earlier draft of this design),
a per-member static IP override on `ApplicationGroupMember`, and two
operator-provisioned prerequisites on `PveCluster` (a snippet storage for
per-run cloud-init config, and the appliance's uplink bridge). All
columns nullable -- inert until an operator opts a cluster/group in.

Revision ID: 0040
Revises: 0039
Create Date: 2026-09-21

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "application_groups",
        sa.Column("lab_network_mode", sa.String(length=10), nullable=False, server_default="dhcp"),
    )
    op.alter_column("application_groups", "lab_network_mode", server_default=None)
    op.add_column("application_groups", sa.Column("lab_subnet_cidr", sa.String(length=43), nullable=True))
    op.add_column("application_groups", sa.Column("lab_gateway_ip", sa.String(length=45), nullable=True))
    op.add_column("application_groups", sa.Column("lab_dns_ip", sa.String(length=45), nullable=True))
    op.add_column("application_groups", sa.Column("lab_dhcp_range_start", sa.String(length=45), nullable=True))
    op.add_column("application_groups", sa.Column("lab_dhcp_range_end", sa.String(length=45), nullable=True))

    op.add_column("application_group_members", sa.Column("expected_ip", sa.String(length=45), nullable=True))

    op.add_column("pve_clusters", sa.Column("lab_snippet_storage_id", sa.String(length=255), nullable=True))
    op.add_column("pve_clusters", sa.Column("lab_appliance_uplink_bridge", sa.String(length=64), nullable=True))

    # No REST endpoint exists to upload a PVE snippet file (confirmed live,
    # 2026-09-21 -- the upload API's `content` param hard-rejects
    # "snippets"), so `resurgam-lab-agent` writes it locally instead; the
    # backend queues the content here until the agent confirms the write.
    op.add_column("restore_test_plans", sa.Column("appliance_snippet_pending", postgresql.JSONB(), nullable=True))
    op.add_column("restore_test_plan_members", sa.Column("resolved_ip", sa.String(length=45), nullable=True))

    # "provisioning_appliance" (22 chars) doesn't fit the original
    # String(20) -- found live while testing this migration's own
    # feature.
    op.alter_column("restore_test_plans", "status", type_=sa.String(length=30))


def downgrade() -> None:
    op.alter_column("restore_test_plans", "status", type_=sa.String(length=20))
    op.drop_column("restore_test_plan_members", "resolved_ip")
    op.drop_column("restore_test_plans", "appliance_snippet_pending")

    op.drop_column("pve_clusters", "lab_appliance_uplink_bridge")
    op.drop_column("pve_clusters", "lab_snippet_storage_id")

    op.drop_column("application_group_members", "expected_ip")

    op.drop_column("application_groups", "lab_dhcp_range_end")
    op.drop_column("application_groups", "lab_dhcp_range_start")
    op.drop_column("application_groups", "lab_dns_ip")
    op.drop_column("application_groups", "lab_gateway_ip")
    op.drop_column("application_groups", "lab_subnet_cidr")
    op.drop_column("application_groups", "lab_network_mode")
