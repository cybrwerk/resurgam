"""Release update source (docs/adr/0026): the singleton
`update_source_settings` table (GitHub/GitLab repository URL, PAT reference, check
schedule and the cached result of the last release check), and
`update_source_token` added to `secret_references.purpose`'s allowed values.

Revision ID: 0058
Revises: 0057
Create Date: 2026-10-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_PURPOSES = ("infra_credential", "mfa_totp", "oidc_client_secret", "smtp_password", "pbs_encryption_key")
_NEW_PURPOSES = _OLD_PURPOSES + ("update_source_token",)


def upgrade() -> None:
    op.drop_constraint("ck_secret_references_purpose", "secret_references", type_="check")
    allowed = ", ".join(f"'{p}'" for p in _NEW_PURPOSES)
    op.create_check_constraint("ck_secret_references_purpose", "secret_references", sa.text(f"purpose IN ({allowed})"))

    op.create_table(
        "update_source_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("provider", sa.String(10), nullable=False),
        sa.Column("repository_url", sa.String(500), nullable=False),
        sa.Column(
            "token_ref_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("secret_references.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("tls_verify", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("include_prereleases", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("auto_check", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("check_interval_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_check_error", sa.Text(), nullable=True),
        sa.Column("latest_version", sa.String(64), nullable=True),
        sa.Column("available_releases", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint("provider IN ('github', 'gitlab')", name="ck_update_source_settings_provider"),
        sa.CheckConstraint(
            "check_interval_hours BETWEEN 1 AND 720", name="ck_update_source_settings_check_interval_hours"
        ),
    )


def downgrade() -> None:
    op.drop_table("update_source_settings")
    op.execute("DELETE FROM secret_references WHERE purpose = 'update_source_token'")
    op.drop_constraint("ck_secret_references_purpose", "secret_references", type_="check")
    allowed = ", ".join(f"'{p}'" for p in _OLD_PURPOSES)
    op.create_check_constraint("ck_secret_references_purpose", "secret_references", sa.text(f"purpose IN ({allowed})"))
