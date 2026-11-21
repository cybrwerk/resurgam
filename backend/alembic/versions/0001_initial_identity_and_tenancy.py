"""initial identity and tenancy model

Revision ID: 0001
Revises:
Create Date: 2026-09-12

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLES = [
    ("viewer", "Read status, usage, and inventory within own scope"),
    ("backup_operator", "Analyze logs and start allowed maintenance jobs"),
    ("restore_operator", "Browse files and request/execute restores"),
    ("customer_admin", "Manage users and visibility for own customer"),
    ("auditor", "Read-only access to audit data and reports"),
    ("platform_admin", "Manage systems, tokens, customers, and global policies"),
]


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("disabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_subject", sa.String(255), nullable=True, unique=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("is_breakglass", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("disabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(50), nullable=False, unique=True),
        sa.Column("description", sa.String(255), nullable=False),
    )

    op.create_table(
        "user_role_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope_type", sa.String(50), nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "role_id", "scope_type", "scope_id", name="uq_user_role_scope"),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_user_role_global_scope ON user_role_assignments (user_id, role_id) "
        "WHERE scope_type = 'global'"
    )

    op.create_table(
        "secret_references",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("reference_key", sa.String(255), nullable=False, unique=True),
        sa.Column("description", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("target_type", sa.String(100), nullable=True),
        sa.Column("target_id", sa.String(255), nullable=True),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("detail", postgresql.JSONB, nullable=True),
    )

    # Manifest §8: audit events must be immutable after being written. Enforce
    # this at the database level, not only by omitting update/delete code
    # paths in the application.
    op.execute(
        """
        CREATE FUNCTION resurgam_audit_events_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events rows are immutable (append-only)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_prevent_update
        BEFORE UPDATE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION resurgam_audit_events_immutable();
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_prevent_delete
        BEFORE DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION resurgam_audit_events_immutable();
        """
    )

    roles_table = sa.table(
        "roles",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("key", sa.String),
        sa.column("description", sa.String),
    )
    op.bulk_insert(
        roles_table,
        [{"id": uuid.uuid4(), "key": key, "description": description} for key, description in _ROLES],
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_events_prevent_delete ON audit_events")
    op.execute("DROP TRIGGER IF EXISTS audit_events_prevent_update ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS resurgam_audit_events_immutable()")
    op.drop_table("audit_events")
    op.drop_table("secret_references")
    op.execute("DROP INDEX IF EXISTS uq_user_role_global_scope")
    op.drop_table("user_role_assignments")
    op.drop_table("roles")
    op.drop_table("users")
    op.drop_table("customers")
