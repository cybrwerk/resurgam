from datetime import UTC, datetime

import pytest

from resurgam_worker.lab_agent import file_index

# --- build_enumeration_command --------------------------------------------------


def test_build_enumeration_command_linux_no_since():
    command = file_index.build_enumeration_command("l26")
    assert command[0] == "/bin/sh"
    assert command[1] == "-c"
    assert "find / -xdev" in command[2]
    assert "-newermt" not in command[2]
    assert "resurgam-index-chunk-" in command[2]
    assert "resurgam-index-manifest.txt" in command[2]


def test_build_enumeration_command_linux_with_since():
    since = datetime(2026, 9, 1, tzinfo=UTC)
    command = file_index.build_enumeration_command("l26", since=since)
    assert "-newermt" in command[2]
    assert str(int(since.timestamp())) in command[2]


def test_build_enumeration_command_linux_chunks_output_via_split():
    command = file_index.build_enumeration_command("l26")
    script = command[2]
    assert "split -d -a 4" in script
    # The split invocation must come after find's own pipeline, and the
    # manifest (chunk count) must be written last -- the read side relies
    # on the manifest only appearing once every chunk file is complete.
    assert script.index("find /") < script.index("split")
    assert script.index("split") < script.index("wc -l >")


def test_build_enumeration_command_windows_no_since():
    command = file_index.build_enumeration_command("win10")
    assert command[0] == "powershell.exe"
    assert "Get-ChildItem" in command[-1]
    assert "Where-Object" not in command[-1]


def test_build_enumeration_command_windows_with_since():
    since = datetime(2026, 9, 1, tzinfo=UTC)
    command = file_index.build_enumeration_command("win11", since=since)
    assert f"$since = {int(since.timestamp())}" in command[-1]


def test_build_enumeration_command_windows_no_since_uses_null():
    command = file_index.build_enumeration_command("win10")
    assert "$since = $null" in command[-1]


def test_build_enumeration_command_windows_chunks_output():
    command = file_index.build_enumeration_command("win10")
    script = command[-1]
    assert "$chunkPrefix" in script
    assert "NextChunk" in script
    assert "resurgam-index-manifest.txt" in script
    # The manifest write must be the very last statement -- the read
    # side treats its existence as proof every chunk file is closed.
    assert script.rstrip().endswith("'C:\\Windows\\Temp\\resurgam-index-manifest.txt'")


def test_build_enumeration_command_unsupported_ostype_raises():
    with pytest.raises(file_index.UnsupportedOsTypeError):
        file_index.build_enumeration_command("solaris")


# --- exclude_patterns pruning (2026-09-24) --------------------------------------
#
# A real, decisive gap found live: the enumeration command had no
# awareness of exclude patterns at all, walking (and transferring back)
# the entire drive unconditionally -- confirmed live to produce a 95MB
# listing and a guest_exec call that ran 9+ minutes unattended against a
# real production VM. Patterns now prune matching subtrees during the
# walk itself.


def test_build_enumeration_command_linux_no_excludes_has_no_prune_clause():
    command = file_index.build_enumeration_command("l26")
    assert "-prune" not in command[2]


def test_build_enumeration_command_linux_prunes_excluded_paths():
    command = file_index.build_enumeration_command("l26", exclude_patterns=[r"(^|/)var/lib/docker(/|$)"])
    script = command[2]
    assert "-regextype posix-extended" in script
    assert "\\( -iregex '(^|/)var/lib/docker(/|$)' \\) -prune -o" in script
    # The prune clause must come before -printf, or find would print
    # excluded paths before ever reaching the prune test.
    assert script.index("-prune") < script.index("-printf")


def test_build_enumeration_command_linux_prunes_multiple_patterns_with_or():
    command = file_index.build_enumeration_command(
        "l26", exclude_patterns=[r"(^|/)var/lib/docker(/|$)", r"(^|/)snap(/|$)"]
    )
    script = command[2]
    assert "-iregex '(^|/)var/lib/docker(/|$)' -o -iregex '(^|/)snap(/|$)'" in script


def test_build_enumeration_command_linux_escapes_single_quote_in_pattern():
    """A real, if narrow, security property: exclude patterns are
    operator-editable content (`BackupGroup.file_index_exclude_patterns`),
    not a trusted literal -- naive interpolation here would let a
    crafted pattern break out of the shell string and inject arbitrary
    commands into what's otherwise a fixed, project-authored script.
    """
    command = file_index.build_enumeration_command("l26", exclude_patterns=["it's/a/test"])
    script = command[2]
    assert "'it'\\''s/a/test'" in script


def test_build_enumeration_command_windows_no_excludes_empty_array():
    command = file_index.build_enumeration_command("win10")
    assert "$exclude = @();" in command[-1]


def test_build_enumeration_command_windows_prunes_excluded_paths():
    command = file_index.build_enumeration_command("win10", exclude_patterns=[r"(^|/)Windows/WinSxS(/|$)"])
    script = command[-1]
    assert "$exclude = @('(^|/)Windows/WinSxS(/|$)');" in script
    assert "Test-Excluded" in script
    # Excluded items must be skipped (and not recursed into) before
    # anything is emitted -- not filtered out after a full walk.
    assert script.index("Test-Excluded $rel") < script.index("if ($_.PSIsContainer) { Walk")


def test_build_enumeration_command_windows_escapes_single_quote_in_pattern():
    command = file_index.build_enumeration_command("win10", exclude_patterns=["it's/a/test"])
    script = command[-1]
    assert "'it''s/a/test'" in script


