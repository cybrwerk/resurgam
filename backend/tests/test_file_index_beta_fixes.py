"""File Indexing fixes from the 2026-11-12 live run on the appliance:
kernel headers/modules excluded by default (and migration 0066 adding
them to groups still on the old default), and the native workers waiting
for the database at boot instead of crashing."""

import importlib.util
import itertools
import re
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from resurgam_api.inventory.file_index_defaults import DEFAULT_FILE_INDEX_EXCLUDE_PATTERNS
from tests.test_file_index_worker import _create_group, _setup_group

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _excluded(path: str) -> bool:
    return any(re.search(p, path, re.IGNORECASE) for p in DEFAULT_FILE_INDEX_EXCLUDE_PATTERNS)


@pytest.mark.parametrize(
    "path",
    [
        "/usr/src/linux-headers-5.15.0-67-generic",
        "/usr/src/linux-headers-5.15.0-67/include/linux/sched.h",
        "/drive-scsi0.img.fidx/lvm/ubuntu-vg/ubuntu-lv/usr/src/linux-hwe-6.8-headers-6.8.0-40/Makefile",
        "/lib/modules/5.15.0-67-generic/kernel/fs/ext4/ext4.ko",
        "/drive-scsi0.img.fidx/part/2/usr/lib/modules/6.1.0-27-amd64/modules.dep",
        "/drive-sata0.img.fidx/part/2/Windows/servicing/LCU",
        "/drive-sata0.img.fidx/part/2/Windows/servicing/LCU/Package_for_RollupFix~31bf3856ad364e35~amd64~~26100.1",
        "/drive-sata0.img.fidx/part/2/Dokumente und Einstellungen",
        "/drive-sata0.img.fidx/part/2/Dokumente und Einstellungen/admin/Desktop/x.txt",
        "/drive-scsi0.img.fidx/part/3/Documents and Settings/bob",
        "/drive-sata0.img.fidx/part/2/Programme/Mozilla Firefox/firefox.exe",
        "/drive-sata0.img.fidx/part/2/Users/All Users/Microsoft",
        "/drive-sata0.img.fidx/part/2/Users/admin/Anwendungsdaten/foo",
        "/drive-sata0.img.fidx/part/2/Users/admin/Lokale Einstellungen",
        "/drive-sata0.img.fidx/part/2/Users/admin/STARTMENÜ/Programme",
        "/drive-sata0.img.fidx/part/2/Users/admin/AppData/Local/Anwendungsdaten/x",
        "/drive-sata0.img.fidx/part/2/Users/admin/Documents/Eigene Bilder",
        "/drive-sata0.img.fidx/part/2/ProgramData/Startmenü",
        "/drive-sata0.img.fidx/part/2/ProgramData/Microsoft/Windows/Start Menu/Programme",
        "/drive-sata0.img.fidx/part/3/timeshift/snapshots/2025-04-22_22-10-34/localhost/etc/fstab",
        "/drive-sata0.img.fidx/part/3/timeshift/snapshots-ondemand",
        "/drive-scsi0.img.fidx/part/1/usr/share/icons/Mint-Y-Orange/places/128@2x/folder.png",
        (
            "/drive-sata0.img.fidx/part/2/Windows/ServiceProfiles/NetworkService/AppData/Local/Microsoft/Windows/"
            "DeliveryOptimization/Cache/239cdd63968b8a75dee24dc93b4d455580e2746a/content.bin"
        ),
    ],
)
def test_default_excludes_skip_kernel_headers_and_modules(path):
    assert _excluded(path)


@pytest.mark.parametrize(
    "path",
    [
        "/usr/src/myapp/main.c",
        "/usr/src",
        "/home/user/lib/modules-notes.txt",
        "/drive-scsi0.img.fidx/lvm/ubuntu-vg/ubuntu-lv/etc/nginx/nginx.conf",
        "/var/www/linux-headers.html",
        "/drive-sata0.img.fidx/part/2/Windows/servicing/Packages/foo.mum",
        "/drive-sata0.img.fidx/part/2/Users/admin/Documents/LCU-notes.docx",
        "/drive-sata0.img.fidx/part/2/Program Files/Mozilla Firefox/firefox.exe",
        "/drive-sata0.img.fidx/part/2/Users/admin/Documents/Programme/setup.exe",
        "/drive-sata0.img.fidx/part/2/Users/admin/Documents/Vorlagen/brief.dotx",
        "/drive-sata0.img.fidx/part/2/Users/admin/AppData/Roaming/Microsoft/Windows/Recent/a.lnk",
        "/drive-sata0.img.fidx/part/2/Users/admin/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/x.lnk",
        "/drive-sata0.img.fidx/part/2/Users/admin/Desktop/Cookies.txt",
        "/drive-sata0.img.fidx/part/2/Users/Public/Documents/report.pdf",
        "/drive-sata0.img.fidx/part/3/home/alice/timeshift-notes.txt",
        "/drive-sata0.img.fidx/part/3/home/alice/.local/share/icons/custom.png",
        "/drive-sata0.img.fidx/part/2/Windows/ServiceProfiles/NetworkService/AppData/Local/Microsoft/Windows/Logs",
    ],
)
def test_default_excludes_keep_ordinary_paths(path):
    assert not _excluded(path)


