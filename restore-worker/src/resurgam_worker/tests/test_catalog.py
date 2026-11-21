"""`catalog.py` wraps `proxmox-backup-client catalog dump`/`restore` for
CT/host snapshots — confirmed live against a real PBS 4.2 instance
(2026-09-14). `fixtures/catalog_dump.txt` is a trimmed excerpt of a real
capture from that session (the same instance/format documented in
docs/milestone-1-pbs-pve-api-assumptions.md), not a synthetic guess.
"""

import subprocess
from pathlib import Path

import pytest

from resurgam_worker.pbs_client import catalog
from resurgam_worker.pbs_client.errors import (
    InvalidPathError,
    PbsBinaryNotFoundError,
    PbsCommandFailedError,
)
from resurgam_worker.pbs_client.models import CatalogEntryType

FIXTURES = Path(__file__).parent / "fixtures"
REPO = "user@pbs!token@host:datastore"
SNAPSHOT = "ct/100/2023-09-20T00:00:03Z"


def _fake_completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _fake_which(monkeypatch):
    monkeypatch.setattr(catalog.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_dump_catalog_parses_real_capture_and_strips_archive_prefix(monkeypatch):
    raw = (FIXTURES / "catalog_dump.txt").read_text()
    monkeypatch.setattr(catalog, "_run", lambda command, **kw: _fake_completed(0, stdout=raw))

    entries = catalog.dump_catalog(REPO, SNAPSHOT, namespace="tenant-a")

    by_path = {e.path: e for e in entries}
    assert by_path["/"].type == CatalogEntryType.DIRECTORY
    assert by_path["/bin"].type == CatalogEntryType.SYMLINK
    assert by_path["/boot/grub/unicode.pf2"].type == CatalogEntryType.FILE
    assert by_path["/boot/grub/unicode.pf2"].size == 2394108
    assert by_path["/boot/grub/unicode.pf2"].mtime == "2023-02-20T06:23:01Z"
    assert by_path["/etc/.git/hooks/pre-commit"].size == 118
    # No entry should still carry the raw "./root.pxar.didx" prefix.
    assert all(not p.startswith("./") and not p.startswith("root.pxar") for p in by_path)


def test_dump_catalog_reads_from_stderr_not_only_stdout(monkeypatch):
    """Confirmed live (2026-09-15) against a real `proxmox-backup-client
    3.4.7`: `catalog dump` writes its actual listing to stderr, not
    stdout (stdout is empty on a successful run) — it goes through the
    client's own logging framework rather than being treated as command
    output. A real, working call was returning zero entries before this
    was caught and fixed.
    """
    monkeypatch.setattr(
        catalog,
        "_run",
        lambda command, **kw: _fake_completed(
            0, stdout="", stderr='d "./root.pxar.didx"\nf "./root.pxar.didx/etc/hosts" 10 2023-09-20T00:00:00Z\n'
        ),
    )

    entries = catalog.dump_catalog(REPO, SNAPSHOT)

    by_path = {e.path: e for e in entries}
    assert by_path["/"].type == CatalogEntryType.DIRECTORY
    assert by_path["/etc/hosts"].type == CatalogEntryType.FILE
    assert by_path["/etc/hosts"].size == 10


def test_dump_catalog_passes_namespace_and_repository(monkeypatch):
    captured = {}

    def fake_run(command, **kw):
        captured["command"] = command
        return _fake_completed(0, stdout='d "./root.pxar.didx"\n')

    monkeypatch.setattr(catalog, "_run", fake_run)
    catalog.dump_catalog(REPO, SNAPSHOT, namespace="tenant-a")

    assert captured["command"][:3] == ["/usr/bin/proxmox-backup-client", "catalog", "dump"]
    assert SNAPSHOT in captured["command"]
    assert "--repository" in captured["command"]
    assert REPO in captured["command"]
    assert "--ns" in captured["command"]
    assert "tenant-a" in captured["command"]


def test_dump_catalog_omits_ns_flag_for_root_namespace(monkeypatch):
    captured = {}

    def fake_run(command, **kw):
        captured["command"] = command
        return _fake_completed(0, stdout='d "./root.pxar.didx"\n')

    monkeypatch.setattr(catalog, "_run", fake_run)
    catalog.dump_catalog(REPO, SNAPSHOT)

    assert "--ns" not in captured["command"]


def test_dump_catalog_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(catalog, "_run", lambda command, **kw: _fake_completed(1, stderr="snapshot not found"))
    with pytest.raises(PbsCommandFailedError):
        catalog.dump_catalog(REPO, SNAPSHOT)


def test_dump_catalog_missing_binary_raises(monkeypatch):
    monkeypatch.setattr(catalog.shutil, "which", lambda name: None)
    with pytest.raises(PbsBinaryNotFoundError):
        catalog.dump_catalog(REPO, SNAPSHOT)


def test_list_directory_returns_only_immediate_children(monkeypatch):
    raw = (FIXTURES / "catalog_dump.txt").read_text()
    monkeypatch.setattr(catalog, "_run", lambda command, **kw: _fake_completed(0, stdout=raw))
    entries = catalog.dump_catalog(REPO, SNAPSHOT)

    root_children = {e.path for e in catalog.list_directory(entries, "/")}
    assert root_children == {"/bin", "/boot", "/dev", "/etc"}

    etc_children = {e.path for e in catalog.list_directory(entries, "/etc")}
    assert etc_children == {"/etc/.etckeeper", "/etc/.git"}

    git_children = {e.path for e in catalog.list_directory(entries, "/etc/.git")}
    assert git_children == {"/etc/.git/HEAD", "/etc/.git/hooks"}


def test_list_directory_rejects_traversal():
    with pytest.raises(InvalidPathError):
        catalog.list_directory([], "/etc/../../root")


def test_restore_archive_builds_correct_command(monkeypatch):
    captured = {}

    def fake_run(command, **kw):
        captured["command"] = command
        return _fake_completed(0)

    monkeypatch.setattr(catalog, "_run", fake_run)
    catalog.restore_archive(REPO, SNAPSHOT, "root.pxar", "/tmp/somewhere", namespace="tenant-a", pattern="/etc/hosts")

    assert captured["command"] == [
        "/usr/bin/proxmox-backup-client",
        "restore",
        SNAPSHOT,
        "root.pxar",
        "/tmp/somewhere",
        "--repository",
        REPO,
        "--ignore-ownership",
        "true",
        "--ignore-permissions",
        "true",
        "--ns",
        "tenant-a",
        "--pattern",
        "/etc/hosts",
    ]


def test_restore_archive_omits_pattern_when_not_given(monkeypatch):
    captured = {}

    def fake_run(command, **kw):
        captured["command"] = command
        return _fake_completed(0)

    monkeypatch.setattr(catalog, "_run", fake_run)
    catalog.restore_archive(REPO, SNAPSHOT, "root.pxar", "/tmp/somewhere")

    assert "--pattern" not in captured["command"]


def test_restore_archive_rejects_relative_target():
    with pytest.raises(InvalidPathError):
        catalog.restore_archive(REPO, SNAPSHOT, "root.pxar", "relative/path")


def test_extract_file_to_bytes_reads_from_restored_archive(monkeypatch, tmp_path):
    def fake_restore_archive(
        repository,
        snapshot,
        archive_name,
        target_dir,
        *,
        namespace="",
        pattern=None,
        token=None,
        keyfile=None,
        fingerprint=None,
        binary="",
    ):
        target = Path(target_dir)
        (target / "etc").mkdir(parents=True)
        (target / "etc" / "hosts").write_bytes(b"127.0.0.1 localhost\n")

    monkeypatch.setattr(catalog, "restore_archive", fake_restore_archive)

    content = catalog.extract_file_to_bytes(REPO, SNAPSHOT, "root.pxar", "/etc/hosts")
    assert content == b"127.0.0.1 localhost\n"


def test_extract_file_to_bytes_cleans_up_temp_dir_even_on_missing_file(monkeypatch):
    captured_dirs = []

    def fake_restore_archive(
        repository,
        snapshot,
        archive_name,
        target_dir,
        *,
        namespace="",
        pattern=None,
        token=None,
        keyfile=None,
        fingerprint=None,
        binary="",
    ):
        captured_dirs.append(target_dir)

    monkeypatch.setattr(catalog, "restore_archive", fake_restore_archive)

    with pytest.raises(InvalidPathError):
        catalog.extract_file_to_bytes(REPO, SNAPSHOT, "root.pxar", "/does/not/exist")

    assert len(captured_dirs) == 1
    assert not Path(captured_dirs[0]).exists()


def test_pinned_fingerprint_reaches_the_cli_as_pbs_fingerprint(monkeypatch):
    """A self-signed PBS (RESURGAM's `fingerprint` trust mode) needs PBS_FINGERPRINT -- the sandboxed
    workers can't confirm a fingerprint interactively (live finding, 2026-10-07)."""
    from resurgam_worker.pbs_client import _subprocess

    seen = {}

    def fake_run_subprocess(command, *, env=None, **kw):
        seen.update(env or {})
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(_subprocess, "run_subprocess", fake_run_subprocess)
    monkeypatch.setattr(catalog.shutil, "which", lambda name: f"/usr/bin/{name}")
    catalog.dump_catalog("t@pbs!x@h:8007:ds", "ct/1/2026-01-01T00:00:00Z", token="s", fingerprint="F3A3560DFA8B")
    assert seen["PBS_FINGERPRINT"] == "f3:a3:56:0d:fa:8b" and seen["PBS_PASSWORD"] == "s"
    seen.clear()
    catalog.dump_catalog("t@pbs!x@h:8007:ds", "ct/1/2026-01-01T00:00:00Z", token="s")
    assert "PBS_FINGERPRINT" not in seen


# --- restore_selection: folder / multi-selection ZIP downloads ---------------------------------


def _guarded(returncode: int, stderr: str = ""):
    from resurgam_worker.pbs_client._subprocess import GuardedResult

    return GuardedResult(returncode=returncode, stderr=stderr)


def test_escape_pattern_escapes_glob_characters():
    assert catalog.escape_pattern("/srv/[draft] notes*?.txt") == "/srv/\\[draft\\] notes\\*\\?.txt"
    assert catalog.escape_pattern("/etc/back\\slash") == "/etc/back\\\\slash"
    assert catalog.escape_pattern("/etc/nginx") == "/etc/nginx"


def test_restore_selection_restores_all_paths_in_one_call(monkeypatch, tmp_path):
    calls = []

    def fake(command, **kw):
        calls.append((command, kw))
        return _guarded(0)

    monkeypatch.setattr(catalog, "_run_guarded", fake)
    guard = lambda: None
    failed = catalog.restore_selection(
        REPO,
        SNAPSHOT,
        "root.pxar",
        str(tmp_path),
        ["/etc/nginx", "/srv/[x]"],
        namespace="tenant-a",
        guard=guard,
        timeout=99,
    )

    assert failed == {}
    assert len(calls) == 1
    command, kw = calls[0]
    assert command == [
        "/usr/bin/proxmox-backup-client",
        "restore",
        SNAPSHOT,
        "root.pxar",
        str(tmp_path),
        "--repository",
        REPO,
        "--ignore-ownership",
        "true",
        "--ignore-permissions",
        "true",
        "--ignore-acls",
        "true",
        "--ignore-xattrs",
        "true",
        "--ignore-extract-device-errors",
        "true",
        "--ns",
        "tenant-a",
        "--pattern",
        "/etc/nginx",
        "--pattern",
        "/srv/\\[x\\]",
    ]
    assert kw["guard"] is guard
    assert kw["timeout"] == 99


def test_restore_selection_retries_entry_by_entry_and_reports_failures(monkeypatch, tmp_path):
    (tmp_path / "leftover").write_text("from the failed combined run")

    def fake(command, **kw):
        patterns = [command[i + 1] for i, a in enumerate(command) if a == "--pattern"]
        if len(patterns) > 1:
            return _guarded(255, 'error at entry "secret": Permission denied')
        if patterns == ["/root/broken"]:
            (tmp_path / "root" / "broken").mkdir(parents=True)
            return _guarded(255, 'something\nerror at entry "x": Input/output error\n')
        (tmp_path / "etc").mkdir(exist_ok=True)
        return _guarded(0)

    monkeypatch.setattr(catalog, "_run_guarded", fake)
    failed = catalog.restore_selection(REPO, SNAPSHOT, "root.pxar", str(tmp_path), ["/etc/hosts", "/root/broken"])

    assert failed == {"/root/broken": 'error at entry "x": Input/output error'}
    assert not (tmp_path / "leftover").exists()
    assert not (tmp_path / "root" / "broken").exists()


def test_restore_selection_raises_when_every_path_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(catalog, "_run_guarded", lambda command, **kw: _guarded(1, "snapshot not found"))
    with pytest.raises(PbsCommandFailedError):
        catalog.restore_selection(REPO, SNAPSHOT, "root.pxar", str(tmp_path), ["/a", "/b"])


def test_restore_selection_does_not_retry_huge_selections(monkeypatch, tmp_path):
    from resurgam_worker.pbs_client.errors import SelectionTooLargeError

    calls = []
    monkeypatch.setattr(catalog, "_run_guarded", lambda command, **kw: calls.append(1) or _guarded(1, "boom"))
    paths = [f"/f{i}" for i in range(catalog.RETRY_INDIVIDUALLY_LIMIT + 1)]
    with pytest.raises(SelectionTooLargeError):
        catalog.restore_selection(REPO, SNAPSHOT, "root.pxar", str(tmp_path), paths)
    assert calls == [1]


def test_restore_selection_rejects_traversal(tmp_path):
    with pytest.raises(InvalidPathError):
        catalog.restore_selection(REPO, SNAPSHOT, "root.pxar", str(tmp_path), ["/etc/../../root"])


def test_extract_file_to_path_moves_the_restored_file_and_cleans_staging(monkeypatch, tmp_path):
    staging = tmp_path / "job.staging"
    out = tmp_path / "job.bin"

    def fake(command, **kw):
        target = Path(command[4])
        (target / "etc").mkdir()
        (target / "etc" / "hosts").write_text("127.0.0.1 localhost\n")
        return _guarded(0)

    monkeypatch.setattr(catalog, "_run_guarded", fake)
    catalog.extract_file_to_path(REPO, SNAPSHOT, "root.pxar", "/etc/hosts", str(out), staging_dir=str(staging))

    assert out.read_text() == "127.0.0.1 localhost\n"
    assert not staging.exists()


def test_extract_file_to_path_rejects_a_directory(monkeypatch, tmp_path):
    def fake(command, **kw):
        (Path(command[4]) / "etc").mkdir()
        return _guarded(0)

    monkeypatch.setattr(catalog, "_run_guarded", fake)
    with pytest.raises(InvalidPathError):
        catalog.extract_file_to_path(
            REPO, SNAPSHOT, "root.pxar", "/etc", str(tmp_path / "o"), staging_dir=str(tmp_path / "s")
        )
    assert not (tmp_path / "s").exists()