def test_build_enumeration_command_windows_multiple_patterns_comma_joined():
    command = file_index.build_enumeration_command("win10", exclude_patterns=["pat1", "pat2"])
    assert "$exclude = @('pat1', 'pat2');" in command[-1]


# --- manifest/chunk path helpers (2026-09-25 chunking rewrite) -----------------


def test_manifest_path_for_ostype():
    assert file_index.manifest_path_for_ostype("l26") == "/tmp/resurgam-index-manifest.txt"
    assert file_index.manifest_path_for_ostype("win10") == "C:\\Windows\\Temp\\resurgam-index-manifest.txt"
    with pytest.raises(file_index.UnsupportedOsTypeError):
        file_index.manifest_path_for_ostype("other")


def test_chunk_path_for_ostype_zero_padded():
    assert file_index.chunk_path_for_ostype("l26", 0) == "/tmp/resurgam-index-chunk-0000"
    assert file_index.chunk_path_for_ostype("l26", 12) == "/tmp/resurgam-index-chunk-0012"
    assert file_index.chunk_path_for_ostype("win10", 0) == "C:\\Windows\\Temp\\resurgam-index-chunk-0000"
    with pytest.raises(file_index.UnsupportedOsTypeError):
        file_index.chunk_path_for_ostype("other", 0)


def test_parse_chunk_count_strips_whitespace():
    assert file_index.parse_chunk_count("3\n") == 3
    assert file_index.parse_chunk_count("7") == 7


# --- build_cleanup_command (2026-09-25) -----------------------------------------
#
# A real, decisive race found live: cleanup was originally the
# enumeration script's own first statement, which raced against the
# caller polling for the manifest to appear -- a fire-and-forget-launched
# enumeration hasn't necessarily reached its own cleanup line yet by the
# time the very first poll runs, so a *stale* manifest from the previous
# run could be found readable and mistaken for this run's fresh result.
# Cleanup is now its own separate, fast command the caller runs via the
# reliable polling `guest_exec` before ever dispatching the enumeration.


def test_build_cleanup_command_linux():
    command = file_index.build_cleanup_command("l26")
    assert command[0] == "/bin/sh"
    assert "rm -f" in command[2]
    assert "resurgam-index-chunk-" in command[2]
    assert "resurgam-index-manifest.txt" in command[2]


def test_build_cleanup_command_windows():
    command = file_index.build_cleanup_command("win10")
    assert command[0] == "powershell.exe"
    script = command[-1]
    assert "Remove-Item" in script
    assert "resurgam-index-chunk-" in script
    assert "resurgam-index-manifest.txt" in script


def test_build_cleanup_command_unsupported_ostype_raises():
    with pytest.raises(file_index.UnsupportedOsTypeError):
        file_index.build_cleanup_command("solaris")


def test_build_enumeration_command_no_longer_embeds_its_own_cleanup():
    """Cleanup moved out to `build_cleanup_command`/the caller -- the
    enumeration script itself must not also delete stale output, or the
    two would race against each other from opposite ends.
    """
    linux_script = file_index.build_enumeration_command("l26")[2]
    assert "rm -f" not in linux_script
    windows_script = file_index.build_enumeration_command("win10")[-1]
    assert "Remove-Item" not in windows_script


# --- parse_linux_find_output ----------------------------------------------------


def test_parse_linux_find_output_parses_files_dirs_symlinks():
    text = (
        "f\t1024\t1758534000.123456789\t/var/log/syslog\n"
        "d\t4096\t1758534000.0\t/var/log\n"
        "l\t0\t1758534000.0\t/etc/alternatives/editor\n"
    )
    entries = file_index.parse_linux_find_output(text)
    assert len(entries) == 3
    assert entries[0] == {
        "path": "/var/log/syslog",
        "filename": "syslog",
        "type": "file",
        "size_bytes": 1024,
        "mtime": datetime.fromtimestamp(1758534000.123456789, tz=UTC),
    }
    assert entries[1]["type"] == "directory"
    assert entries[1]["size_bytes"] is None
    assert entries[2]["type"] == "symlink"


def test_parse_linux_find_output_skips_malformed_lines():
    text = "f\t1024\t123.0\t/ok\nnot-enough-columns\n\n"
    entries = file_index.parse_linux_find_output(text)
    assert len(entries) == 1
    assert entries[0]["path"] == "/ok"


def test_parse_linux_find_output_unknown_type_char_is_other():
    entries = file_index.parse_linux_find_output("p\t0\t123.0\t/tmp/named-pipe\n")
    assert entries[0]["type"] == "other"


# --- parse_output_for_ostype ----------------------------------------------------
#
# Windows switched from `ConvertTo-Csv` to the same plain TSV shape as
# Linux in the 2026-09-25 chunking rewrite (StreamWriter-based chunked
# writes made keeping CSV's quoting machinery pointless) -- both OS
# families now share one parser.


def test_parse_output_for_ostype_dispatches_linux():
    text = "f\t4\t1758700800\t/etc/hostname\n"
    entries = file_index.parse_output_for_ostype("l26", text)
    assert entries[0]["path"] == "/etc/hostname"


def test_parse_output_for_ostype_dispatches_windows():
    text = "f\t2048\t1758700800\tC:\\Users\\admin\\file.txt\n"
    entries = file_index.parse_output_for_ostype("win10", text)
    assert entries[0]["path"] == "C:/Users/admin/file.txt"
    assert entries[0]["size_bytes"] == 2048


def test_parse_output_for_ostype_unsupported_raises():
    with pytest.raises(file_index.UnsupportedOsTypeError):
        file_index.parse_output_for_ostype("unknown-os", "")
