"""Release-bundle format: manifest parsing, Ed25519 signatures, component hashes, version rules.

Pure functions only (file I/O, no subprocesses, no FastAPI/DB imports) -- shared by the
release builder (appliance/release/make_bundle.py), the host updater (``updater.py``) and the
API's upload pre-check. See docs/adr/0024-appliance-updates-signed-bundles.md for the format:

    resurgam-<version>.bundle    uncompressed tar
      manifest.json              {format, version, created_at, min_upgrade_from, changelog,
                                  components: {<path>: {sha256, size}}}
      manifest.json.sig          raw 64-byte Ed25519 signature over manifest.json's exact bytes
      <every component path>     bound to the signed manifest by its SHA-256 and size
      host-packages.list         optional (since 0.2.2): Debian packages the release needs on the host
      host-packages/             a flat apt repository (Packages index + .debs) providing them offline

Every check raises ``BundleError`` with a message that says what is wrong and what to do.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

BUNDLE_FORMAT = 1
MANIFEST_NAME = "manifest.json"
SIGNATURE_NAME = "manifest.json.sig"
# Members every bundle must carry (wheels/ additionally needs at least one *.whl).
REQUIRED_COMPONENTS = (
    "images/backend.tar",
    "images/web.tar",
    "source.tar.gz",
    "CHANGELOG.md",
    "wheels/requirements.txt",
)
# Host packages (ADR 0024, "Host packages and the host-setup hook"): optional, but all-or-nothing.
HOST_PACKAGES_LIST = "host-packages.list"
HOST_PACKAGES_DIR = "host-packages"
HOST_PACKAGES_INDEX = "host-packages/Packages"
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_PACKAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CHUNK = 1024 * 1024


class BundleError(Exception):
    """A bundle (or a version pair) failed validation. ``str(err)`` is operator-facing."""


@dataclass(frozen=True)
class Component:
    sha256: str
    size: int


@dataclass(frozen=True)
class Manifest:
    format: int
    version: str
    created_at: str
    min_upgrade_from: str
    changelog: str
    components: dict[str, Component] = field(default_factory=dict)

    @property
    def has_host_packages(self) -> bool:
        return HOST_PACKAGES_LIST in self.components

    def summary(self) -> dict[str, str]:
        """The ``manifest`` object of the status-file contract (ADR 0024)."""
        return {"version": self.version, "changelog": self.changelog, "min_upgrade_from": self.min_upgrade_from}


# --- versions -------------------------------------------------------------------------------


def parse_version(value: str) -> tuple[int, int, int]:
    """``"1.2.3"`` -> ``(1, 2, 3)``. Only plain MAJOR.MINOR.PATCH is a release version."""
    match = _VERSION_RE.match(value.strip()) if isinstance(value, str) else None
    if not match:
        raise BundleError(f"'{value}' is not a release version -- expected MAJOR.MINOR.PATCH, e.g. 0.2.1.")
    return (int(match[1]), int(match[2]), int(match[3]))


def compare_versions(a: str, b: str) -> int:
    """-1 if a < b, 0 if equal, 1 if a > b (numeric per component, so 0.10.0 > 0.9.9)."""
    pa, pb = parse_version(a), parse_version(b)
    return (pa > pb) - (pa < pb)


def check_upgrade_allowed(manifest: Manifest, current_version: str) -> None:
    """The bundle must be newer than what runs, and what runs must be >= its min_upgrade_from."""
    try:
        parse_version(current_version)
    except BundleError as exc:
        raise BundleError(
            f"The running version '{current_version}' is not a release version, so no upgrade path can be "
            "checked. Updates only apply to an installed appliance release."
        ) from exc
    if compare_versions(manifest.version, current_version) <= 0:
        raise BundleError(
            f"Bundle version {manifest.version} is not newer than the running version {current_version}. "
            "Downgrades and re-installs are not supported -- use a bundle with a higher version."
        )
    if compare_versions(current_version, manifest.min_upgrade_from) < 0:
        raise BundleError(
            f"Bundle {manifest.version} requires at least version {manifest.min_upgrade_from} to upgrade from, "
            f"but {current_version} is running. Apply the intermediate release(s) first."
        )


# --- keys and signatures --------------------------------------------------------------------


def generate_key_pair() -> tuple[bytes, bytes]:
    """New Ed25519 key pair as (private PKCS#8 PEM, public SubjectPublicKeyInfo PEM)."""
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return private_pem, public_key_pem(key.public_key())


def public_key_pem(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def load_private_key(data: bytes) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError) as exc:
        raise BundleError("The release signing key is not an unencrypted PEM private key.") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise BundleError("The release signing key is not an Ed25519 key.")
    return key


def load_public_key(data: bytes) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(data)
    except (ValueError, TypeError) as exc:
        raise BundleError("The release public key is not a PEM public key (expected release-signing.pub).") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise BundleError("The release public key is not an Ed25519 key.")
    return key


def load_public_key_file(path: str | os.PathLike[str]) -> Ed25519PublicKey:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise BundleError(f"Cannot read the release public key at {path}: {exc.strerror}.") from exc
    return load_public_key(data)


def sign_manifest(manifest_bytes: bytes, private_key: Ed25519PrivateKey) -> bytes:
    return private_key.sign(manifest_bytes)


def verify_signature(manifest_bytes: bytes, signature: bytes, public_key: Ed25519PublicKey) -> None:
    try:
        public_key.verify(signature, manifest_bytes)
    except InvalidSignature as exc:
        raise BundleError(
            "The bundle signature does not match this appliance's release key -- the bundle was modified or "
            "signed by someone else. Only install bundles from the official RESURGAM release builder."
        ) from exc


# --- manifest -------------------------------------------------------------------------------


def validate_component_path(path: str) -> str:
    """Relative, forward-slash, no '.'/'..' segments, no hidden/odd names -- safe to join under a directory."""
    if not isinstance(path, str) or not path or len(path) > 255:
        raise BundleError(f"Invalid component path {path!r} in the manifest.")
    segments = path.split("/")
    if any(not _PATH_SEGMENT_RE.match(seg) or seg in (".", "..") for seg in segments):
        raise BundleError(f"Invalid component path {path!r} in the manifest (must be a plain relative path).")
    if path in (MANIFEST_NAME, SIGNATURE_NAME):
        raise BundleError(f"{path} cannot be listed as a component of itself.")
    return path


def build_manifest_bytes(
    *,
    version: str,
    min_upgrade_from: str,
    changelog: str,
    components: dict[str, Component],
    created_at: str | None = None,
) -> bytes:
    """Canonical manifest.json bytes (the exact bytes that get signed)."""
    parse_version(version)
    parse_version(min_upgrade_from)
    doc = {
        "format": BUNDLE_FORMAT,
        "version": version,
        "created_at": created_at or datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat(),
        "min_upgrade_from": min_upgrade_from,
        "changelog": changelog,
        "components": {
            validate_component_path(p): {"sha256": c.sha256, "size": c.size} for p, c in sorted(components.items())
        },
    }
    data = (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    parse_manifest(data)  # never emit something the updater would reject
    return data


def parse_manifest(data: bytes) -> Manifest:
    """Validate manifest.json's structure. Call only on bytes whose signature was verified."""
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"manifest.json is not valid UTF-8 JSON: {exc}.") from exc
    if not isinstance(doc, dict):
        raise BundleError("manifest.json must be a JSON object.")
    if doc.get("format") != BUNDLE_FORMAT:
        raise BundleError(
            f"Unsupported bundle format {doc.get('format')!r} (this updater understands format {BUNDLE_FORMAT}). "
            "Update to an intermediate release that understands the newer format first."
        )
    for key in ("version", "created_at", "min_upgrade_from", "changelog"):
        if not isinstance(doc.get(key), str):
            raise BundleError(f"manifest.json field '{key}' is missing or not a string.")
    parse_version(doc["version"])
    parse_version(doc["min_upgrade_from"])
    if compare_versions(doc["min_upgrade_from"], doc["version"]) > 0:
        raise BundleError("manifest.json min_upgrade_from is newer than the bundle's own version.")
    try:
        datetime.datetime.fromisoformat(doc["created_at"])
    except ValueError as exc:
        raise BundleError("manifest.json created_at is not an ISO 8601 timestamp.") from exc
    raw = doc.get("components")
    if not isinstance(raw, dict) or not raw:
        raise BundleError("manifest.json has no components.")
    components: dict[str, Component] = {}
    for path, entry in raw.items():
        validate_component_path(path)
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("sha256"), str)
            or not _SHA256_RE.match(entry["sha256"])
            or not isinstance(entry.get("size"), int)
            or isinstance(entry.get("size"), bool)
            or entry["size"] < 0
        ):
            raise BundleError(f"manifest.json entry for {path} needs a lowercase hex sha256 and a size in bytes.")
        components[path] = Component(sha256=entry["sha256"], size=entry["size"])
    missing = [p for p in REQUIRED_COMPONENTS if p not in components]
    if missing:
        raise BundleError(f"The bundle is incomplete: missing {', '.join(missing)}.")
    if not any(p.startswith("wheels/") and p.endswith(".whl") for p in components):
        raise BundleError("The bundle is incomplete: wheels/ contains no Python wheels for the native workers.")
    _check_host_package_components(components)
    return Manifest(
        format=doc["format"],
        version=doc["version"],
        created_at=doc["created_at"],
        min_upgrade_from=doc["min_upgrade_from"],
        changelog=doc["changelog"],
        components=components,
    )


