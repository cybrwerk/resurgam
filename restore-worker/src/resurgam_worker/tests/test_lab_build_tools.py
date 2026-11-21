"""The template builds' tool preflight (`cmd_check_tools`) under the shells
that really run it: the agent's `/bin/sh` (dash on Debian/PVE) and bash
(the SSH path). Found live 2026-11-20: `command -v a b c` in dash only
reports `a`, so the old check failed every agent-run build."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from resurgam_worker.lab_build.probe_template import PROBE_TEMPLATE_TOOLS, cmd_check_tools

SHELLS = [s for s in ("dash", "bash", "sh") if shutil.which(s)]


def _run(shell: str, command: str) -> subprocess.CompletedProcess:
    return subprocess.run([shell, "-c", command], capture_output=True, text=True, timeout=10, check=False)


@pytest.mark.parametrize("shell", SHELLS)
def test_all_tools_present_passes(shell):
    result = _run(shell, cmd_check_tools(("sh", "ls", "cat")))
    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.parametrize("shell", SHELLS)
def test_missing_tools_are_named(shell):
    result = _run(shell, cmd_check_tools(("sh", "resurgam-no-such-tool-a", "ls", "resurgam-no-such-tool-b")))
    assert result.returncode == 1
    assert result.stdout.strip() == "missing: resurgam-no-such-tool-a resurgam-no-such-tool-b"


def test_probe_template_needs_genisoimage():
    assert "genisoimage" in PROBE_TEMPLATE_TOOLS
