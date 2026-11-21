"""UX-audit finding (2026-09-24): the Credentials page's own description
promised "wann er zuletzt rotiert wurde" (when it was last rotated) but
no such column existed. Backfills existing rows to their own
`created_at` (the best available approximation: the value currently in
place was, as far as this table's own history goes, set then).

Revision ID: 0046
Revises: 0045
Create Date: 2026-09-24

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A real bug found live (2026-09-24): omitting `server_default` here
    # (relying only on the SQLAlchemy *model's* `server_default=func.now()`,
    # which is metadata for autogeneration, not a DDL clause on its own)
    # left the column with no actual database-level default -- every
    # subsequent INSERT that (correctly) omits this column to let the
    # database fill it in immediately violated the NOT NULL constraint.
    op.add_column(
        "secret_references",
        sa.Column("last_rotated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
    )
    op.execute("UPDATE secret_references SET last_rotated_at = created_at WHERE last_rotated_at IS NULL")
    op.alter_column("secret_references", "last_rotated_at", nullable=False)


def downgrade() -> None:
    op.drop_column("secret_references", "last_rotated_at")