def _check_host_package_components(components: dict[str, Component]) -> None:
    host = [p for p in components if p.startswith(f"{HOST_PACKAGES_DIR}/")]
    if not host and HOST_PACKAGES_LIST not in components:
        return
    if (
        HOST_PACKAGES_LIST not in components
        or HOST_PACKAGES_INDEX not in components
        or not any(p.endswith(".deb") for p in host)
    ):
        raise BundleError(
            f"The bundle's host packages are incomplete: it needs {HOST_PACKAGES_LIST}, {HOST_PACKAGES_INDEX} and "
            "at least one .deb. Rebuild it with appliance/release/build-bundle.sh."
        )
    nested = [p for p in host if p.count("/") != 1]
    if nested:
        raise BundleError(f"{HOST_PACKAGES_DIR}/ must be a flat repository, but contains {', '.join(nested[:3])}.")


# --- host packages --------------------------------------------------------------------------


@dataclass(frozen=True)
class HostPackage:
    """One stanza of host-packages/Packages."""

    package: str
    version: str
    filename: str
    size: int
    sha256: str


def parse_package_list(text: str) -> list[str]:
    """host-packages.list: one Debian package name per line; blank lines and '#' comments ignored."""
    names: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if not _PACKAGE_NAME_RE.match(line):
            raise BundleError(f"{HOST_PACKAGES_LIST}: {line!r} is not a Debian package name.")
        if line not in names:
            names.append(line)
    if not names:
        raise BundleError(f"{HOST_PACKAGES_LIST} lists no packages.")
    return names


