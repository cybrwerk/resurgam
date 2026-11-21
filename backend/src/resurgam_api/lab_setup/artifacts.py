"""The Recovery Lab packages shipped inside the backend image.

backend/Dockerfile's `lab-packages` stage builds `resurgam-lab-agent` and
`resurgam-lab-prober` from `restore-worker/` and writes `manifest.json`
(appliance/release/lab_artifacts_manifest.py) next to them in
`Settings.lab_artifacts_dir` (default `/app/artifacts`). Because the stage
runs on every image build, an update bundle (ADR 0024) always carries the
package versions that match its backend.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from resurgam_api.errors import ResurgamApiError

LAB_AGENT_ARTIFACT = "resurgam-lab-agent"
LAB_PROBER_ARTIFACT = "resurgam-lab-prober"
ARTIFACT_NAMES = (LAB_AGENT_ARTIFACT, LAB_PROBER_ARTIFACT)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+\-~]*\.deb$")


class LabArtifactsMissingError(Exception):
    """No (usable) lab package manifest in the artifacts directory."""


@dataclass(frozen=True)
class LabArtifact:
    name: str
    version: str
    architecture: str
    filename: str
    sha256: str
    size: int
    path: Path


@dataclass(frozen=True)
class LabArtifactManifest:
    release_version: str
    debian_release: str
    debian_version_id: str
    artifacts: dict[str, LabArtifact]

    def get(self, name: str) -> LabArtifact:
        artifact = self.artifacts.get(name)
        if artifact is None:
            raise LabArtifactsMissingError(f"the manifest lists no '{name}' package")
        return artifact


def load_manifest(directory: str) -> LabArtifactManifest:
    """Reads and checks `manifest.json`. Every listed file must exist with
    the listed size; a hand-edited or truncated directory is reported, not
    served."""
    base = Path(directory)
    manifest_path = base / "manifest.json"
    try:
        raw = json.loads(manifest_path.read_text())
    except FileNotFoundError as exc:
        raise LabArtifactsMissingError(f"{manifest_path} does not exist") from exc
    except (OSError, ValueError) as exc:
        raise LabArtifactsMissingError(f"{manifest_path} is unreadable: {exc}") from exc

    artifacts: dict[str, LabArtifact] = {}
    try:
        for entry in raw["artifacts"]:
            name = str(entry["name"])
            filename = str(entry["filename"])
            sha256 = str(entry["sha256"])
            if not _FILENAME.match(filename) or not _SHA256.match(sha256):
                raise LabArtifactsMissingError(f"manifest entry for '{name}' is malformed")
            path = base / filename
            size = int(entry["size"])
            if not path.is_file() or path.stat().st_size != size:
                raise LabArtifactsMissingError(f"{path} is missing or does not match the manifest")
            artifacts[name] = LabArtifact(
                name=name,
                version=str(entry["version"]),
                architecture=str(entry.get("architecture", "")),
                filename=filename,
                sha256=sha256,
                size=size,
                path=path,
            )
        return LabArtifactManifest(
            release_version=str(raw.get("release_version", "")),
            debian_release=str(raw.get("debian_release", "")),
            debian_version_id=str(raw.get("debian_version_id", "")),
            artifacts=artifacts,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LabArtifactsMissingError(f"{manifest_path} has an unexpected shape: {exc}") from exc


def try_load_manifest(directory: str) -> LabArtifactManifest | None:
    try:
        return load_manifest(directory)
    except LabArtifactsMissingError:
        return None


def artifacts_missing_error(exc: Exception) -> ResurgamApiError:
    return ResurgamApiError(
        code="lab_artifacts_missing",
        message=f"This installation ships no Recovery Lab packages ({exc}). They are built into the backend image "
        "(backend/Dockerfile, stage 'lab-packages'); outside the image build them with `dpkg-buildpackage -us -uc "
        "-b` in restore-worker/ on Debian 13 and point LAB_ARTIFACTS_DIR at a directory holding the .debs and the "
        "manifest.json written by appliance/release/lab_artifacts_manifest.py.",
        status_code=404,
    )


def require_manifest(directory: str) -> LabArtifactManifest:
    try:
        return load_manifest(directory)
    except LabArtifactsMissingError as exc:
        raise artifacts_missing_error(exc) from exc


def require_artifact(directory: str, name: str) -> LabArtifact:
    manifest = require_manifest(directory)
    try:
        return manifest.get(name)
    except LabArtifactsMissingError as exc:
        raise artifacts_missing_error(exc) from exc
