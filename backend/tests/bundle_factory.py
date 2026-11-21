"""Builds small but structurally real update bundles for the updates tests (throwaway keys only)."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from resurgam_api.updates import bundle

COMPOSE_YML = "services: {}\n"
UNIT = "[Service]\nExecStart=/opt/venvs/resurgam-backend/bin/resurgam-collector-scheduler\n"


def make_key_pair(directory: Path, name: str = "release") -> tuple[Path, Path]:
    private_pem, public_pem = bundle.generate_key_pair()
    key, pub = directory / f"{name}.key", directory / f"{name}.pub"
    key.write_bytes(private_pem)
    pub.write_bytes(public_pem)
    return key, pub


def _source_tar(version: str, extra_files: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, text in {
            "VERSION": f"{version}\n",
            "appliance/docker-compose.yml": COMPOSE_YML,
            "backend/packaging/systemd/resurgam-collector-scheduler.service": UNIT + f"# {version}\n",
            **(extra_files or {}),
        }.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def default_members(version: str, *, source_files: dict[str, str] | None = None) -> dict[str, bytes]:
    """``source_files`` adds files to source.tar.gz, e.g. {"appliance/release/host-setup.sh": "..."}."""
    return {
        "images/backend.tar": f"backend image {version}".encode(),
        "images/web.tar": f"web image {version}".encode(),
        "source.tar.gz": _source_tar(version, source_files),
        "CHANGELOG.md": f"## {version}\n- things\n".encode(),
        "wheels/requirements.txt": b"resurgam-api==0.1.0\n",
        "wheels/resurgam_api-0.1.0-py3-none-any.whl": b"not really a wheel",
    }


def host_package_members(names: tuple[str, ...] = ("pve-qemu-kvm", "proxmox-backup-client")) -> dict[str, bytes]:
    """A tiny but consistent host-packages/ flat repository (fake .debs, real Packages index) + list."""
    members: dict[str, bytes] = {}
    stanzas = []
    for name in names:
        data = f"fake deb {name}".encode()
        filename = f"{name}_1.0-1_amd64.deb"
        members[f"host-packages/{filename}"] = data
        stanzas.append(
            f"Package: {name}\nVersion: 1.0-1\nArchitecture: amd64\nFilename: ./{filename}\n"
            f"Size: {len(data)}\nSHA256: {_sha(data)}\nDescription: test\n continued line\n"
        )
    members["host-packages/Packages"] = "\n".join(stanzas).encode()
    members["host-packages.list"] = ("# host packages\n" + "\n".join(names) + "\n").encode()
    return members


def write_tar(path: Path, members: list[tuple[str, bytes]]) -> Path:
    with tarfile.open(path, "w") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def make_bundle(
    path: Path,
    key_path: Path,
    *,
    version: str = "0.2.1",
    min_upgrade_from: str = "0.2.0",
    members: dict[str, bytes] | None = None,
    tamper: dict[str, bytes] | None = None,
    extra: dict[str, bytes] | None = None,
) -> Path:
    """Signed bundle; ``tamper`` replaces member bytes AFTER signing, ``extra`` adds unlisted members."""
    members = members if members is not None else default_members(version)
    components = {name: bundle.Component(sha256=_sha(data), size=len(data)) for name, data in members.items()}
    manifest = bundle.build_manifest_bytes(
        version=version, min_upgrade_from=min_upgrade_from, changelog="- things", components=components
    )
    signature = bundle.sign_manifest(manifest, bundle.load_private_key(key_path.read_bytes()))
    content = {**members, **(tamper or {}), **(extra or {})}
    return write_tar(
        path,
        [(bundle.MANIFEST_NAME, manifest), (bundle.SIGNATURE_NAME, signature), *content.items()],
    )


def _sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()
