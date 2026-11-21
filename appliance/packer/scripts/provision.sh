#!/bin/sh
# Runs as root inside the build VM (Packer's shell provisioner). Mirrors
# DEPLOY.md's "base OS prep" + "install the native workers" sections,
# minus anything secret -- see files/first-boot.sh for what still has to
# happen on the real, deployed VM's first real boot.
set -eux

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl

# Docker's own official repo, not Debian's docker.io -- Debian's package
# doesn't carry the Compose plugin at all (it lives only in Docker's
# repo), and lags upstream Docker releases independently of that gap.
# Using the official repo gets both the engine and docker-compose-plugin
# as real apt packages that `apt upgrade` keeps current, instead of a
# hand-pinned binary this script would otherwise have to remember to bump.
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update
apt-get install -y --no-install-recommends \
  docker-ce docker-ce-cli containerd.io docker-compose-plugin git openssl \
  qemu-guest-agent
rm -rf /var/lib/apt/lists/*

systemctl enable docker
systemctl start docker

curl -LsSf https://astral.sh/uv/install.sh | sh
install -m 0755 /root/.local/bin/uv /usr/local/bin/uv
install -m 0755 /root/.local/bin/uvx /usr/local/bin/uvx

# Same unprivileged user packaging/systemd/README.md documents.
useradd --system --home /var/lib/resurgam-worker --create-home \
  --shell /usr/sbin/nologin resurgam-worker
usermod -aG kvm resurgam-worker

# Release layout (ADR 0024): every release lives in its own directory and
# /opt/resurgam/current points at the running one, so the host updater
# (resurgam-update) can install the next release beside it and switch -- or
# switch back -- atomically. Release-independent state lives in
# /opt/resurgam/appliance: .env (written by first-boot.sh), the release
# public key, first-boot.sh, and a docker-compose.yml symlink into the
# current release (so `cd /opt/resurgam/appliance && docker compose ...`
# keeps working across updates).
mkdir -p /tmp/resurgam-src
tar -xzf /tmp/resurgam-src.tar.gz -C /tmp/resurgam-src
rm -f /tmp/resurgam-src.tar.gz
version=$(tr -d ' \n' < /tmp/resurgam-src/VERSION)
mkdir -p /opt/resurgam/releases /opt/resurgam/appliance
mv /tmp/resurgam-src "/opt/resurgam/releases/${version}"
ln -sfn "releases/${version}" /opt/resurgam/current
ln -sfn /opt/resurgam/current/appliance/docker-compose.yml /opt/resurgam/appliance/docker-compose.yml
install -m 0644 /opt/resurgam/current/appliance/release/release-signing.pub \
  /opt/resurgam/appliance/release-signing.pub

# The Proxmox file-restore tools the native workers run (proxmox-backup-client for CT/host
# backups; proxmox-backup-file-restore + proxmox-backup-restore-image + pve-qemu-kvm for VM
# backups), all four from ONE Proxmox VE repository release matching Debian 13 (ADR 0011), plus
# the tmpfiles.d entry that lets the unprivileged workers run them. The release's own host-setup
# hook does it -- the same script every update bundle runs (offline, from the bundle's own
# host-packages/ repository), so image and updates can't drift apart. --online: the Proxmox
# keyring is checked against the SHA-256 from Proxmox's docs before apt trusts it.
sh /opt/resurgam/current/appliance/release/host-setup.sh --online

# Native workers' venv -- same locked dependency set the Dockerfile uses for
# the API container, one venv per release behind a stable link (the systemd
# units only ever reference /opt/venvs/resurgam-backend). --no-editable
# makes it self-contained (no references into the release tree), exactly
# like the wheel-based venvs the updater builds for later releases, and
# --python pins Debian's own interpreter, the one those bundles' wheels are
# built for.
cd "/opt/resurgam/releases/${version}/backend"
UV_PROJECT_ENVIRONMENT="/opt/venvs/resurgam-backend-${version}" \
  uv sync --frozen --no-dev --no-editable --python /usr/bin/python3
ln -sfn "resurgam-backend-${version}" /opt/venvs/resurgam-backend

# Pre-build/pre-pull every Compose image now, at build time, not on
# first real boot -- the whole point of shipping an image is that it's
# already ready to run with no network wait. The build override tags the
# images resurgam-backend:<version>/resurgam-web:<version>, which is what
# docker-compose.yml runs (image tags, never builds, on the appliance);
# postgres has only an `image:` key, so it needs its own explicit `pull`
# or first boot would silently download it instead of starting
# immediately. Both subcommands parse the whole file, which evaluates the
# ${VAR:?...} required-secret guards even though nothing at build/pull
# time actually consumes them (no build.args reference them, only the
# backend service's runtime `environment:` block does) -- throwaway values
# satisfy the parser without ever reaching an image layer.
cd /opt/resurgam/current/appliance
RESURGAM_VERSION="${version}" POSTGRES_PASSWORD=build-only SESSION_SECRET=build-only \
  SECRETS_LOCAL_ENCRYPTION_KEY=build-only BREAKGLASS_PASSWORD_HASH=build-only \
  sh -c 'docker compose -f docker-compose.yml -f docker-compose.build.yml build && docker compose pull postgres'

cp /opt/resurgam/current/backend/packaging/systemd/*.service /etc/systemd/system/
install -m 0644 /tmp/resurgam-first-boot.service /etc/systemd/system/resurgam-first-boot.service
install -m 0755 /tmp/first-boot.sh /opt/resurgam/appliance/first-boot.sh
rm -f /tmp/resurgam-first-boot.service /tmp/first-boot.sh

# Host updater (ADR 0024): the CLI, the path unit watching for the web UI's
# requests, and the directories it shares with the API container (bind
# mounts in docker-compose.yml). update-work/ is root-only scratch space for
# extracted bundles and deliberately NOT shared.
install -m 0755 /opt/resurgam/current/appliance/release/resurgam-update.sh /usr/local/sbin/resurgam-update
install -m 0644 /opt/resurgam/current/appliance/release/systemd/resurgam-updater.path \
  /opt/resurgam/current/appliance/release/systemd/resurgam-updater.service /etc/systemd/system/
install -d -m 0755 /var/lib/resurgam /var/lib/resurgam/updates /var/lib/resurgam/updates/incoming \
  /var/lib/resurgam/updates/requests /var/lib/resurgam/updates/status
install -d -m 0700 /var/lib/resurgam/backups /var/lib/resurgam/update-work

systemctl daemon-reload
# Deliberately NOT enabling the four worker units here -- they need the
# per-worker /etc/default files first-boot.sh writes once real secrets
# exist. resurgam-first-boot.service IS enabled: it's the thing that
# creates those files and then enables/starts the workers itself.
systemctl enable resurgam-first-boot.service
# Needs no secrets, so unlike the four workers it's enabled right away:
# it must have written its /dev/kvm report before the API container
# first starts (see the unit's own comments).
systemctl enable resurgam-host-probe.service
systemctl enable resurgam-updater.path

# Debian's genericcloud base ships no network config of its own --
# cloud-init's own netplan render (/etc/netplan/50-cloud-init.yaml) is
# the ONLY thing configuring the NIC, and it matches by the exact MAC
# address cloud-init saw THIS build's NIC using (QEMU's own generic
# default, 52:54:00:12:34:56, since the Packer template doesn't pin
# one). A real deployment's NIC has a different MAC and, depending on
# the hypervisor's PCI topology, a different predictable name too
# (confirmed live: ens3 here vs ens18 on a real PVE q35/OVMF VM) -- so
# that MAC-pinned file leaves every real clone with no network at all.
# Replace it with a plain "DHCP on whatever wired NIC exists" fallback
# that doesn't care about MAC or exact name. cloud-init itself stays able
# to manage networking: Packer's shutdown step runs `cloud-init clean`, so
# a clone never re-applies this build's state, and cloud-init only runs at
# all when an operator attaches a PVE cloud-init drive -- whose network
# config (correct MAC, maybe a static IP) should then win. Hence 90-: networkd
# applies the first matching .network file by name across /etc and /run,
# and cloud-init's netplan render lands as /run/systemd/network/10-netplan-*.
#
# The Name= glob is load-bearing, not cosmetic: Docker's veth pairs are
# Type=ether too, so a Type-only match makes networkd adopt every
# container's veth, pulling it off its bridge -- confirmed live, every
# container lost its network ("Host is unreachable" between containers,
# backend crash-looping on its database connection). Predictable NIC names
# start with en (ens18, ens3, enp*), the kernel fallback is eth*; veth*,
# docker0 and br-* never match.
rm -f /etc/netplan/50-cloud-init.yaml
cat > /etc/systemd/network/90-dhcp-ethernet.network <<'EOF'
[Match]
Type=ether
Name=en* eth*

[Network]
DHCP=yes
EOF

# Packer's shutdown step deletes the SSH host keys so clones don't share
# them. cloud-init regenerates them when an operator attaches a cloud-init
# drive (the supported way to get a shell, see packer/README.md); this
# covers every other boot, since Debian's ssh.service refuses to start
# without keys rather than generating them.
cat > /etc/systemd/system/resurgam-ssh-host-keys.service <<'EOF'
[Unit]
Description=Generate SSH host keys missing on a fresh appliance clone
Before=ssh.service ssh.socket
ConditionPathExistsGlob=!/etc/ssh/ssh_host_*_key

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -A

[Install]
WantedBy=multi-user.target
EOF
systemctl enable resurgam-ssh-host-keys.service

apt-get clean
