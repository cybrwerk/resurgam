# Deploying RESURGAM to a real appliance VM

This is the end-to-end walkthrough: a real Debian 13 VM, both halves of manifest §5.3's split
installed and talking to each other, finishing with the first-boot setup wizard. `README.md` in
this directory documents what each piece *is*; this document is how you actually put them on one
box by hand.

**Prefer not to do this by hand?** `packer/` builds a qcow2 that automates §1-§5 below entirely
(Docker, the native workers' venv, all systemd units, pre-built images) -- import that into PVE
and you land directly at §6. See [`packer/README.md`](packer/README.md).

## What you end up with

One VM, two halves, per the user's own end-state decision (not a container-only or bare-metal-only
design):

- **Docker Compose** (`docker-compose.yml`, this directory) -- API, UI/reverse-proxy, PostgreSQL.
- **Native systemd services** (`../backend/packaging/systemd/`) -- the four workers (file-restore,
  file-index, collector-scheduler, recovery-lab-scheduler). The file-restore/file-index workers run
  every PBS CLI call (`proxmox-backup-client` for CT/host backups, `proxmox-file-restore` and its
  QEMU micro-VM for VM backups) and need `/dev/kvm`, which a container can't cleanly grant (ADR
  0011) -- so they run directly on the VM, not in Docker, as the unprivileged `resurgam-worker`
  user (no capabilities; see §5). The API container needs none of the Proxmox tools.

Both halves share one PostgreSQL database and must agree on `SECRETS_LOCAL_ENCRYPTION_KEY` (the
workers decrypt the same stored PBS/PVE credentials the API does).

## 1. Provision the VM (on the PVE host)

Manifest §5.2 requires nested virtualization so `proxmox-file-restore`'s VM-path restore can boot
its own internal QEMU micro-VM:

1. On the **PVE host** (not the guest), enable nested virtualization for your CPU vendor and
   persist it:
   ```sh
   # Intel
   echo "options kvm_intel nested=1" > /etc/modprobe.d/kvm-intel.conf
   # AMD
   echo "options kvm_amd nested=1" > /etc/modprobe.d/kvm-amd.conf
   ```
   Reload the module (requires no VMs currently using it, or a host reboot) and confirm:
   ```sh
   modprobe -r kvm_intel && modprobe kvm_intel   # or kvm_amd
   cat /sys/module/kvm_intel/parameters/nested    # expect Y
   ```
2. Create the appliance VM with **CPU type `host`** (`qm set <vmid> --cpu host`, or the
   equivalent field in the PVE GUI) -- a generic CPU type (e.g. `kvm64`) does not expose VMX/SVM
   to the guest even with nesting on at the host.
