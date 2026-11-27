"""3-2-1 gap detection (ADR 0032): sync job fields that decide where a copy
lands, the per-cluster list of guests no backup job covers, append-only gap
exceptions with an audited reason, and the offsite lag thresholds.

Revision ID: 0073
Revises: 0072
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0073"
down_revision: str | None = "0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sync_jobs", sa.Column("sync_direction", sa.String(10), nullable=False, server_default="pull"))
    op.add_column("sync_jobs", sa.Column("local_ns", sa.String(255), nullable=True))
    op.add_column("sync_jobs", sa.Column("remote_ns", sa.String(255), nullable=True))
    op.add_column("sync_jobs", sa.Column("max_depth", sa.Integer, nullable=True))
    op.add_column("sync_jobs", sa.Column("group_filter", postgresql.JSONB, nullable=True))
    op.add_column("sync_jobs", sa.Column("transfer_last", sa.Integer, nullable=True))
    op.add_column("sync_jobs", sa.Column("remote_host", sa.String(255), nullable=True))
    op.add_column("sync_jobs", sa.Column("remote_port", sa.Integer, nullable=True))
    op.add_column("sync_jobs", sa.Column("remote_fingerprint", sa.String(100), nullable=True))
    # Pull jobs keep their remote side nullable as before; a push job's
    # `remote_name` is always set by PBS, a local pull job has none.
    op.alter_column("sync_jobs", "remote_name", existing_type=sa.String(255), nullable=True)

    op.add_column("pve_clusters", sa.Column("backup_gaps_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("pve_clusters", sa.Column("backup_gaps_error", sa.Text, nullable=True))

    op.add_column("instance_settings", sa.Column("offsite_lag_warning_hours", sa.Integer, nullable=True))
    op.add_column("instance_settings", sa.Column("offsite_lag_critical_hours", sa.Integer, nullable=True))

    op.create_table(
        "pve_guest_backup_gaps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pve_cluster_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pve_clusters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("vmid", sa.Integer, nullable=False),
        sa.Column("guest_type", sa.String(10), nullable=False),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("reason", sa.String(30), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("pve_cluster_id", "vmid", name="uq_pve_guest_backup_gap"),
    )

    op.create_table(
        "backup_gap_exceptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("target_kind", sa.String(20), nullable=False),
        # No foreign keys on the target columns: the history must outlive a
        # removed cluster or datastore, same as `audit_events.target_id`.
        sa.Column("pve_cluster_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("vmid", sa.Integer, nullable=True),
        sa.Column("datastore_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("namespace", sa.String(255), nullable=True),
        sa.Column("backup_type", sa.String(20), nullable=True),
        sa.Column("backup_id", sa.String(100), nullable=True),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "target_kind IN ('pve_guest', 'pve_cluster', 'backup_group', 'datastore')",
            name="ck_backup_gap_exceptions_kind",
        ),
    )
    op.create_index(
        "ix_backup_gap_exceptions_target",
        "backup_gap_exceptions",
        ["target_kind", "pve_cluster_id", "datastore_id", "created_at"],
    )
    op.execute(
        """
        CREATE FUNCTION resurgam_backup_gap_exceptions_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'backup_gap_exceptions rows are immutable (append-only)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for event in ("UPDATE", "DELETE"):
        op.execute(
            f"""
            CREATE TRIGGER backup_gap_exceptions_prevent_{event.lower()}
            BEFORE {event} ON backup_gap_exceptions
            FOR EACH ROW EXECUTE FUNCTION resurgam_backup_gap_exceptions_immutable();
            """
        )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS backup_gap_exceptions_prevent_delete ON backup_gap_exceptions")
    op.execute("DROP TRIGGER IF EXISTS backup_gap_exceptions_prevent_update ON backup_gap_exceptions")
    op.execute("DROP FUNCTION IF EXISTS resurgam_backup_gap_exceptions_immutable()")
    op.drop_index("ix_backup_gap_exceptions_target", table_name="backup_gap_exceptions")
    op.drop_table("backup_gap_exceptions")
    op.drop_table("pve_guest_backup_gaps")
    op.drop_column("instance_settings", "offsite_lag_critical_hours")
    op.drop_column("instance_settings", "offsite_lag_warning_hours")
    op.drop_column("pve_clusters", "backup_gaps_error")
    op.drop_column("pve_clusters", "backup_gaps_checked_at")
    op.execute("UPDATE sync_jobs SET remote_name = '' WHERE remote_name IS NULL")
    op.alter_column("sync_jobs", "remote_name", existing_type=sa.String(255), nullable=False)
    for column in (
        "remote_fingerprint",
        "remote_port",
        "remote_host",
        "transfer_last",
        "group_filter",
        "max_depth",
        "remote_ns",
        "local_ns",
        "sync_direction",
    ):
        op.drop_column("sync_jobs", column)
