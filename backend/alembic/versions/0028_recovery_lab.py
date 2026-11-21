"""Recovery Lab, Phase A (manifest EPIC 10 / §22, ADR 0019): PVE clusters
gain a lab-compute flag and an optional admin-configured SDN zone name;
Application Groups (§22.4, customer-scoped, reusable, ordered VM/CT
membership with per-member check lists) and Recovery Lab runs
(`RestoreTestPlan`/`RestoreTestPlanMember`, one row per run and one per
group member within that run) are new tables. No backfill needed -- every
column/table here is new.

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pve_clusters", sa.Column("is_lab_compute", sa.Boolean, nullable=False, server_default=sa.false()))
    op.add_column("pve_clusters", sa.Column("lab_sdn_zone_name", sa.String(20), nullable=True))
    op.add_column("pve_clusters", sa.Column("lab_appliance_template_vmid", sa.Integer, nullable=True))
    op.add_column("pve_clusters", sa.Column("lab_agent_token_hash", sa.String(255), nullable=True))

    op.create_table(
        "application_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_application_groups_customer_id", "application_groups", ["customer_id"])

    op.create_table(
        "application_group_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "application_group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "backup_group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backup_groups.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("startup_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("check_types", postgresql.JSONB, nullable=False, server_default="[]"),
    )
    op.create_index("ix_application_group_members_group_id", "application_group_members", ["application_group_id"])

    op.create_table(
        "restore_test_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "application_group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_groups.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "pve_cluster_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pve_clusters.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("vnet_name", sa.String(20), nullable=False),
        sa.Column("appliance_vmid", sa.Integer, nullable=True),
        sa.Column("appliance_run_token_hash", sa.String(255), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="planned"),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_restore_test_plans_application_group_id", "restore_test_plans", ["application_group_id"])
    op.create_index("ix_restore_test_plans_pve_cluster_id", "restore_test_plans", ["pve_cluster_id"])
    op.create_index("ix_restore_test_plans_status", "restore_test_plans", ["status"])

    op.create_table(
        "restore_test_plan_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "restore_test_plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("restore_test_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "application_group_member_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_group_members.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "backup_snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backup_snapshots.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("target_vmid", sa.Integer, nullable=False),
        sa.Column("target_node", sa.String(255), nullable=False),
        sa.Column("target_storage", sa.String(255), nullable=False),
        sa.Column("upid", sa.String(255), nullable=True),
        sa.Column("member_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("probe_results", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
    )
    op.create_index("ix_restore_test_plan_members_plan_id", "restore_test_plan_members", ["restore_test_plan_id"])


def downgrade() -> None:
    op.drop_table("restore_test_plan_members")
    op.drop_table("restore_test_plans")
    op.drop_table("application_group_members")
    op.drop_table("application_groups")
    op.drop_column("pve_clusters", "lab_agent_token_hash")
    op.drop_column("pve_clusters", "lab_appliance_template_vmid")
    op.drop_column("pve_clusters", "lab_sdn_zone_name")
    op.drop_column("pve_clusters", "is_lab_compute")
