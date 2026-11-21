"""resurgam_api.updates.bundle -- the signed release-bundle format (ADR 0024)."""

import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

from resurgam_api.updates import bundle
from tests.bundle_factory import default_members, host_package_members, make_bundle, make_key_pair, write_tar

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def keys(tmp_path):
    return make_key_pair(tmp_path)


def _pub(path: Path):
    return bundle.load_public_key_file(path)


# --- versions -----------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1.2", "1.2.3.4", "01.2.3", "1.2.3-rc1", "v1.2.3", "", "a.b.c"])
def test_parse_version_rejects_non_release_versions(value):
    with pytest.raises(bundle.BundleError, match="MAJOR.MINOR.PATCH"):
        bundle.parse_version(value)


def test_compare_versions_is_numeric_per_component():
    assert bundle.compare_versions("0.10.0", "0.9.9") == 1
    assert bundle.compare_versions("0.2.0", "0.2.0") == 0
    assert bundle.compare_versions("0.2.0", "1.0.0") == -1


def _manifest(version="0.2.1", min_upgrade_from="0.2.0") -> bundle.Manifest:
    return bundle.Manifest(
        format=1, version=version, created_at="2026-10-07T00:00:00+00:00", min_upgrade_from=min_upgrade_from,
        changelog="", components={},
    )  # fmt: skip


def test_upgrade_allowed_when_newer_and_at_least_min():
    bundle.check_upgrade_allowed(_manifest("0.3.0", "0.2.0"), "0.2.5")


@pytest.mark.parametrize("current", ["0.2.1", "0.3.0"])
def test_upgrade_rejected_when_not_newer(current):
    with pytest.raises(bundle.BundleError, match="not newer"):
        bundle.check_upgrade_allowed(_manifest("0.2.1"), current)


def test_upgrade_rejected_below_min_upgrade_from():
    with pytest.raises(bundle.BundleError, match="requires at least version 0.3.0"):
        bundle.check_upgrade_allowed(_manifest("0.4.0", "0.3.0"), "0.2.9")


def test_upgrade_rejected_from_a_dev_checkout():
    with pytest.raises(bundle.BundleError, match="not a release version"):
        bundle.check_upgrade_allowed(_manifest(), "0.0.0-dev")


# --- signatures and the full bundle ---------------------------------------------------------------


def test_valid_bundle_verifies_and_extracts_exact_bytes(tmp_path, keys):
    key, pub = keys
    path = make_bundle(tmp_path / "ok.bundle", key)
    manifest = bundle.verify_bundle(path, _pub(pub), "0.2.0")
    assert manifest.version == "0.2.1"
    assert manifest.summary() == {"version": "0.2.1", "changelog": "- things", "min_upgrade_from": "0.2.0"}
    out = tmp_path / "out"
    bundle.extract_bundle(path, out, _pub(pub))
    for name, data in default_members("0.2.1").items():
        assert (out / name).read_bytes() == data
    assert json.loads((out / "manifest.json").read_text())["version"] == "0.2.1"


def test_bundle_signed_by_a_foreign_key_is_rejected(tmp_path, keys):
    _, pub = keys
    foreign_key, _ = make_key_pair(tmp_path, "foreign")
    path = make_bundle(tmp_path / "foreign.bundle", foreign_key)
    with pytest.raises(bundle.BundleError, match="signature does not match"):
        bundle.verify_bundle(path, _pub(pub))
    with pytest.raises(bundle.BundleError, match="signature does not match"):
        bundle.extract_bundle(path, tmp_path / "out", _pub(pub))


def test_tampered_component_is_rejected(tmp_path, keys):
    key, pub = keys
    path = make_bundle(tmp_path / "t.bundle", key, tamper={"images/web.tar": b"web image 0.2.X"})
    # same size, different content -- only the hash catches it
    with pytest.raises(bundle.BundleError, match="images/web.tar does not match the signed manifest"):
        bundle.verify_bundle(path, _pub(pub))
    with pytest.raises(bundle.BundleError, match="images/web.tar does not match"):
        bundle.extract_bundle(path, tmp_path / "out", _pub(pub))
    # the signature-only pre-check cannot see it -- the updater's full check must
    assert bundle.verify_bundle(path, _pub(pub), check_hashes=False).version == "0.2.1"


