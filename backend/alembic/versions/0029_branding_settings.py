"""Administration -> System Settings, first real content for that nav
placeholder: white-label branding, confirmed per-instance (not
per-customer) -- one logo, one primary color, one accent color, shown
everywhere including the pre-login page. Singleton table: at most one
row is ever expected (enforced in application code, matching
`oidc_settings`'s existing precedent -- no unique-constraint-on-a-
constant trick, same reasoning as migration 0027).

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-20

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "branding_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("logo", sa.LargeBinary, nullable=True),
        sa.Column("logo_content_type", sa.String(100), nullable=True),
        sa.Column("primary_color", sa.String(7), nullable=True),
        sa.Column("accent_color", sa.String(7), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("branding_settings")
