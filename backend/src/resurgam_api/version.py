"""The running RESURGAM release version (ADR 0024).

Resolution order: the `RESURGAM_VERSION` environment variable (set by the
appliance's compose file to the image tag that is running), else the
repository-root `VERSION` file (a source checkout / local dev), else
"0.0.0-dev". Read on every call -- cheap, and it lets tests and a
restarted process pick up a changed value without a cache to invalidate.
"""

import os
import re
from pathlib import Path

FALLBACK_VERSION = "0.0.0-dev"
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([.+-][0-9A-Za-z.+-]+)?$")


def _version_file_candidates() -> list[Path]:
    # backend/src/resurgam_api/version.py -> the repo root is three levels
    # up from the package dir; check a few ancestors so an installed
    # layout (/app/backend/src/...) resolves the same way.
    here = Path(__file__).resolve()
    return [parent / "VERSION" for parent in here.parents[1:6]]


def _read_version_file() -> str | None:
    for candidate in _version_file_candidates():
        try:
            text = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if _VERSION_RE.match(text):
            return text
    return None


def running_version() -> str:
    env_value = os.environ.get("RESURGAM_VERSION", "").strip()
    if env_value:
        return env_value
    return _read_version_file() or FALLBACK_VERSION