def test_tampered_component_size_is_rejected(tmp_path, keys):
    key, pub = keys
    path = make_bundle(tmp_path / "t.bundle", key, tamper={"source.tar.gz": b"short"})
    with pytest.raises(bundle.BundleError, match="source.tar.gz does not match"):
        bundle.verify_bundle(path, _pub(pub))


def test_tampered_manifest_breaks_the_signature(tmp_path, keys):
    key, pub = keys
    good = make_bundle(tmp_path / "g.bundle", key)
    with tarfile.open(good) as tf:
        members = [(m.name, tf.extractfile(m).read()) for m in tf.getmembers()]
    members = [
        (n, d.replace(b'"version": "0.2.1"', b'"version": "9.9.9"') if n == "manifest.json" else d) for n, d in members
    ]
    path = write_tar(tmp_path / "m.bundle", members)
    with pytest.raises(bundle.BundleError, match="signature does not match"):
        bundle.verify_bundle(path, _pub(pub))


def test_unlisted_member_is_rejected(tmp_path, keys):
    key, pub = keys
    path = make_bundle(tmp_path / "x.bundle", key, extra={"evil.sh": b"rm -rf /"})
    with pytest.raises(bundle.BundleError, match="does not list: evil.sh"):
        bundle.verify_bundle(path, _pub(pub))


def test_missing_member_is_rejected(tmp_path, keys):
    key, pub = keys
    good = make_bundle(tmp_path / "g.bundle", key)
    with tarfile.open(good) as tf:
        members = [(m.name, tf.extractfile(m).read()) for m in tf.getmembers() if m.name != "images/web.tar"]
    path = write_tar(tmp_path / "m.bundle", members)
    with pytest.raises(bundle.BundleError, match="missing files its manifest lists: images/web.tar"):
        bundle.verify_bundle(path, _pub(pub))


def test_missing_signature_and_non_tar_are_rejected(tmp_path, keys):
    _, pub = keys
    path = write_tar(tmp_path / "nosig.bundle", [("manifest.json", b"{}")])
    with pytest.raises(bundle.BundleError, match="no manifest.json.sig"):
        bundle.verify_bundle(path, _pub(pub))
    junk = tmp_path / "junk.bundle"
    junk.write_bytes(b"\x00not a tar" * 100)
    with pytest.raises(bundle.BundleError, match="not a RESURGAM bundle|corrupt|no manifest"):
        bundle.verify_bundle(junk, _pub(pub))
    with pytest.raises(bundle.BundleError, match="does not exist"):
        bundle.verify_bundle(tmp_path / "absent.bundle", _pub(pub))


