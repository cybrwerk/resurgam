#!/bin/sh
# Builds and signs a RESURGAM update bundle (docs/adr/0024) for the version in
# the repo-root VERSION file: dist/resurgam-<version>.bundle. See UPDATE.md.
#
# Requires: docker (with network access -- base images, npm, PyPI), uv.
#
# Environment (all optional):
#   MIN_UPGRADE_FROM      oldest running version this bundle may upgrade. Default: 0.2.0 (the
#                         first release with the updater) for releases up to 0.2.2, and 0.2.2
#                         (the first updater that runs the release's host-setup hook) after it
#   DEBIAN_IMAGE          image the host packages are resolved in; must be the appliance's
#                         Debian release. Default debian:trixie
#   RESURGAM_SIGNING_KEY  private key (default ~/.config/resurgam/release-signing.key,
#                         created with its public half on the first run)
#   ALLOW_FOREIGN_KEY=1   sign with a key that does NOT match release-signing.pub
#                         (only for testing that appliances reject such bundles)
#   OUT_DIR               output directory (default appliance/release/dist, gitignored)
#   PYTHON_IMAGE          image used to collect the workers' wheels; must match the
#                         appliance's Python (Debian 13 = 3.13). Default python:3.13-slim-trixie
set -eu

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
version=$(tr -d ' \n' < "$repo/VERSION")
key=${RESURGAM_SIGNING_KEY:-$HOME/.config/resurgam/release-signing.key}
out_dir=${OUT_DIR:-$here/dist}
python_image=${PYTHON_IMAGE:-python:3.13-slim-trixie}
debian_image=${DEBIAN_IMAGE:-debian:trixie}
out="$out_dir/resurgam-$version.bundle"

helper() {
  uv run --project "$repo/backend" --frozen --quiet python "$here/make_bundle.py" "$@"
}

foreign=""
[ "${ALLOW_FOREIGN_KEY:-}" = "1" ] && foreign="--allow-foreign"
min_upgrade_from=${MIN_UPGRADE_FROM:-$(helper default-min-upgrade-from --version "$version")}
helper ensure-key --key "$key" --pub "$here/release-signing.pub" $foreign
# Fails early if the release notes are missing.
helper changelog --file "$repo/CHANGELOG.md" --version "$version" > /dev/null
# The Recovery Lab .debs built into the backend image (Dockerfile stage
# lab-packages) must carry this release's version: restore-worker/debian/
# changelog's upstream version == VERSION (see restore-worker/README.md).
# The image build enforces it too; checking here fails before any build.
deb_version=$(sed -n '1s/^resurgam-worker (\([^)]*\)).*/\1/p' "$repo/restore-worker/debian/changelog")
if [ "${deb_version%-*}" != "$version" ]; then
  echo "restore-worker/debian/changelog is at $deb_version, VERSION is $version:" >&2
  echo "  add an entry first: cd restore-worker && dch -v $version-1 (or edit debian/changelog by hand)" >&2
  exit 1
fi

staging=$(mktemp -d "${TMPDIR:-/tmp}/resurgam-bundle.XXXXXX")
cleanup() { rm -rf "$staging"; }
trap cleanup EXIT
mkdir -p "$staging/images" "$staging/wheels"

echo "==> Building images resurgam-backend:$version and resurgam-web:$version"
# The same Dockerfiles and contexts appliance/docker-compose.build.yml uses.
docker build --pull -f "$repo/backend/Dockerfile" --build-arg "RESURGAM_VERSION=$version" \
  -t "resurgam-backend:$version" "$repo"
docker build --pull --build-arg "RESURGAM_VERSION=$version" \
  -t "resurgam-web:$version" "$repo/frontend"
docker save -o "$staging/images/backend.tar" "resurgam-backend:$version"
docker save -o "$staging/images/web.tar" "resurgam-web:$version"

echo "==> Packing source"
sh "$here/pack-source.sh" "$repo" "$staging/source.tar.gz"
cp "$repo/CHANGELOG.md" "$staging/CHANGELOG.md"

