"""Add the kernel headers/modules exclude patterns to every backup group
that still uses the unmodified default File Indexing exclude list.

Groups whose list an operator changed keep it untouched -- same rule as
`inventory/file_index_defaults.py`: changing a group's list is an
explicit operator action this migration must not override.

Both lists are spelled out here rather than imported, so the migration
keeps doing the same thing if the live default changes again later.

Revision ID: 0066
Revises: 0065
Create Date: 2026-11-12

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREVIOUS_DEFAULT = [
    r"(^|/)Windows/WinSxS(/|$)",
    r"(^|/)Windows/Installer(/|$)",
    r"(^|/)Windows/SoftwareDistribution(/|$)",
    r"(^|/)Windows/Temp(/|$)",
    r"(^|/)Windows/CSC(/|$)",
    r"(^|/)Windows/Panther(/|$)",
    r"(^|/)\$Recycle\.Bin(/|$)",
    r"(^|/)System Volume Information(/|$)",
    r"(^|/)AppData/Local/Temp(/|$)",
    r"(^|/)pagefile\.sys$",
    r"(^|/)hiberfil\.sys$",
    r"(^|/)swapfile\.sys$",
    r"(^|/)var/cache/apt/archives(/|$)",
    r"(^|/)var/cache/(yum|dnf)(/|$)",
    r"(^|/)var/lib/docker(/|$)",
    r"(^|/)var/lib/snapd(/|$)",
    r"(^|/)snap(/|$)",
    r"(^|/)lost\+found(/|$)",
    r"(^|/)swapfile$",
    r"(^|/)swap\.img$",
]
_ADDED = [
    r"(^|/)usr/src/linux-[^/]+(/|$)",
    r"(^|/)(usr/)?lib/modules(/|$)",
]


def _replace(old: list[str], new: list[str]) -> None:
    op.get_bind().execute(
        sa.text(
            "UPDATE backup_groups SET file_index_exclude_patterns = CAST(:new AS jsonb) "
            "WHERE file_index_exclude_patterns = CAST(:old AS jsonb)"
        ),
        {"old": json.dumps(old), "new": json.dumps(new)},
    )


def upgrade() -> None:
    _replace(_PREVIOUS_DEFAULT, _PREVIOUS_DEFAULT + _ADDED)


def downgrade() -> None:
    _replace(_PREVIOUS_DEFAULT + _ADDED, _PREVIOUS_DEFAULT)
