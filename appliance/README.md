# appliance

Manifest §5 "Appliance-Modell": a QEMU/KVM VM, Debian 13 base, with a Docker Compose stack (API + UI/
reverse-proxy + PostgreSQL, this directory) alongside native systemd-managed privileged workers
(`backend/packaging/systemd/`, not containerized -- they need root + the real Proxmox CLI tooling for
VM-path restores, ADR 0011).

**Deploying for real?** See [`DEPLOY.md`](DEPLOY.md) -- the end-to-end walkthrough that ties both
halves together on one real VM. Everything below is a reference of what exists, not a how-to.

## What's here today

- `docker-compose.yml` + `.env.example` -- the containerized stack. `backend/Dockerfile` builds the API
  (runs `alembic upgrade head` on every start, then uvicorn); `frontend/Dockerfile` builds the UI and
  bundles it into a Caddy image that also reverse-proxies `/api/*` to the backend on the same origin (no
  CORS needed) and self-signs a TLS cert by default (`Caddyfile`). Live-verified end to end 2026-10-01:
  fresh containerized Postgres, every migration up to head (`backend/alembic/versions/`), breakglass
  login, and an authenticated API call all worked through the real TLS-terminated reverse proxy.
- `backend/packaging/systemd/` -- the native units: the four workers (`resurgam-collector-scheduler`,
  `resurgam-recovery-lab-scheduler`, `resurgam-file-index-worker`, `resurgam-file-restore-worker`) and
  `resurgam-host-probe` (reports `/dev/kvm` and the file-restore tools to the containerised API). The updater units
  (`resurgam-updater.path` + `.service`) live in `release/systemd/`.
- A first-boot setup wizard (`GET/POST /api/v1/setup/*`, `SetupWizard.tsx`) -- shown in place of the
  login page until a real admin account exists; runs manifest §5.2's required nested-KVM functional test
  along the way. Live-verified end to end 2026-10-01 against a fresh appliance container.
- `config.py` refuses to start with a missing/weak `SESSION_SECRET`, `SECRETS_LOCAL_ENCRYPTION_KEY`, the
  hardcoded dev database password, or `BREAKGLASS_ENABLED=true` with no hash set, unless `DEV_MODE=true`
  is explicitly set -- this compose file never sets it, so the protection stays on by default.
- `packer/` -- a reproducible, generic (no secrets baked in) qcow2 image build that automates
  `DEPLOY.md`'s §1-§5 entirely: Docker, the native workers' venv, all systemd units, and a first-boot
  script that generates real secrets and starts everything on the real first boot. See
  [`packer/README.md`](packer/README.md). Built and boot-tested end to end 2026-10-01.
- `release/` -- signed update bundles (ADR 0024): `build-bundle.sh` builds and signs
  `resurgam-<version>.bundle`, `resurgam-update` (host CLI + `resurgam-updater.path`/`.service`)
  verifies and applies it on the appliance with a pre-update database backup and automatic rollback.
  See [`UPDATE.md`](UPDATE.md). Bundles also carry the host's Debian packages (the Proxmox
  file-restore tools) as an offline apt repository, installed by the release's own
  `release/host-setup.sh` hook.
- The file-restore/file-index workers run unprivileged (`resurgam-worker`, group `kvm`, no
  capabilities) -- live-verified 2026-10-07 on a locally built image in nested KVM against a local PBS:
  CT and VM browsing/download through the real workers (`../backend/packaging/systemd/README.md`).

## What's still planned (manifest §17 Milestone 5)

- An OVA export of the qcow2 `packer/` builds (its PVE import is verified, see `packer/README.md`).

## Local test run (not a real deployment -- see DEPLOY.md for that)

```sh
cd appliance
cp .env.example .env   # fill in real values; RESURGAM_VERSION = ../VERSION
docker compose -f docker-compose.yml -f docker-compose.build.yml build
docker compose up -d
docker compose logs -f backend   # watch migrations + startup
```

This talks to its own containerized Postgres (a fresh, empty database) -- it does not touch the shared
dev database on `localhost:55432` the rest of this repo's dev workflow uses.
