"""3-2-1-1-0 self-assessment: an append-only `compliance_attestations`
table filling the three legs RESURGAM cannot observe on its own
(immutability, production copy count, media-type diversity) -- raised
directly by the user, every attestation carries its author and is kept
as permanent history, never overwritten. Immutability enforced at the
database level exactly like `audit_events` (migration 0001): a trigger
rejects UPDATE and DELETE outright, not just an absent application-code
path.

Revision ID: 0030
Revises: 0029
Create Date: 2026-09-20

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "compliance_attestations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("scope_type", sa.String(20), nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dimension", sa.String(50), nullable=False),
        sa.Column("value", postgresql.JSONB, nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column(
            "asserted_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_compliance_attestations_scope_dimension",
        "compliance_attestations",
        ["scope_type", "scope_id", "dimension", "created_at"],
    )

    op.execute(
        """
        CREATE FUNCTION resurgam_compliance_attestations_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'compliance_attestations rows are immutable (append-only)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER compliance_attestations_prevent_update
        BEFORE UPDATE ON compliance_attestations
        FOR EACH ROW EXECUTE FUNCTION resurgam_compliance_attestations_immutable();
        """
    )
    op.execute(
        """
        CREATE TRIGGER compliance_attestations_prevent_delete
        BEFORE DELETE ON compliance_attestations
        FOR EACH ROW EXECUTE FUNCTION resurgam_compliance_attestations_immutable();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS compliance_attestations_prevent_delete ON compliance_attestations")
    op.execute("DROP TRIGGER IF EXISTS compliance_attestations_prevent_update ON compliance_attestations")
    op.execute("DROP FUNCTION IF EXISTS resurgam_compliance_attestations_immutable()")
    op.drop_index("ix_compliance_attestations_scope_dimension", table_name="compliance_attestations")
    op.drop_table("compliance_attestations")
