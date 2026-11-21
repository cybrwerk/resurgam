"""Disk Mounts (ADR 0025): the `disk_mounts` table, the share helper template
per cluster (`pve_clusters.disk_mount_helper_template_vmid`) and lab uplink
range addresses bound to a disk mount (`lab_uplink_ip_allocations.disk_mount_id`).

Revision ID: 0057
Revises: 0056
Create Date: 2026-10-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pve_clusters", sa.Column("disk_mount_helper_template_vmid", sa.Integer(), nullable=True))

    uuid_type = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "disk_mounts",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "backup_snapshot_id", uuid_type, sa.ForeignKey("backup_snapshots.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("backup_group_id", uuid_type, sa.ForeignKey("backup_groups.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("customer_id", uuid_type, sa.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("disk_archives", jsonb, nullable=False),
        sa.Column("pve_cluster_id", uuid_type, sa.ForeignKey("pve_clusters.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("target_node", sa.String(63), nullable=False),
        sa.Column("target_storage", sa.String(64), nullable=True),
        sa.Column("bridge", sa.String(64), nullable=False),
        sa.Column("vlan_tag", sa.Integer(), nullable=True),
        sa.Column("ip_mode", sa.String(20), nullable=False),
        sa.Column("ip_address_cidr", sa.String(64), nullable=True),
        sa.Column("gateway", sa.String(45), nullable=True),
        sa.Column("protocols", jsonb, nullable=False),
        sa.Column("allowed_clients", jsonb, nullable=False, server_default="[]"),
        sa.Column("smb_username", sa.String(32), nullable=True),
        sa.Column("share_password_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("lifetime_hours", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("teardown_reason", sa.String(20), nullable=True),
        sa.Column("created_by_user_id", uuid_type, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("approval_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("approved_by_user_id", uuid_type, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_notes", sa.Text(), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_progress_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "stop_requested_by_user_id", uuid_type, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("helper_vmid", sa.Integer(), nullable=True),
        sa.Column("mapped_devices", jsonb, nullable=False, server_default="[]"),
        sa.Column("snippet_volume", sa.String(255), nullable=True),
        sa.Column("helper_addresses", jsonb, nullable=False, server_default="[]"),
        sa.Column("volumes", jsonb, nullable=False, server_default="[]"),
        sa.Column("teardown_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_teardown_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending_approval', 'queued', 'provisioning', 'ready', 'stopping', 'stopped', 'expired', "
            "'failed', 'rejected')",
            name="ck_disk_mounts_status",
        ),
        sa.CheckConstraint("ip_mode IN ('dhcp', 'static', 'cluster_range')", name="ck_disk_mounts_ip_mode"),
        sa.CheckConstraint("vlan_tag IS NULL OR vlan_tag BETWEEN 1 AND 4094", name="ck_disk_mounts_vlan_tag"),
    )
    op.create_index("ix_disk_mounts_status", "disk_mounts", ["status"])
    op.create_index("ix_disk_mounts_customer_id", "disk_mounts", ["customer_id"])

    op.add_column(
        "lab_uplink_ip_allocations",
        sa.Column("disk_mount_id", uuid_type, sa.ForeignKey("disk_mounts.id", ondelete="CASCADE"), nullable=True),
    )
    op.create_unique_constraint(
        "uq_lab_uplink_ip_allocation_disk_mount", "lab_uplink_ip_allocations", ["disk_mount_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_lab_uplink_ip_allocation_disk_mount", "lab_uplink_ip_allocations", type_="unique")
    op.drop_column("lab_uplink_ip_allocations", "disk_mount_id")
    op.drop_index("ix_disk_mounts_customer_id", table_name="disk_mounts")
    op.drop_index("ix_disk_mounts_status", table_name="disk_mounts")
    op.drop_table("disk_mounts")
    op.drop_column("pve_clusters", "disk_mount_helper_template_vmid")
