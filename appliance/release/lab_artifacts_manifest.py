"""Writes `manifest.json` for the Recovery Lab packages the backend image
ships (backend/Dockerfile, stage `lab-packages`): every `.deb` in a
directory with its package name, version, architecture, SHA-256 and size,
plus the Debian release it was built on.

The backend reads this file (`resurgam_api.lab_setup.artifacts`) to serve
the packages for download, to install the lab agent over SSH and to build
the probe appliance template -- so the version RESURGAM hands out is always
the one built from the same source tree as the backend itself.

Also enforces the versioning rule (see restore-worker/README.md):
`restore-worker/debian/changelog`'s upstream version must equal the
repo-root VERSION; only the Debian revision (`-N`) may differ.

Usage: python3 lab_artifacts_manifest.py <deb-dir> <VERSION-file>
Stdlib only (runs in a plain debian:trixie build stage).
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path


def _deb_field(path: Path, field: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed binary + argument list, no shell
        ["dpkg-deb", "--field", str(path), field], check=True, capture_output=True, text=True
    ).stdout.strip()


def _os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return values


def upstream_version(debian_version: str) -> str:
    """`0.2.1-1` -> `0.2.1` (drops the Debian revision and any epoch)."""
    version = debian_version.split(":", 1)[-1]
    return version.rsplit("-", 1)[0] if "-" in version else version


def main(deb_dir: str, version_file: str) -> int:
    directory = Path(deb_dir)
    release_version = Path(version_file).read_text().strip()
    artifacts = []
    for deb in sorted(directory.glob("*.deb")):
        name = _deb_field(deb, "Package")
        version = _deb_field(deb, "Version")
        if upstream_version(version) != release_version:
            print(
                f"error: {deb.name} has version {version}, but VERSION is {release_version}. Add a "
                f"restore-worker/debian/changelog entry '({release_version}-1)' (dch -v {release_version}-1).",
                file=sys.stderr,
            )
            return 1
        artifacts.append(
            {
                "name": name,
                "version": version,
                "architecture": _deb_field(deb, "Architecture"),
                "filename": deb.name,
                "sha256": hashlib.sha256(deb.read_bytes()).hexdigest(),
                "size": deb.stat().st_size,
            }
        )
    if not artifacts:
        print(f"error: no .deb files in {directory}", file=sys.stderr)
        return 1
    os_release = _os_release()
    manifest = {
        "schema": 1,
        "release_version": release_version,
        "debian_release": os_release.get("VERSION_CODENAME", ""),
        "debian_version_id": os_release.get("VERSION_ID", ""),
        "artifacts": artifacts,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