3. Install Debian 13 (trixie) as the guest OS.
4. Boot it, and confirm `/dev/kvm` exists and is accessible:
   ```sh
   ls -l /dev/kvm
   ```
   If this is missing, the setup wizard's welcome step will also tell you (it runs this same
   check) -- RESURGAM still works without it, just with a clear warning that VM-path file
   restores will be slow or unavailable (manifest §5.2's required UI warning).

## 2. Base OS prep (inside the VM)

```sh
apt update && apt install -y docker.io docker-compose-plugin git

# uv -- used to build both the API container and the native workers' venv
curl -LsSf https://astral.sh/uv/install.sh | sh

# The unprivileged user the native workers run as (packaging/systemd/README.md)
useradd --system --home /var/lib/resurgam-worker --shell /usr/sbin/nologin resurgam-worker
usermod -aG kvm resurgam-worker
```

## 3. Get the code onto the VM

Any method that preserves the monorepo layout works (`git clone`, `rsync`, a release tarball) --
the important part is that `backend/`, `restore-worker/`, `frontend/`, and `appliance/` land as
siblings, since `backend/pyproject.toml` depends on `../restore-worker` by relative path:

```sh
git clone <this-repo> /opt/resurgam
cd /opt/resurgam
```

## 4. Bring up the Docker Compose stack (API + UI + DB)

```sh
cd /opt/resurgam/appliance
cp .env.example .env
```

Fill in `.env` with real values (see the generation commands already in `.env.example` for
`SESSION_SECRET`, `SECRETS_LOCAL_ENCRYPTION_KEY`, `BREAKGLASS_PASSWORD_HASH`). **Leave `DEV_MODE`
unset** -- `config.py`'s own startup validator refuses to boot the API with a missing/weak secret
unless it's explicitly set, and this compose file never sets it, so the API itself will tell you
clearly if something in `.env` is still a placeholder.

`docker-compose.yml` runs prebuilt, version-tagged images (`resurgam-backend:${RESURGAM_VERSION}`);
on a manual install you build them yourself with the build override (keep `RESURGAM_VERSION` in
`.env` equal to the repo's `VERSION` file):

```sh
docker compose -f docker-compose.yml -f docker-compose.build.yml build
docker compose up -d
docker compose logs -f backend   # watch migrations + startup; Ctrl-C once it's serving
```

At this point `https://<RESURGAM_DOMAIN>/` is reachable (self-signed cert by default) and the API
is up, but nothing can actually index files or restore a VM disk yet -- that needs the native
workers below.

## 5. Install the native workers

**Install the Proxmox file-restore tools and host configuration** -- all four Proxmox packages
from one Proxmox VE repository release (ADR 0011), plus the tmpfiles.d entry that lets the
unprivileged workers run them. The release's own hook does both (the same script the image build
and every update run; it checks the Proxmox keyring's SHA-256 against Proxmox's docs):

```sh
useradd --system --home /var/lib/resurgam-worker --create-home --shell /usr/sbin/nologin resurgam-worker
usermod -aG kvm resurgam-worker
sh /opt/resurgam/appliance/release/host-setup.sh --online
```

**Build the venv** (mirrors what `backend/Dockerfile` does for the API container, just at a fixed
path the systemd units expect instead of inside an image layer):

```sh
cd /opt/resurgam/backend
UV_PROJECT_ENVIRONMENT=/opt/venvs/resurgam-backend uv sync --frozen --no-dev
```

**Configure each worker.** All four read `resurgam_api.config.Settings` the same way the API does
-- including its startup validator, which means every worker's environment needs a real
`SESSION_SECRET` and `SECRETS_LOCAL_ENCRYPTION_KEY` too (the workers never use the session secret
directly, but `Settings` doesn't know which process is asking). Use the **same values** as
`appliance/.env`. `DATABASE_URL` must point at the Postgres container's loopback-published port
(§4's compose file publishes `127.0.0.1:5432`), not the in-Docker hostname `postgres`, since these
processes run on the host, outside Docker's network:

```sh
for name in resurgam-collector-scheduler resurgam-file-index-worker \
            resurgam-file-restore-worker resurgam-recovery-lab-scheduler; do
  cat > /etc/default/$name <<EOF
DATABASE_URL=postgresql+psycopg://resurgam:<same POSTGRES_PASSWORD as .env>@127.0.0.1:5432/resurgam
SESSION_SECRET=<same SESSION_SECRET as appliance/.env>
SECRETS_LOCAL_ENCRYPTION_KEY=<same SECRETS_LOCAL_ENCRYPTION_KEY as appliance/.env>
EOF
  chmod 600 /etc/default/$name
done

# The scheduler that provisions Recovery Lab probe appliances also needs
# to know its own externally-reachable URL (see config.py's own comment
# on recovery_lab_public_backend_url) -- same value as
# RECOVERY_LAB_PUBLIC_BACKEND_URL in appliance/.env.
echo "RECOVERY_LAB_PUBLIC_BACKEND_URL=https://<RESURGAM_DOMAIN>/api/v1" \
  >> /etc/default/resurgam-recovery-lab-scheduler
```

**Install and start the units:**

```sh
cp /opt/resurgam/backend/packaging/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now \
  resurgam-collector-scheduler resurgam-file-index-worker \
  resurgam-file-restore-worker resurgam-recovery-lab-scheduler
systemctl status resurgam-collector-scheduler   # spot-check one; repeat for the others
```

The file-restore/file-index workers run as `resurgam-worker` (group `kvm`) with **no
capabilities at all** -- live-verified 2026-10-07 for CT and VM browsing and download (see
`../backend/packaging/systemd/README.md`). That needs `host-setup.sh` above: without its
tmpfiles.d entry `proxmox-file-restore` fails with `Permission denied` for anyone but root.

## 6. First boot: the setup wizard

Browse to `https://<RESURGAM_DOMAIN>/`. With no platform-admin account yet, you land on the
first-boot setup wizard instead of the login page -- it shows the `/dev/kvm` check from §1, then
walks you through creating the real first admin account and signs you straight in.

Once that account exists, consider disabling breakglass in `appliance/.env`
(`BREAKGLASS_ENABLED=false`) and `docker compose up -d` again to pick it up -- breakglass is the
emergency fallback (ADR 0004), not meant to be the everyday login once a real account exists.

> **Before you require MFA for everyone** (Users & Roles -> "Require MFA for all local users"):
> keep a second way in. Create a second platform admin, make sure every admin has saved their
> recovery codes, and know how to switch break-glass on from this console (break-glass sign-in
> never asks for a second factor). A single admin who loses their only authenticator is otherwise
> locked out. The step-by-step recovery is in the operator manual,
> [`../docs/manual/index.md`](../docs/manual/index.md#mfa-lockout-recovery).

## 7. Verify

- `curl -k https://<RESURGAM_DOMAIN>/api/v1/readiness` -- expect `{"status":"ready","database":"ok"}`.
- `systemctl is-active` all four worker units.
- Log in as the admin account created in §6, add a real PBS instance, and confirm a sync job runs
  on its own within the collector-scheduler's poll interval (no manual "Sync" click needed).

## 8. Updating

Appliances built from the image (`packer/`) update with **signed release bundles** -- System
Settings -> Updates in the web UI, or `sudo resurgam-update apply <bundle>` on the console, with an
automatic database backup and automatic rollback. See [`UPDATE.md`](./UPDATE.md).

A manual install from this document does not have the release layout the updater needs
(`/opt/resurgam/releases/<version>` + `current`); update it by hand:

```sh
cd /opt/resurgam && git pull
cd appliance && sed -i "s/^RESURGAM_VERSION=.*/RESURGAM_VERSION=$(cat ../VERSION)/" .env
docker compose -f docker-compose.yml -f docker-compose.build.yml build && docker compose up -d   # runs new migrations too
cd ../backend && UV_PROJECT_ENVIRONMENT=/opt/venvs/resurgam-backend uv sync --frozen --no-dev
systemctl restart resurgam-collector-scheduler resurgam-file-index-worker \
  resurgam-file-restore-worker resurgam-recovery-lab-scheduler
```

## What's not here yet

A reproducible image build that automates §1-§5 exists: [`packer/`](packer/README.md) builds a
generic qcow2 (first boot generates the secrets and starts everything). This document remains the
manual route and the reference for what that image sets up. Still planned: an OVA export of that
qcow2 -- see `README.md`'s "What's still planned".