def _stanzas(text: str) -> Iterator[dict[str, str]]:
    fields: dict[str, str] = {}
    key = None
    for line in [*text.splitlines(), ""]:
        if not line.strip():
            if fields:
                yield fields
            fields, key = {}, None
        elif line[0] in " \t" and key is not None:
            fields[key] += "\n" + line.strip()
        elif ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()


def check_host_repo(repo_dir: str | os.PathLike[str], package_names: list[str] | None = None) -> list[HostPackage]:
    """Validate a flat host-packages repository: every Packages stanza names a .deb in the directory whose
    size and SHA-256 match, every .deb is indexed, and every name in ``package_names`` is provided."""
    repo = Path(repo_dir)
    index = repo / "Packages"
    try:
        text = index.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        raise BundleError(f"{index} is missing or unreadable -- not a host-packages repository.") from exc
    packages: list[HostPackage] = []
    for fields in _stanzas(text):
        try:
            entry = HostPackage(
                package=fields["Package"],
                version=fields["Version"],
                filename=fields["Filename"].removeprefix("./"),
                size=int(fields["Size"]),
                sha256=fields["SHA256"],
            )
        except (KeyError, ValueError) as exc:
            raise BundleError(f"{index} has a stanza without Package/Version/Filename/Size/SHA256.") from exc
        if "/" in entry.filename or not _PATH_SEGMENT_RE.match(entry.filename):
            raise BundleError(f"{index}: Filename {entry.filename!r} must be a plain file in the repository.")
        deb = repo / entry.filename
        if not deb.is_file():
            raise BundleError(f"{index} lists {entry.filename}, which is not in the repository.")
        if hash_file(deb) != Component(sha256=entry.sha256, size=entry.size):
            raise BundleError(f"{entry.filename} does not match its Packages entry (size/SHA-256 differ).")
        packages.append(entry)
    indexed = {p.filename for p in packages}
    unindexed = sorted(p.name for p in repo.glob("*.deb") if p.name not in indexed)
    if unindexed:
        raise BundleError(f"{index} does not list {', '.join(unindexed[:3])} -- regenerate the index.")
    provided = {p.package for p in packages}
    absent = [name for name in package_names or [] if name not in provided]
    if absent:
        raise BundleError(f"The host-packages repository does not provide {', '.join(absent)}.")
    return packages