echo "==> Collecting the native workers' wheels ($python_image)"
# The exact locked dependency set (uv.lock, no dev group), minus the two
# local projects, which are built as wheels themselves below -- the appliance
# installs everything with --no-index, so nothing may need a build backend or
# the network there. Built inside a Debian 13 / Python 3.13 container so the
# binary wheels match the appliance's interpreter and glibc.
(cd "$repo/backend" && uv export --frozen --no-dev --no-hashes --no-header \
  --no-emit-project --no-emit-package resurgam-worker -o "$staging/locked-requirements.txt")
(cd "$repo" && tar --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache \
  --exclude=.ruff_cache --exclude=.env --exclude=tests -cf "$staging/projects.tar" backend restore-worker)
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e PIP_DISABLE_PIP_VERSION_CHECK=1 \
  -v "$staging:/work" "$python_image" sh -euc '
    mkdir /tmp/src && tar -xf /work/projects.tar -C /tmp/src
    pip wheel --quiet --no-deps --wheel-dir /work/wheels -r /work/locked-requirements.txt
    pip wheel --quiet --no-deps --wheel-dir /work/wheels /tmp/src/restore-worker /tmp/src/backend
  '
rm -f "$staging/locked-requirements.txt" "$staging/projects.tar"

echo "==> Collecting host packages ($debian_image + Proxmox VE pve-no-subscription)"
# host-packages.list and the flat apt repository host-setup.sh installs them from on the appliance,
# offline (ADR 0024). Resolved in a container of the appliance's Debian release against an EMPTY
# dpkg status, so the repository holds the complete dependency closure and installs on any
# appliance regardless of what it already has (apt only installs what is actually missing or
# older). File names are sanitised to the bundle's path rules (no '%3a' epochs, no '~'); the
# Packages index points at the renamed files. The Proxmox keyring is verified against the SHA-256
# from Proxmox's docs, the very constants host-setup.sh uses.
mkdir -p "$staging/host-packages"
cp "$here/host-packages.list" "$staging/host-packages.list"
keyring_url=$(sed -n 's/^KEYRING_URL=//p' "$here/host-setup.sh")
keyring_sha256=$(sed -n 's/^KEYRING_SHA256=//p' "$here/host-setup.sh")
docker run --rm -e DEBIAN_FRONTEND=noninteractive -e KEYRING_URL="$keyring_url" \
  -e KEYRING_SHA256="$keyring_sha256" -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
  -v "$staging:/work" "$debian_image" sh -euc '
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends ca-certificates curl apt-utils > /dev/null
    curl -fsSL --retry 3 "$KEYRING_URL" -o /usr/share/keyrings/proxmox-archive-keyring.gpg
    echo "$KEYRING_SHA256  /usr/share/keyrings/proxmox-archive-keyring.gpg" | sha256sum -c -
    printf "Types: deb\nURIs: http://download.proxmox.com/debian/pve\nSuites: trixie\nComponents: pve-no-subscription\nSigned-By: /usr/share/keyrings/proxmox-archive-keyring.gpg\n" \
      > /etc/apt/sources.list.d/proxmox.sources
    apt-get update -qq
    pkgs=$(sed "s/#.*//" /work/host-packages.list | tr -s " \t\r\n" " ")
    mkdir -p /tmp/debs/partial
    : > /tmp/empty-status
    apt-get -qq -o Dir::State::status=/tmp/empty-status -o Dir::Cache::archives=/tmp/debs \
      install --download-only -y --no-install-recommends $pkgs > /dev/null
    for f in /tmp/debs/*.deb; do
      name=$(basename "$f" | sed "s/%3a/_/g; s/[^A-Za-z0-9._+-]/_/g")
      mv "$f" "/work/host-packages/$name"
    done
    cd /work/host-packages && apt-ftparchive packages . > Packages 2> /dev/null
    chown -R "$HOST_UID:$HOST_GID" /work/host-packages
    echo "$(ls *.deb | wc -l) packages, $(du -sh . | cut -f1)"
  '

echo "==> Signing and packing"
helper pack --staging "$staging" --version "$version" --min-upgrade-from "$min_upgrade_from" \
  --key "$key" --out "$out"
