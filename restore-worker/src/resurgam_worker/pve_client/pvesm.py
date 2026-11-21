from __future__ import annotations

import shutil
import subprocess

from resurgam_worker._subprocess import run_subprocess
from resurgam_worker.pve_client.errors import PveBinaryNotFoundError, PveCommandFailedError

_PVESM_BINARY = "pvesm"
_COMMAND_TIMEOUT_SECONDS = 10


def _resolve_binary(binary: str) -> str:
    binary_path = shutil.which(binary)
    if binary_path is None:
        raise PveBinaryNotFoundError(binary)
    return binary_path


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return run_subprocess(command, timeout=_COMMAND_TIMEOUT_SECONDS, on_timeout=PveCommandFailedError)


def resolve_storage_path(volume_id: str, *, binary: str = _PVESM_BINARY) -> str:
    """`pvesm path <storage>:<content-type>/<file>` -- resolves a PVE
    storage-relative volume id to its real local filesystem path.
    Recovery Lab Phase B uses this for the probe appliance's cloud-init
    snippet (`<storage>:snippets/<file>`): confirmed live (2026-09-22)
    that `pvesm path` resolves this correctly even for a file that
    doesn't exist yet (`resurgam-lab-agent` writes it there itself --
    PVE has no REST endpoint to upload a snippet file, confirmed live
    the same day: the upload API's `content` param rejects "snippets").
    """
    binary_path = _resolve_binary(binary)
    command = [binary_path, "path", volume_id]
    result = _run(command)
    if result.returncode != 0:
        raise PveCommandFailedError(command, result.returncode, result.stderr)
    return result.stdout.strip()
