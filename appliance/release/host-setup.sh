#!/bin/sh
# RESURGAM host-setup hook (docs/adr/0024, "Host packages and the host-setup hook").
#
# Brings the appliance host (the VM itself, not a container) into the state this release needs.
# Shipped in every release at /opt/resurgam/releases/<version>/appliance/release/host-setup.sh and
# run, as root:
#   - by the host updater (resurgam-update) during an apply, step "host_setup": the NEW release's
#     copy, after its files and venv are installed and before anything is switched over, with
#       --repo <extracted bundle>/host-packages --packages <extracted bundle>/host-packages.list
#     (a non-zero exit rolls the update back);
#   - by the image build (packer/scripts/provision.sh) with --online;
#   - by an operator to repair a host:  sudo sh /opt/resurgam/current/appliance/release/host-setup.sh --online
#
# Usage: host-setup.sh [--repo DIR | --online] [--packages FILE]
#   --repo DIR       install the listed packages offline from DIR, a flat apt repository (Packages
#                    index + .debs) whose files the updater already verified against the bundle's
#                    signed manifest -- so it is used as [trusted=yes] and nothing is downloaded.
#   --online         install them from the Proxmox VE repository (download.proxmox.com).
#   --packages FILE  package list (default: host-packages.list next to this script).
#   With neither --repo nor --online only the host configuration below is (re)applied.
#
# Contract: idempotent (safe to run any number of times, a second run changes nothing), exit 0 on
# success, non-zero with a message on stderr otherwise. Environment from the updater (informative):
# RESURGAM_FROM_VERSION, RESURGAM_TO_VERSION, RESURGAM_RELEASE_DIR. Installed packages are never
# removed or downgraded by this script.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
packages_file="$here/host-packages.list"
repo=""
online=0