# --- hashing --------------------------------------------------------------------------------


def hash_file(path: str | os.PathLike[str]) -> Component:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return Component(sha256=digest.hexdigest(), size=size)


def _copy_hashing(src: IO[bytes], dst: IO[bytes] | None) -> Component:
    digest = hashlib.sha256()
    size = 0
    while chunk := src.read(_CHUNK):
        digest.update(chunk)
        size += len(chunk)
        if dst is not None:
            dst.write(chunk)
    return Component(sha256=digest.hexdigest(), size=size)


# --- bundles --------------------------------------------------------------------------------


def _open_bundle(path: str | os.PathLike[str]) -> tarfile.TarFile:
    try:
        return tarfile.open(path, mode="r:")  # uncompressed only, per the format
    except FileNotFoundError as exc:
        raise BundleError(f"Bundle file {path} does not exist.") from exc
    except (tarfile.TarError, OSError) as exc:
        raise BundleError(f"{path} is not a RESURGAM bundle (not an uncompressed tar archive): {exc}.") from exc


def _members(tf: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members: dict[str, tarfile.TarInfo] = {}
    try:
        for info in tf:
            name = info.name.removeprefix("./")
            if info.isdir():
                continue
            if name in members:
                raise BundleError(f"The bundle contains {name} twice -- refusing an ambiguous bundle.")
            if not info.isreg():
                raise BundleError(f"The bundle member {name} is not a regular file (links/devices are not allowed).")
            members[name] = info
    except tarfile.TarError as exc:
        raise BundleError(f"The bundle archive is truncated or corrupt: {exc}. Upload it again.") from exc
    return members


def _read_small(tf: tarfile.TarFile, info: tarfile.TarInfo | None, name: str) -> bytes:
    if info is None:
        raise BundleError(f"The bundle has no {name} -- it is not a signed RESURGAM release bundle.")
    if info.size > _MAX_MANIFEST_BYTES:
        raise BundleError(f"{name} in the bundle is implausibly large.")
    fh = tf.extractfile(info)
    assert fh is not None  # isreg() checked in _members
    return fh.read()


def _verified_manifest(
    tf: tarfile.TarFile, public_key: Ed25519PublicKey
) -> tuple[Manifest, dict[str, tarfile.TarInfo]]:
    members = _members(tf)
    manifest_bytes = _read_small(tf, members.get(MANIFEST_NAME), MANIFEST_NAME)
    signature = _read_small(tf, members.get(SIGNATURE_NAME), SIGNATURE_NAME)
    verify_signature(manifest_bytes, signature, public_key)
    manifest = parse_manifest(manifest_bytes)
    unexpected = sorted(set(members) - set(manifest.components) - {MANIFEST_NAME, SIGNATURE_NAME})
    if unexpected:
        raise BundleError(f"The bundle contains files the signed manifest does not list: {', '.join(unexpected)}.")
    missing = sorted(set(manifest.components) - set(members))
    if missing:
        raise BundleError(f"The bundle is missing files its manifest lists: {', '.join(missing)}.")
    return manifest, members


def _check_component(path: str, expected: Component, actual: Component) -> None:
    if actual != expected:
        raise BundleError(
            f"Component {path} does not match the signed manifest (sha256/size differ) -- the bundle was "
            "corrupted or tampered with. Download/upload it again."
        )


def _component_streams(
    tf: tarfile.TarFile, manifest: Manifest, members: dict[str, tarfile.TarInfo]
) -> Iterator[tuple[str, Component, IO[bytes]]]:
    for path in sorted(manifest.components):
        info = members[path]
        if info.size != manifest.components[path].size:
            _check_component(path, manifest.components[path], Component(sha256="", size=info.size))
        fh = tf.extractfile(info)
        assert fh is not None
        yield path, manifest.components[path], fh


def read_manifest_unverified(bundle_path: str | os.PathLike[str]) -> Manifest:
    """Parse manifest.json WITHOUT checking the signature -- for display only, never for trust decisions."""
    with _open_bundle(bundle_path) as tf:
        members = _members(tf)
        return parse_manifest(_read_small(tf, members.get(MANIFEST_NAME), MANIFEST_NAME))


def verify_bundle(
    bundle_path: str | os.PathLike[str],
    public_key: Ed25519PublicKey,
    current_version: str | None = None,
    *,
    check_hashes: bool = True,
) -> Manifest:
    """Signature + (optionally) every component hash, without extracting; plus version rules if given.

    ``check_hashes=False`` is a fast pre-check (signature and structure only) -- the host updater
    always runs the full check on its own extracted copy (``extract_bundle``).
    """
    with _open_bundle(bundle_path) as tf:
        manifest, members = _verified_manifest(tf, public_key)
        if check_hashes:
            for path, expected, fh in _component_streams(tf, manifest, members):
                _check_component(path, expected, _copy_hashing(fh, None))
    if current_version is not None:
        check_upgrade_allowed(manifest, current_version)
    return manifest


def extract_bundle(
    bundle_path: str | os.PathLike[str],
    dest_dir: str | os.PathLike[str],
    public_key: Ed25519PublicKey,
) -> Manifest:
    """Verify and extract every component into ``dest_dir`` (must be empty or absent).

    The hashes are computed over the very bytes written, so the extracted tree is exactly what
    the release key signed -- later changes to the bundle file itself cannot affect it.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        raise BundleError(f"Extraction directory {dest} is not empty.")
    with _open_bundle(bundle_path) as tf:
        manifest, members = _verified_manifest(tf, public_key)
        (dest / MANIFEST_NAME).write_bytes(_read_small(tf, members[MANIFEST_NAME], MANIFEST_NAME))
        for path, expected, fh in _component_streams(tf, manifest, members):
            target = dest / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as out:
                actual = _copy_hashing(fh, out)
            _check_component(path, expected, actual)
    return manifest
