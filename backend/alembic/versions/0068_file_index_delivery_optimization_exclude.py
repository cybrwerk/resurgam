"""Add the Windows Update download cache (Delivery Optimization) to every
backup group that still uses the unmodified default File Indexing exclude
list. Same rule as 0066/0067: lists an operator changed stay untouched.

Revision ID: 0068
Revises: 0067
Create Date: 2026-11-14

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0068"
down_revision: str | None = "0067"
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
    r"(^|/)usr/src/linux-[^/]+(/|$)",
    r"(^|/)(usr/)?lib/modules(/|$)",
    r"(^|/)Windows/servicing/LCU(/|$)",
    r"(^|/)part/[^/]+/(Documents and Settings|Dokumente und Einstellungen|Programme)(/|$)",
    r"(^|/)Users/(All Users|Default User)(/|$)",
    r"(^|/)Users/[^/]+/(Application Data|Anwendungsdaten|Cookies|Local Settings|Lokale Einstellungen|My Documents|Eigene Dateien|NetHood|Netzwerkumgebung|PrintHood|Druckumgebung|Recent|SendTo|Start Menu|Startmenü|Templates|Vorlagen)(/|$)",
    r"(^|/)Users/[^/]+/AppData/Local/(Application Data|Anwendungsdaten|History|Verlauf|Temporary Internet Files)(/|$)",
    r"(^|/)Users/[^/]+/Documents/(My Music|My Pictures|My Videos|Eigene Musik|Eigene Bilder|Eigene Videos)(/|$)",
    r"(^|/)ProgramData/(Application Data|Anwendungsdaten|Desktop|Documents|Dokumente|Favorites|Favoriten|Start Menu|Startmenü|Templates|Vorlagen)(/|$)",
    r"(^|/)Start Menu/Programme(/|$)",
    r"(^|/)Startmenü/Programme(/|$)",
    r"(^|/)timeshift(-btrfs)?/snapshots[^/]*(/|$)",
    r"(^|/)usr/share/icons(/|$)",
]
_ADDED = [
    r"(^|/)Windows/ServiceProfiles/NetworkService/AppData/Local/Microsoft/Windows/DeliveryOptimization(/|$)",
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
