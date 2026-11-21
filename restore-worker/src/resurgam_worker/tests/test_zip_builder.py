"""`zip_builder.ZipBuilder` -- the one ZIP a folder / multi-selection download produces, from
local trees (CT/host restore staging, VM single files) and from the ZIP `proxmox-file-restore
extract --format zip` builds (VM directories)."""

import os
import stat
import zipfile
from pathlib import Path

import pytest

from resurgam_worker.zip_builder import ZipBuilder, ZipWriteError, sanitize_component


def _names(zip_path: Path) -> list[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return zf.namelist()


def test_add_tree_preserves_relative_paths_and_empty_dirs(tmp_path):
    src = tmp_path / "staging" / "etc" / "nginx"
    (src / "sites").mkdir(parents=True)
    (src / "empty").mkdir()
    (src / "nginx.conf").write_text("worker_processes 1;\n")
    (src / "sites" / "default").write_text("server {}\n")
    out = tmp_path / "out.zip"

    with ZipBuilder(out) as builder:
        builder.add_tree(src, ["nginx"], "/etc/nginx")

    names = _names(out)
    assert "nginx/" in names
    assert "nginx/empty/" in names
    assert "nginx/nginx.conf" in names
    assert "nginx/sites/default" in names
    with zipfile.ZipFile(out) as zf:
        assert zf.read("nginx/nginx.conf") == b"worker_processes 1;\n"
    report = builder.report
    assert report.file_count == 2
    assert report.directory_count == 3
    assert report.total_bytes == len("worker_processes 1;\n") + len("server {}\n")
    assert report.skipped == []
    # remove_after=True (default): the staged copy is gone once it is in the archive
    assert not src.exists()


def test_non_ascii_names_get_the_utf8_flag(tmp_path):
    src = tmp_path / "Übersicht"
    src.mkdir()
    (src / "Grüsse – 日本.txt").write_text("hoi")
    out = tmp_path / "out.zip"

    with ZipBuilder(out) as builder:
        builder.add_tree(src, ["Übersicht"], "/Übersicht")

    with zipfile.ZipFile(out) as zf:
        info = zf.getinfo("Übersicht/Grüsse – 日本.txt")
        assert info.flag_bits & 0x800  # general purpose bit 11: name is UTF-8
        assert zf.read(info) == b"hoi"
    assert builder.report.renamed == []


def test_undecodable_bytes_are_replaced_and_reported(tmp_path):
    src = tmp_path / "dir"
    src.mkdir()
    raw = os.fsdecode(b"bad-\xff-name.txt")
    (src / raw).write_text("x")
    out = tmp_path / "out.zip"

    with ZipBuilder(out) as builder:
        builder.add_tree(src, ["dir"], "/dir")

    assert "dir/bad-�-name.txt" in _names(out)
    assert builder.report.renamed_count == 1


@pytest.mark.parametrize(
    ("raw", "safe"),
    [
        ("report:v2.txt", "report_v2.txt"),
        ('a<b>c"d|e?f*g', "a_b_c_d_e_f_g"),
        ("back\\slash", "back_slash"),
        ("trailing. ", "trailing_"),
        ("CON", "_CON"),
        ("lpt1.log", "_lpt1.log"),
        ("normal name.txt", "normal name.txt"),
        ("..", "_"),
    ],
)
def test_sanitize_component_makes_windows_safe_names(raw, safe):
    assert sanitize_component(raw) == safe


def test_windows_unsafe_and_case_colliding_names_are_renamed_once(tmp_path):
    src = tmp_path / "share"
    (src / "a:b").mkdir(parents=True)
    (src / "a:b" / "inner.txt").write_text("1")
    (src / "Readme").write_text("2")
    (src / "README").write_text("3")
    out = tmp_path / "out.zip"

    with ZipBuilder(out) as builder:
        builder.add_tree(src, ["share"], "/share")

    names = _names(out)
    assert "share/a_b/inner.txt" in names
    assert "share/README" in names
    assert "share/Readme (1)" in names
    # the directory rename is reported once, not again for its child
    reported = {r.path: r.archive_name for r in builder.report.renamed}
    assert reported == {"/share/a:b": "share/a_b", "/share/Readme": "share/Readme (1)"}


def test_symlinks_are_stored_not_followed(tmp_path):
    secret = tmp_path / "host-secret"
    secret.write_text("must never end up in the archive")
    src = tmp_path / "dir"
    src.mkdir()
    os.symlink(secret, src / "link")
    out = tmp_path / "out.zip"

    with ZipBuilder(out) as builder:
        builder.add_tree(src, ["dir"], "/dir")

    with zipfile.ZipFile(out) as zf:
        info = zf.getinfo("dir/link")
        assert stat.S_ISLNK(info.external_attr >> 16)
        assert zf.read(info) == str(secret).encode()
    assert builder.report.symlink_count == 1
    assert secret.exists()


def test_special_and_unreadable_entries_are_skipped_and_reported(tmp_path):
    src = tmp_path / "dir"
    src.mkdir()
    os.mkfifo(src / "pipe")
    (src / "ok.txt").write_text("fine")
    locked = src / "locked.txt"
    locked.write_text("secret")
    locked.chmod(0)
    out = tmp_path / "out.zip"

    try:
        with ZipBuilder(out) as builder:
            builder.add_tree(src, ["dir"], "/dir", remove_after=False)
    finally:
        locked.chmod(0o600)

    names = _names(out)
    assert "dir/ok.txt" in names
    assert "dir/pipe" not in names
    skipped = {s.path: s.reason for s in builder.report.skipped}
    assert "special file" in skipped["/dir/pipe"]
    if os.geteuid() != 0:  # root reads mode-0 files anyway
        assert "not readable" in skipped["/dir/locked.txt"]
        assert "dir/locked.txt" not in names


def test_missing_source_is_reported_not_fatal(tmp_path):
    out = tmp_path / "out.zip"
    with ZipBuilder(out) as builder:
        builder.add_tree(tmp_path / "nope", ["nope"], "/var/nope")
    assert builder.report.skipped[0].path == "/var/nope"
    assert _names(out) == []


def test_add_zip_merges_a_tool_built_zip_under_its_prefix(tmp_path):
    vendor = tmp_path / "vendor.zip"
    with zipfile.ZipFile(vendor, "w") as zf:
        zf.writestr("Users/", b"")
        zf.writestr("Users/Präsentation.pptx", b"pptx-bytes")
        zf.writestr("Users/../escape.txt", b"nope")
        link = zipfile.ZipInfo("Users/link")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(link, "Präsentation.pptx".encode())
    out = tmp_path / "out.zip"

    with ZipBuilder(out) as builder:
        builder.add_zip(vendor, ["part2"], "/drive-scsi0.img.fidx/part/2")

    with zipfile.ZipFile(out) as zf:
        assert zf.read("part2/Users/Präsentation.pptx") == b"pptx-bytes"
        assert zf.getinfo("part2/Users/Präsentation.pptx").flag_bits & 0x800
        assert stat.S_ISLNK(zf.getinfo("part2/Users/link").external_attr >> 16)
        # ".." is dropped -- nothing escapes the archive root
        assert "part2/Users/escape.txt" in zf.namelist()
        assert not any(".." in n for n in zf.namelist())
    assert builder.report.file_count == 2
    assert builder.report.symlink_count == 1


def test_add_zip_reports_tool_output_that_is_not_a_zip(tmp_path):
    bogus = tmp_path / "vendor.zip"
    bogus.write_text('{"message": "all mounts failed or no supported file system"}')
    out = tmp_path / "out.zip"

    with ZipBuilder(out) as builder:
        builder.add_zip(bogus, [], "/drive-scsi0.img.fidx/part/1")

    reason = builder.report.skipped[0].reason
    assert "no valid ZIP" in reason
    assert "all mounts failed" in reason


def test_check_aborts_the_build(tmp_path):
    src = tmp_path / "dir"
    src.mkdir()
    for i in range(3):
        (src / f"f{i}").write_text("x" * 10)

    class TooBig(Exception):
        pass

    def check(report):
        if report.total_bytes >= 10:
            raise TooBig

    with pytest.raises(TooBig), ZipBuilder(tmp_path / "out.zip", check=check) as builder:
        builder.add_tree(src, ["dir"], "/dir")


def test_check_sees_the_next_members_size_before_it_is_written(tmp_path):
    """A size limit must refuse the member that would cross it, not notice afterwards."""
    src = tmp_path / "dir"
    src.mkdir()
    (src / "a").write_bytes(b"x" * 60)
    (src / "b").write_bytes(b"y" * 60)

    class TooBig(Exception):
        pass

    def check(report):
        if report.total_bytes + report.pending_bytes > 100:
            raise TooBig

    builder = ZipBuilder(tmp_path / "out.zip", check=check)
    with pytest.raises(TooBig), builder:
        builder.add_tree(src, ["dir"], "/dir", remove_after=False)
    assert builder.report.total_bytes == 60
    assert builder.report.pending_bytes == 0


def test_a_failed_write_raises_instead_of_reporting_a_source_error(tmp_path, monkeypatch):
    """ENOSPC while writing the ZIP leaves it corrupt -- that must abort the build, not be
    reported as one unreadable entry."""
    src = tmp_path / "f.bin"
    src.write_bytes(b"z" * 4096)
    builder = ZipBuilder(tmp_path / "out.zip")
    real_open = builder._zip.open

    class FailingWriter:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.inner.close()

        def write(self, data):
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(builder._zip, "open", lambda info, mode="r": FailingWriter(real_open(info, mode)))
    with pytest.raises(ZipWriteError, match="No space left on device"), builder:
        builder.add_tree(src, ["f.bin"], "/f.bin", remove_after=False)
    assert builder.report.skipped == []


def test_a_source_read_error_is_still_reported_and_the_archive_stays_valid(tmp_path):
    good = tmp_path / "good.txt"
    good.write_text("fine")
    out = tmp_path / "out.zip"

    class Broken:
        def read(self, n):
            raise OSError(5, "Input/output error")

    with ZipBuilder(out) as builder:
        builder._write_file("broken.bin", Broken(), 10, (2020, 1, 1, 0, 0, 0), 0o644, "/broken.bin")
        builder.add_tree(good, ["good.txt"], "/good.txt", remove_after=False)
    assert builder.report.skipped[0].path == "/broken.bin"
    with zipfile.ZipFile(out) as zf:
        assert zf.testzip() is None
        assert zf.read("good.txt") == b"fine"