die() { echo "host-setup: error: $*" >&2; exit 1; }
log() { echo "host-setup: $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) [ $# -ge 2 ] || die "--repo needs a directory"; repo=$2; shift 2 ;;
    --online) online=1; shift ;;
    --packages) [ $# -ge 2 ] || die "--packages needs a file"; packages_file=$2; shift 2 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) die "unknown argument '$1' (see --help)" ;;
  esac
done
[ -z "$repo" ] || [ "$online" = 0 ] || die "use either --repo or --online, not both"
[ "$(id -u)" = 0 ] || die "must run as root (sudo sh $0 ...)"
. /etc/os-release
[ "${VERSION_CODENAME:-}" = trixie ] || die "expects Debian 13 (trixie), found '${PRETTY_NAME:-unknown}' -- the Proxmox packages must match the Debian release (ADR 0011)"
id resurgam-worker >/dev/null 2>&1 || die "user resurgam-worker does not exist -- this is not a RESURGAM appliance"

# The Proxmox release keyring for Debian 13, as published in Proxmox's own docs (pve.proxmox.com
# wiki "Package Repositories" and pbs.proxmox.com docs "Debian Package Repositories", checked
# 2026-10-07): URL and SHA-256. It holds the "Proxmox Trixie Release Key"
# 24B3 0F06 ECC1 836A 4E5E FECB A7BC D142 0BFE 778E that signs the trixie InRelease files.
KEYRING=/usr/share/keyrings/proxmox-archive-keyring.gpg
KEYRING_URL=https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg
KEYRING_SHA256=136673be77aba35dcce385b28737689ad64fd785a797e57897589aed08db6e45
SOURCES=/etc/apt/sources.list.d/proxmox.sources

write_sources() {
  # deb822, exactly the "No-Subscription" stanza from the PVE docs. Written whenever the keyring
  # exists, so later online `apt upgrade`s keep all four tools on one repository release.
  cat > "$SOURCES.tmp" <<'EOF'
# Managed by RESURGAM (appliance/release/host-setup.sh): the Proxmox VE repository that provides
# proxmox-backup-client, proxmox-backup-file-restore, proxmox-backup-restore-image and pve-qemu-kvm
# as one co-tested release (ADR 0011). Only these packages and their dependencies are installed.
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF
  chmod 0644 "$SOURCES.tmp"
  mv "$SOURCES.tmp" "$SOURCES"
}

pkgs=""
if [ -n "$repo" ] || [ "$online" = 1 ]; then
  [ -f "$packages_file" ] || die "package list $packages_file not found"
  pkgs=$(sed 's/#.*//' "$packages_file" | tr -s ' \t\r\n' ' ' | sed 's/^ *//; s/ *$//')
  [ -n "$pkgs" ] || die "package list $packages_file is empty"
fi

export DEBIAN_FRONTEND=noninteractive
dpkg_opts="-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"

# --- 1. packages ----------------------------------------------------------------------------------
if [ -n "$pkgs" ]; then
  # Finish whatever an interrupted earlier run (or anything else) left half-configured.
  dpkg --configure -a
fi

if [ -n "$repo" ]; then
  [ -f "$repo/Packages" ] || die "$repo is not a host-packages repository (no Packages index)"
  repo=$(cd "$repo" && pwd)
  work=$(mktemp -d /var/tmp/resurgam-host-setup.XXXXXX)
  trap 'rm -rf "$work"' EXIT
  mkdir "$work/lists" "$work/parts"
  echo "deb [trusted=yes] file:$repo ./" > "$work/sources.list"
  # Only the bundle's repository, with its own lists directory: the host's own apt sources and
  # lists are neither used (offline) nor touched. APT::Sandbox::User=root: the repository lives in
  # the updater's root-only work directory, which apt's unprivileged _apt user can't read.
  apt_opts="-o Dir::Etc::SourceList=$work/sources.list -o Dir::Etc::SourceParts=$work/parts \
    -o Dir::State::Lists=$work/lists -o APT::Get::List-Cleanup=false -o APT::Sandbox::User=root"
  log "installing from the bundle's repository: $pkgs"
  # shellcheck disable=SC2086
  apt-get $apt_opts update -q
  # shellcheck disable=SC2086
  apt-get $apt_opts install -y -q --no-install-recommends $dpkg_opts $pkgs
elif [ "$online" = 1 ]; then
  if [ ! -s "$KEYRING" ]; then
    tmpkey=$(mktemp)
    curl -fsSL --retry 3 "$KEYRING_URL" -o "$tmpkey" || die "could not download $KEYRING_URL"
    actual=$(sha256sum "$tmpkey" | cut -d' ' -f1)
    if [ "$actual" != "$KEYRING_SHA256" ]; then
      rm -f "$tmpkey"
      die "$KEYRING_URL has SHA-256 $actual, expected $KEYRING_SHA256 (Proxmox docs) -- refusing to trust it"
    fi
    install -m 0644 "$tmpkey" "$KEYRING"
    rm -f "$tmpkey"
    log "installed the Proxmox release keyring (SHA-256 verified)"
  fi
  write_sources
  log "installing from download.proxmox.com (pve-no-subscription): $pkgs"
  apt-get update -q
  # shellcheck disable=SC2086
  apt-get install -y -q --no-install-recommends $dpkg_opts $pkgs
fi

# The online source is useful even on an offline-updated host (for a later online upgrade), and
# the keyring package installed above provides its key.
if [ -s "$KEYRING" ]; then
  write_sources
fi

# --- 2. host configuration for unprivileged file restore ------------------------------------------
# Live-verified 2026-10-07 (ADR 0011 addendum): proxmox-file-restore works for the workers'
# unprivileged user (group kvm, no extra capabilities) once it can write its per-uid state dir and
# QMP socket under /run/proxmox-backup and its micro-VM log under /var/log/proxmox-backup/
# file-restore -- on a stock install both are root/backup-owned, which was the "EACCES unless
# root" ADR 0011 recorded. /run is a tmpfs, hence tmpfiles.d (re-created on every boot); the
# sticky bit keeps users from removing each other's sockets.
cat > /etc/tmpfiles.d/resurgam-file-restore.conf.tmp <<'EOF'
# Managed by RESURGAM (appliance/release/host-setup.sh): lets the file-restore/file-index workers
# (user resurgam-worker) run proxmox-file-restore without root.
d /run/proxmox-backup 1775 backup resurgam-worker -
d /var/log/proxmox-backup 0755 backup backup -
d /var/log/proxmox-backup/file-restore 0750 resurgam-worker resurgam-worker -
EOF
mv /etc/tmpfiles.d/resurgam-file-restore.conf.tmp /etc/tmpfiles.d/resurgam-file-restore.conf
systemd-tmpfiles --create /etc/tmpfiles.d/resurgam-file-restore.conf

# File-restore download content (single files, folder/multi-selection ZIPs): the file-restore
# worker (resurgam-worker) writes it, the API container reads it through a read-only bind mount
# (docker-compose.yml, FILE_RESTORE_OUTPUT_DIR). Not shared with anyone else, hence 0750; the API
# container's root reads it regardless of the mode. Created here rather than only at image build so
# appliances updated from a release without it get it too (host setup runs before the switch).
install -d -m 0755 /var/lib/resurgam
install -d -m 0750 -o resurgam-worker -g resurgam-worker /var/lib/resurgam/file-restore

# The host<->micro-VM channel. Usually autoloaded on first open of /dev/vhost-vsock; made explicit.
echo vhost_vsock > /etc/modules-load.d/resurgam-vhost-vsock.conf
modprobe vhost_vsock 2>/dev/null || log "warning: could not load vhost_vsock (no VM-path file restore until it loads)"

# --- 3. verify --------------------------------------------------------------------------------------
if [ -n "$pkgs" ]; then
  for p in $pkgs; do
    dpkg-query -W -f='${db:Status-Abbrev}' "$p" 2>/dev/null | grep -q '^ii' || die "package $p is not installed"
  done
  proxmox-backup-client version >/dev/null || die "proxmox-backup-client does not run"
  proxmox-file-restore help >/dev/null || die "proxmox-file-restore does not run"
  qemu-system-x86_64 --version >/dev/null || die "qemu-system-x86_64 (pve-qemu-kvm) does not run"
  for f in /usr/lib/x86_64-linux-gnu/proxmox-backup/file-restore/bzImage \
           /var/cache/proxmox-backup/file-restore-initramfs.img; do
    [ -s "$f" ] || die "$f is missing -- proxmox-backup-restore-image/proxmox-backup-file-restore are not correctly installed"
  done
  log "installed: $(dpkg-query -W -f='${Package} ${Version}, ' $pkgs | sed 's/, $//')"
fi
for d in /run/proxmox-backup /var/log/proxmox-backup/file-restore /var/lib/resurgam/file-restore; do
  runuser -u resurgam-worker -- test -w "$d" || die "$d is not writable for resurgam-worker"
done

# Refresh the capability report the API shows (resurgam-host-probe.service), if it is running.
systemctl try-restart resurgam-host-probe.service 2>/dev/null || true
log "done"
