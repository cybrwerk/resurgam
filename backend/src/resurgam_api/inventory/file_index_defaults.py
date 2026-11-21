"""Default File Indexing exclude patterns (manifest §13, ADR 0016).

Applied automatically to every newly synced `BackupGroup` (via this
module's constant as that column's default -- see
`models.inventory.BackupGroup.file_index_exclude_patterns`), not merely
suggested: raised directly by the user after reviewing a real VM's index
job spending most of its time walking Windows' component store
(`WinSxS`) -- these are large, OS-internal directories that are
essentially never individually searched/restored, so skipping them by
default is the safe, high-value choice. Deliberately narrower than the
OS's own pseudo-filesystems (`/proc`, `/sys`, `/dev`, ...): those aren't
normally present in a backup image at all, and a bare short segment name
like that risks matching an unrelated real directory with the same name
elsewhere in the tree.

Changing a specific group's list away from this default is an explicit,
intentional operator action (`PUT /backup-groups/{id}/file-index-exclude-
patterns`) -- this module only supplies what a *new* group starts with.
Existing groups synced before this default existed keep whatever list
they already had (usually empty) rather than being silently rewritten.

Single source of truth for both the ORM column default and the read-only
`GET /file-index-default-exclude-patterns` endpoint the frontend calls to
show/reapply the same list -- never duplicate this list in TypeScript.
"""

DEFAULT_FILE_INDEX_EXCLUDE_PATTERNS: list[str] = [
    # Windows
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
    # Linux
    r"(^|/)var/cache/apt/archives(/|$)",
    r"(^|/)var/cache/(yum|dnf)(/|$)",
    r"(^|/)var/lib/docker(/|$)",
    r"(^|/)var/lib/snapd(/|$)",
    r"(^|/)snap(/|$)",
    r"(^|/)lost\+found(/|$)",
    r"(^|/)swapfile$",
    r"(^|/)swap\.img$",
    # Kernel headers and modules: confirmed live (2026-11-12) on a real
    # 100 GB Ubuntu VM -- 75% of its 251k indexed entries were
    # `/usr/src/linux-headers-*` and `/lib/modules`, one copy per
    # installed kernel, never searched or restored individually.
    r"(^|/)usr/src/linux-[^/]+(/|$)",
    r"(^|/)(usr/)?lib/modules(/|$)",
    # Live 2026-11-13 (appliance, Windows 11 VM): `Windows/servicing/LCU`
    # holds the unpacked payload of the latest cumulative update -- OS
    # component data like WinSxS, >136k entries (~35% of that VM's index)
    # and the slowest part of the walk. Not user data.
    r"(^|/)Windows/servicing/LCU(/|$)",
    # Windows compatibility junctions (NTFS reparse points kept for
    # pre-Vista paths). `proxmox-file-restore list` reports them as plain
    # directories, so the walk followed them: live 2026-11-13 on a German
    # Windows 11 VM, `Dokumente und Einstellungen` alone re-indexed 174k
    # entries of `Users`, `Programme` re-indexed `Program Files`.
    # English + German names, anchored to where Windows creates them so a
    # user's own folder of the same name elsewhere is still indexed.
    r"(^|/)part/[^/]+/(Documents and Settings|Dokumente und Einstellungen|Programme)(/|$)",
    r"(^|/)Users/(All Users|Default User)(/|$)",
    (
        r"(^|/)Users/[^/]+/(Application Data|Anwendungsdaten|Cookies|Local Settings|Lokale Einstellungen"
        r"|My Documents|Eigene Dateien|NetHood|Netzwerkumgebung|PrintHood|Druckumgebung|Recent|SendTo"
        r"|Start Menu|Startmenü|Templates|Vorlagen)(/|$)"
    ),
    r"(^|/)Users/[^/]+/AppData/Local/(Application Data|Anwendungsdaten|History|Verlauf|Temporary Internet Files)(/|$)",
    r"(^|/)Users/[^/]+/Documents/(My Music|My Pictures|My Videos|Eigene Musik|Eigene Bilder|Eigene Videos)(/|$)",
    (
        r"(^|/)ProgramData/(Application Data|Anwendungsdaten|Desktop|Documents|Dokumente|Favorites|Favoriten"
        r"|Start Menu|Startmenü|Templates|Vorlagen)(/|$)"
    ),
    r"(^|/)Start Menu/Programme(/|$)",
    r"(^|/)Startmenü/Programme(/|$)",
    # Live 2026-11-13 (Linux Mint VM, 64 GB): Timeshift's own system
    # snapshots (full copies of `/`) were 2.96M of 4.99M entries, the
    # icon themes another 1.73M. Neither is user data.
    r"(^|/)timeshift(-btrfs)?/snapshots[^/]*(/|$)",
    r"(^|/)usr/share/icons(/|$)",
    # Windows Update download cache (Delivery Optimization): ~3 GB of
    # update payload on the same Windows 11 VM; re-downloadable, never user
    # data. (`Windows/SoftwareDistribution` is already excluded in this list.)
    r"(^|/)Windows/ServiceProfiles/NetworkService/AppData/Local/Microsoft/Windows/DeliveryOptimization(/|$)",
]


def default_file_index_exclude_patterns() -> list[str]:
    """Factory for the ORM column default -- a plain list must never be
    used directly as a SQLAlchemy `default=`, since that would share one
    mutable list object across every row's in-memory default instead of
    giving each a fresh copy.
    """
    return list(DEFAULT_FILE_INDEX_EXCLUDE_PATTERNS)
