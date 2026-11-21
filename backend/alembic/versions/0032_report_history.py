"""A permanent, checksummed record of every generated Recovery Evidence
PDF -- raised directly by the user: the exact bytes handed to an
auditor should stay re-downloadable later, not just be regenerable
(which could legitimately produce different numbers as data changes).
Content row (`report_history`) is immutable once written -- a trigger
rejects UPDATE, same discipline as `audit_events`/
`compliance_attestations` -- but, unlike those two tables, rows here
ARE deletable: the whole point of this table is a governed retention
period, and the retention sweep needs to purge expired rows. So the
trigger blocks UPDATE only, not DELETE.

Revision ID: 0032
Revises: 0031
Create Date: 2026-09-20

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "report_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("report_type", sa.String(50), nullable=False),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("date_from", sa.Date, nullable=False),
        sa.Column("date_to", sa.Date, nullable=False),
        sa.Column("framework", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "generated_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("sha256_checksum", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_report_history_customer_created_at", "report_history", ["customer_id", "created_at"])

    op.create_table(
        "report_history_content",
        sa.Column(
            "report_history_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("report_history.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("pdf_content", sa.LargeBinary, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.execute(
        """
        CREATE FUNCTION resurgam_report_history_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'report_history rows are immutable once written (still deletable by the retention sweep)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER report_history_prevent_update
        BEFORE UPDATE ON report_history
        FOR EACH ROW EXECUTE FUNCTION resurgam_report_history_immutable();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS report_history_prevent_update ON report_history")
    op.execute("DROP FUNCTION IF EXISTS resurgam_report_history_immutable()")
    op.drop_table("report_history_content")
    op.drop_index("ix_report_history_customer_created_at", table_name="report_history")
    op.drop_table("report_history")