# Every "extend the untouched default exclude list" migration, oldest
# first. Each one's previous+added list must be the next one's previous
# list, and the newest one's must equal the live default.
EXCLUDE_MIGRATIONS = [
    VERSIONS / "0066_file_index_kernel_excludes.py",
    VERSIONS / "0067_file_index_beta_excludes.py",
    VERSIONS / "0068_file_index_delivery_optimization_exclude.py",
]


def _load_migration(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(db_session, fn_name: str, path: Path) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = _load_migration(path)
    context = MigrationContext.configure(db_session.connection())
    with Operations.context(context):
        getattr(module, fn_name)()


def test_exclude_migrations_form_a_chain_ending_in_the_live_default():
    modules = [_load_migration(path) for path in EXCLUDE_MIGRATIONS]
    for older, newer in itertools.pairwise(modules):
        assert older._PREVIOUS_DEFAULT + older._ADDED == newer._PREVIOUS_DEFAULT
    assert modules[-1]._PREVIOUS_DEFAULT + modules[-1]._ADDED == DEFAULT_FILE_INDEX_EXCLUDE_PATTERNS


@pytest.mark.parametrize("path", EXCLUDE_MIGRATIONS, ids=lambda p: p.stem[:4])
def test_exclude_migration_extends_only_untouched_default_lists(
    path, client, platform_admin_headers, db_session, monkeypatch
):
    from resurgam_api.models.inventory import BackupGroup

    module = _load_migration(path)
    _c, default_group_id, _s = _setup_group(client, platform_admin_headers, db_session, monkeypatch, backup_id="100")
    default_group = db_session.get(BackupGroup, default_group_id)
    custom_group_id = _create_group(db_session, default_group.datastore_id, backup_id="101")
    custom_group = db_session.get(BackupGroup, custom_group_id)
    default_group.file_index_exclude_patterns = list(module._PREVIOUS_DEFAULT)
    custom_group.file_index_exclude_patterns = [r"^/usr/src"]
    db_session.flush()

    _run(db_session, "upgrade", path)
    db_session.expire_all()
    assert (
        db_session.get(BackupGroup, default_group_id).file_index_exclude_patterns
        == module._PREVIOUS_DEFAULT + module._ADDED
    )
    assert db_session.get(BackupGroup, custom_group_id).file_index_exclude_patterns == [r"^/usr/src"]

    _run(db_session, "downgrade", path)
    db_session.expire_all()
    assert db_session.get(BackupGroup, default_group_id).file_index_exclude_patterns == module._PREVIOUS_DEFAULT
    assert db_session.get(BackupGroup, custom_group_id).file_index_exclude_patterns == [r"^/usr/src"]


class _FlakySession:
    def __init__(self, outcomes):
        self._outcomes = outcomes

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, _statement):
        if self._outcomes.pop(0):
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))


def test_wait_for_database_retries_with_backoff_until_reachable():
    import logging

    from resurgam_api.db import wait_for_database

    outcomes = [True, True, True, False]  # fail, fail, fail, succeed
    sleeps: list[float] = []
    wait_for_database(
        lambda: _FlakySession(outcomes),
        logging.getLogger("test"),
        initial_delay=2.0,
        max_delay=5.0,
        sleep=sleeps.append,
    )
    assert sleeps == [2.0, 4.0, 5.0]
    assert outcomes == []


def test_wait_for_database_returns_at_once_when_reachable():
    import logging

    from resurgam_api.db import wait_for_database

    sleeps: list[float] = []
    wait_for_database(lambda: _FlakySession([False]), logging.getLogger("test"), sleep=sleeps.append)
    assert sleeps == []