def test_symlink_and_duplicate_members_are_rejected(tmp_path, keys):
    key, pub = keys
    good = make_bundle(tmp_path / "g.bundle", key)
    link = tmp_path / "link.bundle"
    with tarfile.open(good) as src, tarfile.open(link, "w") as dst:
        for m in src.getmembers():
            dst.addfile(m, src.extractfile(m))
        info = tarfile.TarInfo("images/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/shadow"
        dst.addfile(info)
    with pytest.raises(bundle.BundleError, match="not a regular file"):
        bundle.verify_bundle(link, _pub(pub))
    dup = tmp_path / "dup.bundle"
    with tarfile.open(good) as src, tarfile.open(dup, "w") as dst:
        for m in src.getmembers():
            dst.addfile(m, src.extractfile(m))
        info = tarfile.TarInfo("CHANGELOG.md")
        info.size = 3
        dst.addfile(info, io.BytesIO(b"bad"))
    with pytest.raises(bundle.BundleError, match="CHANGELOG.md twice"):
        bundle.verify_bundle(dup, _pub(pub))


def test_extract_refuses_a_non_empty_directory(tmp_path, keys):
    key, pub = keys
    path = make_bundle(tmp_path / "ok.bundle", key)
    out = tmp_path / "out"
    out.mkdir()
    (out / "leftover").write_text("x")
    with pytest.raises(bundle.BundleError, match="not empty"):
        bundle.extract_bundle(path, out, _pub(pub))


# --- manifest structure ---------------------------------------------------------------------------


def _doc(**overrides):
    comp = {"sha256": "0" * 64, "size": 1}
    doc = {
        "format": 1, "version": "0.2.1", "created_at": "2026-10-07T00:00:00+00:00", "min_upgrade_from": "0.2.0",
        "changelog": "", "components": {
            "images/backend.tar": comp, "images/web.tar": comp, "source.tar.gz": comp, "CHANGELOG.md": comp,
            "wheels/requirements.txt": comp, "wheels/a-1-py3-none-any.whl": comp,
        },
    }  # fmt: skip
    doc.update(overrides)
    return json.dumps(doc).encode()


def test_parse_manifest_accepts_a_complete_manifest():
    assert bundle.parse_manifest(_doc()).version == "0.2.1"


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"format": 2}, "Unsupported bundle format"),
        ({"version": "latest"}, "MAJOR.MINOR.PATCH"),
        ({"min_upgrade_from": "0.3.0"}, "newer than the bundle"),
        ({"created_at": "yesterday"}, "ISO 8601"),
        ({"changelog": None}, "'changelog' is missing"),
        ({"components": {}}, "no components"),
        ({"components": {"../../etc/passwd": {"sha256": "0" * 64, "size": 1}}}, "Invalid component path"),
        ({"components": {"/abs": {"sha256": "0" * 64, "size": 1}}}, "Invalid component path"),
        ({"components": {"images/backend.tar": {"sha256": "XYZ", "size": 1}}}, "lowercase hex sha256"),
        ({"components": {"images/backend.tar": {"sha256": "0" * 64, "size": 1}}}, "incomplete: missing"),
    ],
)
def test_parse_manifest_rejects_bad_manifests(overrides, match):
    with pytest.raises(bundle.BundleError, match=match):
        bundle.parse_manifest(_doc(**overrides))


def test_parse_manifest_requires_wheels():
    doc = json.loads(_doc())
    del doc["components"]["wheels/a-1-py3-none-any.whl"]
    with pytest.raises(bundle.BundleError, match="no Python wheels"):
        bundle.parse_manifest(json.dumps(doc).encode())


def test_keys_must_be_ed25519_pem(tmp_path):
    with pytest.raises(bundle.BundleError, match="not a PEM public key"):
        bundle.load_public_key(b"garbage")
    with pytest.raises(bundle.BundleError, match="not an unencrypted PEM private key"):
        bundle.load_private_key(b"garbage")
    with pytest.raises(bundle.BundleError, match="Cannot read the release public key"):
        bundle.load_public_key_file(tmp_path / "absent.pub")


def test_committed_release_public_key_is_a_valid_ed25519_key():
    bundle.load_public_key_file(REPO_ROOT / "appliance" / "release" / "release-signing.pub")


# --- release-builder helper -----------------------------------------------------------------------


