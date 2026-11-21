"""Release-builder helper for build-bundle.sh (ADR 0024): signing key, changelog, manifest, bundle tar.

Run through the backend's own environment so it shares the exact verification code the
appliance uses (``resurgam_api.updates.bundle``):

    uv run --project backend --frozen python appliance/release/make_bundle.py <command> ...

Commands:
    ensure-key  --key K --pub P        create the Ed25519 key pair on first run; check K matches P
    changelog   --file CHANGELOG.md --version V
    default-min-upgrade-from --version V
    pack        --staging DIR --version V --min-upgrade-from M --key K --out FILE

Never prints private key material.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tarfile
from pathlib import Path

from resurgam_api.updates import bundle


def ensure_key(key_path: Path, pub_path: Path, *, allow_foreign: bool) -> None:
    if not key_path.exists():
        if pub_path.exists() and not allow_foreign:
            sys.exit(
                f"error: {key_path} does not exist, but {pub_path} already holds a release public key.\n"
                "Appliances in the field trust that key -- a new key would make every bundle you build "
                "unverifiable for them. Restore the private key from your backup. Only if it is truly lost, "
                f"delete {pub_path} to create a new pair (every appliance then needs the new public key)."
            )
        key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        private_pem, public_pem = bundle.generate_key_pair()
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(private_pem)
        os.chmod(key_path, 0o600)
        print(f"Created a new release signing key at {key_path} (mode 600) -- BACK IT UP somewhere safe.")
        if not allow_foreign:
            pub_path.write_bytes(public_pem)
            print(f"Wrote the public key to {pub_path} -- commit it.")
        return
    mode = key_path.stat().st_mode & 0o777
    if mode & 0o077:
        sys.exit(f"error: {key_path} is readable by others (mode {mode:o}); run: chmod 600 {key_path}")
    key = bundle.load_private_key(key_path.read_bytes())
    public_pem = bundle.public_key_pem(key.public_key())
    if allow_foreign:
        return
    if not pub_path.exists():
        pub_path.write_bytes(public_pem)
        print(f"Wrote the public key to {pub_path} -- commit it.")
    elif pub_path.read_bytes().strip() != public_pem.strip():
        sys.exit(
            f"error: {key_path} does not match the committed public key {pub_path}. "
            "Bundles signed with it would be rejected by every appliance. Use the right key "
            "(RESURGAM_SIGNING_KEY=...) or, for a deliberate test with a foreign key, ALLOW_FOREIGN_KEY=1."
        )


def changelog_excerpt(changelog: str, version: str) -> str:
    """The '## <version>' section of CHANGELOG.md (heading line excluded), stripped."""
    pattern = re.compile(rf"^##\s+\[?{re.escape(version)}\]?(?:\s.*)?$", re.MULTILINE)
    match = pattern.search(changelog)
    if not match:
        sys.exit(f"error: CHANGELOG.md has no '## {version}' section -- add the release notes first.")
    rest = changelog[match.end() :]
    following = re.search(r"^##\s", rest, re.MULTILINE)
    return (rest[: following.start()] if following else rest).strip()


_WHEEL_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.]+?)-(?P<version>[^-]+)-.+\.whl$")


def write_wheel_requirements(wheels_dir: Path) -> None:
    """wheels/requirements.txt pinning exactly the wheels present (installed with --no-index)."""
    lines = []
    for wheel in sorted(wheels_dir.glob("*.whl")):
        match = _WHEEL_RE.match(wheel.name)
        if not match:
            sys.exit(f"error: unexpected wheel file name {wheel.name}")
        lines.append(f"{match['name'].replace('_', '-').lower()}=={match['version']}")
    if not lines:
        sys.exit(f"error: no wheels in {wheels_dir}")
    (wheels_dir / "requirements.txt").write_text("\n".join(lines) + "\n")


# The first release whose updater runs the new release's host-setup hook (ADR 0024). A bundle for a
# later release relies on that hook (e.g. to install its host packages), and an older updater would
# silently skip it -- so such bundles must not be applicable from anything older.
HOOK_UPDATER_VERSION = "0.2.2"


def default_min_upgrade_from(version: str) -> str:
    return HOOK_UPDATER_VERSION if bundle.compare_versions(version, HOOK_UPDATER_VERSION) > 0 else "0.2.0"


def check_host_packages(staging: Path) -> None:
    """Fail the build (not the appliance) if the collected host-packages repository is inconsistent."""
    package_list = staging / bundle.HOST_PACKAGES_LIST
    if not package_list.exists():
        return
    names = bundle.parse_package_list(package_list.read_text())
    debs = bundle.check_host_repo(staging / bundle.HOST_PACKAGES_DIR, names)
    print(f"Host packages: {', '.join(names)} ({len(debs)} .debs incl. dependencies).")


def pack(staging: Path, version: str, min_upgrade_from: str, key_path: Path, out: Path) -> None:
    key = bundle.load_private_key(key_path.read_bytes())
    write_wheel_requirements(staging / "wheels")
    check_host_packages(staging)
    changelog = changelog_excerpt((staging / "CHANGELOG.md").read_text(), version)
    components: dict[str, bundle.Component] = {}
    for path in sorted(p for p in staging.rglob("*") if p.is_file()):
        rel = path.relative_to(staging).as_posix()
        if rel in (bundle.MANIFEST_NAME, bundle.SIGNATURE_NAME):
            continue
        components[rel] = bundle.hash_file(path)
    manifest_bytes = bundle.build_manifest_bytes(
        version=version, min_upgrade_from=min_upgrade_from, changelog=changelog, components=components
    )
    (staging / bundle.MANIFEST_NAME).write_bytes(manifest_bytes)
    (staging / bundle.SIGNATURE_NAME).write_bytes(bundle.sign_manifest(manifest_bytes, key))

    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".partial")
    # Small members first, so manifest + signature are read without seeking through the images.
    order = [bundle.MANIFEST_NAME, bundle.SIGNATURE_NAME, *sorted(components, key=lambda p: (components[p].size, p))]
    with tarfile.open(partial, "w", format=tarfile.PAX_FORMAT) as tf:
        for rel in order:
            info = tf.gettarinfo(str(staging / rel), arcname=rel)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mode = 0o644
            with open(staging / rel, "rb") as fh:
                tf.addfile(info, fh)
    os.replace(partial, out)
    print(f"Built {out} ({out.stat().st_size // (1024 * 1024)} MiB, {len(components)} components).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ensure-key")
    p.add_argument("--key", type=Path, required=True)
    p.add_argument("--pub", type=Path, required=True)
    p.add_argument("--allow-foreign", action="store_true")
    p = sub.add_parser("changelog")
    p.add_argument("--file", type=Path, required=True)
    p.add_argument("--version", required=True)
    p = sub.add_parser("default-min-upgrade-from")
    p.add_argument("--version", required=True)
    p = sub.add_parser("pack")
    p.add_argument("--staging", type=Path, required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--min-upgrade-from", required=True)
    p.add_argument("--key", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "ensure-key":
            ensure_key(args.key, args.pub, allow_foreign=args.allow_foreign)
        elif args.command == "default-min-upgrade-from":
            print(default_min_upgrade_from(args.version))
        elif args.command == "changelog":
            print(changelog_excerpt(args.file.read_text(), args.version))
        else:
            pack(args.staging, args.version, args.min_upgrade_from, args.key, args.out)
    except bundle.BundleError as exc:
        sys.exit(f"error: {exc}")


if __name__ == "__main__":
    main()
