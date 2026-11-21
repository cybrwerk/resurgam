"""Split a BackupGroup's history when a VMID is reused for a different
guest -- raised directly by the user: a deleted-and-recreated VMID
should never silently blend two unrelated VMs' history into one
continuous timeline. Adds `superseded_at` and replaces the plain
identity uniqueness constraint with a partial unique index that only
enforces "at most one *current* row per identity tuple" -- the same
shape already used for `pricing_plans`' global/customer partial indexes
(0009_billing_foundations.py), applied here rather than re-declared as
an ORM-level Index, matching that migration's own convention.

Revision ID: 0039
Revises: 0038
Create Date: 2026-09-21

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backup_groups", sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))

    op.drop_constraint("uq_backup_group_identity", "backup_groups", type_="unique")

    # A VMID slot can have many superseded (closed) generations over
    # time, but at most one currently-open (superseded_at IS NULL)
    # generation at once -- a plain UniqueConstraint can't express that,
    # a partial index can.
    op.create_index(
        "uq_backup_group_identity_current",
        "backup_groups",
        ["datastore_id", "namespace", "backup_type", "backup_id"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_backup_group_identity_current", table_name="backup_groups")
    # Fails loudly if any identity tuple has more than one row (e.g. a
    # closed generation left over from a real split) -- expected: there
    # is no lossless way to collapse multiple generations back into the
    # old one-row-forever model.
    op.create_unique_constraint(
        "uq_backup_group_identity", "backup_groups", ["datastore_id", "namespace", "backup_type", "backup_id"]
    )
    op.drop_column("backup_groups", "superseded_at")
