"""The guest-side file scan's lock (live finding 2026-11-21)."""

from resurgam_worker.lab_agent import file_index


def test_scan_takes_a_lock_first_and_cleanup_releases_it():
    """A repeated start of the same scan (retried after a guest-agent timeout)
    must exit at once instead of running a second scan side by side."""
    linux = file_index.build_enumeration_command("l26")[2]
    assert linux.startswith("mkdir /tmp/resurgam-index.lock 2>/dev/null || exit 0; ")
    assert "rmdir /tmp/resurgam-index.lock" in file_index.build_cleanup_command("l26")[2]
    windows = file_index.build_enumeration_command("win11")[4]
    assert windows.startswith("try { New-Item -ItemType Directory -Path 'C:\\Windows\\Temp\\resurgam-index.lock'")
    assert "resurgam-index.lock' -Recurse" in file_index.build_cleanup_command("win11")[4]


def test_linux_scan_lock_lets_only_the_first_start_through(tmp_path):
    import subprocess

    script = file_index.build_enumeration_command("l26")[2].replace("/tmp/resurgam-index.lock", str(tmp_path / "lock"))
    guarded = script.split("; ", 1)[0] + "; echo started"
    first = subprocess.run(["sh", "-c", guarded], capture_output=True, text=True, check=False)
    second = subprocess.run(["sh", "-c", guarded], capture_output=True, text=True, check=False)
    assert first.stdout.strip() == "started"
    assert second.returncode == 0 and second.stdout == ""
