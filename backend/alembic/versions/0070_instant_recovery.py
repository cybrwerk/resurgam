"""Instant Recovery (ADR 0030): `restore_plans.method` plus the state the lab
agent reports while a VM runs straight from its backup, and the per-cluster
switch and alarm threshold.

Revision ID: 0070
Revises: 0069
Create Date: 2026-11-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0070"
down_revision: str | None = "0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON_LISTS = (
    "ir_mapped_devices",
    "ir_overlay_volumes",
    "ir_imported_volumes",
    "ir_skipped_disks",
    "ir_move_disks",
)
_TIMESTAMPS = (
    "ir_claimed_at",
    "ir_last_progress_at",
    "ir_running_since",
    "ir_last_heartbeat_at",
    "ir_move_requested_at",
    "ir_discard_requested_at",
    "ir_next_attempt_at",
    "ir_ended_at",
)


def upgrade() -> None:
    op.add_column(
        "pve_clusters",
        sa.Column("instant_recovery_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "pve_clusters",
        sa.Column("instant_recovery_alarm_hours", sa.Integer(), nullable=False, server_default="24"),
    )
    op.create_check_constraint(
        "ck_pve_clusters_instant_recovery_alarm_hours",
        "pve_clusters",
        "instant_recovery_alarm_hours BETWEEN 1 AND 720",
    )

    op.add_column("restore_plans", sa.Column("method", sa.String(length=20), nullable=False, server_default="full"))
    op.create_check_constraint("ck_restore_plans_method", "restore_plans", "method IN ('full', 'instant')")
    op.add_column("restore_plans", sa.Column("ir_original_vm", postgresql.JSONB(), nullable=True))
    op.add_column("restore_plans", sa.Column("ir_keep_mac", sa.Boolean(), nullable=False, server_default=sa.false()))
    for name in _JSON_LISTS:
        op.add_column(
            "restore_plans",
            sa.Column(name, postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        )
    op.add_column("restore_plans", sa.Column("ir_vm_created", sa.Boolean(), nullable=False, server_default=sa.false()))
    for name in _TIMESTAMPS:
        op.add_column("restore_plans", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    op.add_column("restore_plans", sa.Column("ir_overlay_bytes", sa.BigInteger(), nullable=True))
    op.add_column("restore_plans", sa.Column("ir_storage_total_bytes", sa.BigInteger(), nullable=True))
    op.add_column("restore_plans", sa.Column("ir_storage_free_bytes", sa.BigInteger(), nullable=True))
    op.add_column("restore_plans", sa.Column("ir_move_bwlimit_kib", sa.Integer(), nullable=True))
    op.add_column("restore_plans", sa.Column("ir_last_error", sa.Text(), nullable=True))
    for name in ("ir_move_requested_by_user_id", "ir_discard_requested_by_user_id"):
        op.add_column(
            "restore_plans",
            sa.Column(
                name, postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
            ),
        )


def downgrade() -> None:
    for name in ("ir_move_requested_by_user_id", "ir_discard_requested_by_user_id"):
        op.drop_column("restore_plans", name)
    for name in (
        "ir_last_error",
        "ir_move_bwlimit_kib",
        "ir_storage_free_bytes",
        "ir_storage_total_bytes",
        "ir_overlay_bytes",
        *_TIMESTAMPS,
        "ir_vm_created",
        *_JSON_LISTS,
        "ir_keep_mac",
        "ir_original_vm",
    ):
        op.drop_column("restore_plans", name)
    op.drop_constraint("ck_restore_plans_method", "restore_plans", type_="check")
    op.drop_column("restore_plans", "method")
    op.drop_constraint("ck_pve_clusters_instant_recovery_alarm_hours", "pve_clusters", type_="check")
    op.drop_column("pve_clusters", "instant_recovery_alarm_hours")
    op.drop_column("pve_clusters", "instant_recovery_enabled")