def _make_bundle_module():
    spec = importlib.util.spec_from_file_location("make_bundle", REPO_ROOT / "appliance" / "release" / "make_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_builder_changelog_excerpt_and_pack_roundtrip(tmp_path, keys):
    mb = _make_bundle_module()
    text = "# Changelog\n\n## 0.2.1\n\n- fixed a thing\n\n## 0.2.0\n\n- first\n"
    assert mb.changelog_excerpt(text, "0.2.1") == "- fixed a thing"
    assert mb.changelog_excerpt(text, "0.2.0") == "- first"
    with pytest.raises(SystemExit):
        mb.changelog_excerpt(text, "0.3.0")

    key, pub = keys
    staging = tmp_path / "staging"
    for name, data in default_members("0.2.1").items():
        if name == "wheels/requirements.txt":
            continue  # pack() generates it from the wheel names
        (staging / name).parent.mkdir(parents=True, exist_ok=True)
        (staging / name).write_bytes(data if name != "CHANGELOG.md" else text.encode())
    out = tmp_path / "dist" / "resurgam-0.2.1.bundle"
    mb.pack(staging, "0.2.1", "0.2.0", key, out)
    manifest = bundle.verify_bundle(out, _pub(pub), "0.2.0")
    assert manifest.changelog == "- fixed a thing"
    assert (staging / "wheels" / "requirements.txt").read_text() == "resurgam-api==0.1.0\n"
    with tarfile.open(out) as tf:
        assert tf.getnames()[:2] == ["manifest.json", "manifest.json.sig"]


def test_builder_refuses_a_key_that_does_not_match_the_committed_public_key(tmp_path, keys):
    mb = _make_bundle_module()
    key, _ = keys
    _, other_pub = make_key_pair(tmp_path, "other")
    key.chmod(0o600)
    with pytest.raises(SystemExit, match="does not match the committed public key"):
        mb.ensure_key(key, other_pub, allow_foreign=False)
    mb.ensure_key(key, other_pub, allow_foreign=True)  # explicit test override


def test_builder_creates_a_mode_600_key_and_never_replaces_an_existing_public_key(tmp_path):
    mb = _make_bundle_module()
    key, pub = tmp_path / "cfg" / "release.key", tmp_path / "release.pub"
    mb.ensure_key(key, pub, allow_foreign=False)
    assert key.stat().st_mode & 0o777 == 0o600
    bundle.load_public_key_file(pub)
    key.unlink()
    with pytest.raises(SystemExit, match="Restore the private key"):
        mb.ensure_key(key, pub, allow_foreign=False)


# --- host packages (ADR 0024, "Host packages and the host-setup hook") --------------------------


def _with_host_packages(version="0.2.3", **kwargs):
    return {**default_members(version), **host_package_members(**kwargs)}


def test_host_packages_are_hashed_like_every_other_component(tmp_path, keys):
    key, pub = keys
    path = make_bundle(tmp_path / "h.bundle", key, version="0.2.3", members=_with_host_packages())
    manifest = bundle.verify_bundle(path, _pub(pub), "0.2.2")
    assert manifest.has_host_packages
    assert "host-packages/Packages" in manifest.components
    assert "host-packages/pve-qemu-kvm_1.0-1_amd64.deb" in manifest.components
    out = tmp_path / "x"
    bundle.extract_bundle(path, out, _pub(pub))
    names = bundle.parse_package_list((out / "host-packages.list").read_text())
    assert [p.package for p in bundle.check_host_repo(out / "host-packages", names)] == names


def test_a_tampered_host_package_fails_verification(tmp_path, keys):
    key, pub = keys
    members = _with_host_packages()
    path = make_bundle(
        tmp_path / "h.bundle",
        key,
        version="0.2.3",
        members=members,
        tamper={"host-packages/pve-qemu-kvm_1.0-1_amd64.deb": b"fake deb pve-qemu-kvX"},
    )
    with pytest.raises(bundle.BundleError, match="pve-qemu-kvm_1.0-1_amd64.deb does not match"):
        bundle.verify_bundle(path, _pub(pub))
    with pytest.raises(bundle.BundleError, match="does not match"):
        bundle.extract_bundle(path, tmp_path / "x", _pub(pub))


def test_bundles_without_host_packages_stay_valid():
    manifest = bundle.parse_manifest(
        bundle.build_manifest_bytes(
            version="0.2.2", min_upgrade_from="0.2.0", changelog="", components=_components(default_members("0.2.2"))
        )
    )
    assert not manifest.has_host_packages


@pytest.mark.parametrize(
    "drop", ["host-packages.list", "host-packages/Packages", "host-packages/pve-qemu-kvm_1.0-1_amd64.deb"]
)
def test_incomplete_host_packages_are_rejected(drop):
    members = _with_host_packages(names=("pve-qemu-kvm",))
    del members[drop]
    with pytest.raises(bundle.BundleError, match="host packages are incomplete"):
        bundle.build_manifest_bytes(
            version="0.2.3", min_upgrade_from="0.2.2", changelog="", components=_components(members)
        )


def test_host_packages_must_be_a_flat_repository():
    members = {**_with_host_packages(), "host-packages/pool/x.deb": b"x"}
    with pytest.raises(bundle.BundleError, match="flat repository"):
        bundle.build_manifest_bytes(
            version="0.2.3", min_upgrade_from="0.2.2", changelog="", components=_components(members)
        )


def _components(members):
    return {name: bundle.Component(sha256=hashlib.sha256(d).hexdigest(), size=len(d)) for name, d in members.items()}


def test_package_list_parsing():
    assert bundle.parse_package_list("# c\npve-qemu-kvm\n\n  cpio  # why\npve-qemu-kvm\n") == ["pve-qemu-kvm", "cpio"]
    with pytest.raises(bundle.BundleError, match="not a Debian package name"):
        bundle.parse_package_list("pve-qemu-kvm; rm -rf /\n")
    with pytest.raises(bundle.BundleError, match="lists no packages"):
        bundle.parse_package_list("# nothing\n")


def _repo(tmp_path, members):
    repo = tmp_path / "host-packages"
    for name, data in members.items():
        if name.startswith("host-packages/"):
            (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / name).write_bytes(data)
    return repo


def test_check_host_repo_catches_inconsistent_repositories(tmp_path):
    members = host_package_members(names=("pve-qemu-kvm",))
    repo = _repo(tmp_path, members)
    assert [p.version for p in bundle.check_host_repo(repo, ["pve-qemu-kvm"])] == ["1.0-1"]
    with pytest.raises(bundle.BundleError, match="does not provide cpio"):
        bundle.check_host_repo(repo, ["pve-qemu-kvm", "cpio"])
    (repo / "stray_1_all.deb").write_bytes(b"x")
    with pytest.raises(bundle.BundleError, match="does not list stray_1_all.deb"):
        bundle.check_host_repo(repo)
    (repo / "stray_1_all.deb").unlink()
    (repo / "pve-qemu-kvm_1.0-1_amd64.deb").write_bytes(b"changed")
    with pytest.raises(bundle.BundleError, match="does not match its Packages entry"):
        bundle.check_host_repo(repo)
    (repo / "Packages").write_text("Package: x\nVersion: 1\nFilename: ../../etc/passwd\nSize: 1\nSHA256: ab\n")
    with pytest.raises(bundle.BundleError, match="plain file"):
        bundle.check_host_repo(repo)


def test_real_deb_file_names_pass_the_0_2_1_path_rules():
    """The builder sanitises apt's file names ('%3a' epochs, '~') -- what remains must be accepted
    by the component-path regex, which is unchanged since 0.2.1 (the updater on the test appliance)."""
    for name in ("dmsetup_2_1.02.205-2+pmx1_amd64.deb", "libllvm19_1_19.1.7-3+b1_amd64.deb", "Packages"):
        bundle.validate_component_path(f"host-packages/{name}")
    with pytest.raises(bundle.BundleError):
        bundle.validate_component_path("host-packages/libllvm19_1%3a19.1.7-3+b1_amd64.deb")


def test_builder_checks_host_packages_and_defaults_min_upgrade_from(tmp_path, keys):
    mb = _make_bundle_module()
    assert mb.default_min_upgrade_from("0.2.2") == "0.2.0"
    assert mb.default_min_upgrade_from("0.2.3") == "0.2.2"
    assert mb.default_min_upgrade_from("1.0.0") == "0.2.2"
    key, pub = keys
    staging = tmp_path / "staging"
    for name, data in _with_host_packages().items():
        if name == "wheels/requirements.txt":
            continue
        (staging / name).parent.mkdir(parents=True, exist_ok=True)
        (staging / name).write_bytes(data if name != "CHANGELOG.md" else b"## 0.2.3\n- host packages\n")
    out = tmp_path / "resurgam-0.2.3.bundle"
    mb.pack(staging, "0.2.3", "0.2.2", key, out)
    assert bundle.verify_bundle(out, _pub(pub), "0.2.2").has_host_packages
    (staging / "host-packages" / "pve-qemu-kvm_1.0-1_amd64.deb").write_bytes(b"corrupted download")
    with pytest.raises(bundle.BundleError, match="does not match its Packages entry"):
        mb.pack(staging, "0.2.3", "0.2.2", key, tmp_path / "again.bundle")
