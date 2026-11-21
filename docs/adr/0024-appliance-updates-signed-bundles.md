# 0024 — Appliance updates via signed release bundles

Status: Accepted (2026-10-07)

## Context

The only upgrade path so far was "build a new qcow2, replace the VM", which loses every connection, customer and
account — workable for a test VM, not for an appliance someone depends on. The manifest (§17) already listed a signed
release-bundle update mechanism and a secrets-excluded configuration backup as open items. The user decided
(2026-10-07):

- **Delivery:** a signed update bundle (one file per release) — works offline/air-gapped, no registry needed; a
  download URL can be added later on top of the same bundle.
- **Trigger:** both the web UI (System Settings → Updates) and a CLI on the appliance (`resurgam-update`).
- **Safety:** an automatic database backup before every update with automatic rollback on failure, plus an exportable
  configuration backup that can be imported into a fresh appliance.

## Decision

### Versioning
One version string per release, `MAJOR.MINOR.PATCH`, in the repo-root `VERSION` file (the single source; backend and
frontend read it at build time). The running version is exposed by `GET /api/v1/version` and shown in the UI.

### The bundle
`resurgam-<version>.bundle` — an uncompressed tar containing:

| member | content |
|---|---|
| `manifest.json` | `{format: 1, version, created_at, min_upgrade_from, changelog, components: {<path>: {sha256, size}}}` |
| `manifest.json.sig` | Ed25519 signature over the exact bytes of `manifest.json` |
| `images/backend.tar`, `images/web.tar` | `docker save` of `resurgam-backend:<version>` / `resurgam-web:<version>` |
| `source.tar.gz` | `backend/ restore-worker/ frontend/ appliance/ VERSION CHANGELOG.md` (as the image build packs them, `appliance/release/pack-source.sh`: no secrets, no packer dir) |
| `wheels/` | every Python wheel the native workers' venv needs, incl. the `resurgam-api`/`resurgam-worker` project wheels, plus `requirements.txt` pinning them (installed with `uv pip install --offline --no-index`) |
| `CHANGELOG.md` | human-readable release notes |
| `host-packages.list`, `host-packages/` | *(since 0.2.2, optional but all-or-nothing)* Debian packages the release needs on the host, and a flat apt repository (`Packages` index + every `.deb` of their full dependency closure) providing them offline -- see "Host packages and the host-setup hook" |

Only `manifest.json` is signed; every other member is bound to it by its SHA-256 in `components`. Signing uses
Ed25519 (Python `cryptography`, already a backend dependency; no extra tooling). The **private key never enters the
repository or the appliance**: it lives on the release builder's machine (default
`~/.config/resurgam/release-signing.key`, created on first `build-bundle` run). The public key is committed
(`appliance/release/release-signing.pub`) and baked into the image at `/opt/resurgam/appliance/release-signing.pub`.
A bundle signed by any other key is rejected.

### Layout on the appliance
```
/opt/resurgam/releases/<version>/   one directory per installed release (extracted source.tar.gz)
/opt/resurgam/current -> releases/<version>
/opt/resurgam/appliance/            release-independent: .env, release-signing.pub, first-boot.sh,
                                    docker-compose.yml -> /opt/resurgam/current/appliance/docker-compose.yml
/opt/venvs/resurgam-backend-<version>/ ; /opt/venvs/resurgam-backend -> resurgam-backend-<version>
/var/lib/resurgam/updates/          shared with the backend container (bind mount, rw for the backend)
    incoming/<upload-id>.bundle      uploaded by the UI (or copied by the operator)
    requests/<request-id>.json       {action: "verify"|"apply", bundle: "<file>", requested_by, requested_at}
    status/<request-id>.json         written by the host updater (see below)
    history.json                     applied/rolled-back updates, newest first
/var/lib/resurgam/backups/           pre-update DB dumps (pg_dump -Fc), kept: last 5
/var/lib/resurgam/update-work/       root-only scratch: bundles are extracted AND verified here (not shared)
```
`appliance/.env` and the worker env files stay where they are and are shared by all releases (secrets never move).
Compose uses image tags (`resurgam-backend:${RESURGAM_VERSION}`), not `build:`, on the appliance.

### Who does what
The backend container never gets Docker or systemd access. It only writes uploads and request files into
`/var/lib/resurgam/updates` and reads status files back. A host-side `resurgam-updater.path` unit watches `requests/`
and starts `resurgam-updater.service` (root), which runs the same code as the CLI (`resurgam-update`).

Status file contract (`status/<request-id>.json`, rewritten atomically as the run progresses):
```json
{"request_id": "...", "action": "verify|apply", "state": "queued|running|succeeded|failed|rolled_back",
 "step": "verify|backup|stop|load_images|install|host_setup|migrate|start|health|rollback|done",
 "from_version": "...", "to_version": "...", "message": "...", "log": ["..."],
 "manifest": {"version": "...", "changelog": "...", "min_upgrade_from": "..."},
 "backup_file": "...", "started_at": "...", "finished_at": "..."}
```

### Apply sequence (host updater)
1. Verify: signature, every component hash, `version` > current, current >= `min_upgrade_from`. Stop on any failure.
2. Back up: `pg_dump -Fc` of the RESURGAM database to `/var/lib/resurgam/backups/pre-<version>-<timestamp>.dump`.
3. Stop the native workers.
4. `docker load` both images; extract source to `releases/<version>`; build the new venv offline from `wheels/`;
   *(since 0.2.2, step `host_setup`)* run the new release's own host-setup hook (below).
5. Switch `current` and the venv link; set `RESURGAM_VERSION`; `docker compose up -d` (the backend runs Alembic
   migrations on start, as it already does).
6. Health: wait for `/api/v1/readiness` = ready and the expected version from `/api/v1/version`; start the workers and
   check they stay active.
7. On any failure after step 2: switch links back, compose up the previous version, restore the database from the
   dump (only if a migration ran), restart the workers, record `rolled_back` with the reason.

### Implementation notes (2026-10-07)
- The updater extracts the bundle into root-only `update-work/` and verifies the hashes of the bytes
  it wrote, then uses only that copy -- the API container can write `incoming/`, so verifying the
  uploaded file in place and reading it again later would leave a swap-after-verify window.
- Venvs are built with `uv venv` + `uv pip install --offline --no-index -r wheels/requirements.txt`
  rather than `uv sync --offline`: `uv sync` needs the lockfile's registry sources and a build
  backend for the two local projects, neither of which exists offline. The wheels are collected in
  a `python:3.13-slim-trixie` container to match Debian 13's interpreter; the image build's own
  venv uses `uv sync --no-editable --python /usr/bin/python3` so both kinds are self-contained.
- The updater runs from the *resolved* old venv (`/usr/local/sbin/resurgam-update` resolves the link
  once), so switching the link mid-run never mixes code from two releases.
- Each switch (and each rollback) also installs that release's worker unit files and runs
  `systemctl daemon-reload`, so unit changes travel with a release.
- Health checks call the API from inside the backend container (`docker compose exec backend`), not
  through Caddy, so they don't depend on `RESURGAM_DOMAIN`/TLS/SNI.
- The readiness wait fails fast when the new backend container crash-loops (>= 3 Docker restarts,
  e.g. a failing migration) instead of waiting out the full 300 s timeout.
- `/var/lib/resurgam/backups` is mounted read-only into the backend container; dumps are mode 600.

### Host packages and the host-setup hook (added 2026-10-07, first shipped in 0.2.2)

**Why.** The appliance's native file-restore/file-index workers run every PBS CLI call --
`proxmox-backup-client` for CT/host backups, `proxmox-file-restore` (booting
`proxmox-backup-restore-image`'s micro-VM under `pve-qemu-kvm`) for VM backups (ADR 0011/0013).
The 0.2.0/0.2.1 image never installed any of them (file restore had only ever run on the dev VM, a
full PVE install), so on the first real appliance (the test appliance, 0.2.1) every file-restore job failed
with `file_restore_unavailable` ("not available here"). The API container needs none of them:
nothing in the API process calls the PBS CLI. Its capability endpoint did check its own container
-- the wrong side, it even reported `/dev/kvm` missing on a host that had it -- and now reports from
the host probe instead, per backup type. So the tools are *host* packages, and updates must be able
to deliver host packages, offline.

**Which packages, from where.** `appliance/release/host-packages.list`: `proxmox-archive-keyring`,
`proxmox-backup-client`, `proxmox-backup-file-restore`, `proxmox-backup-restore-image`,
`pve-qemu-kvm`, and `cpio` (the file-restore postinst builds the micro-VM initramfs with it but
doesn't depend on it -- found installing into a minimal Debian 13). Per ADR 0011 all four tools
come from ONE Proxmox repository release matching the Debian release: on Debian 13 that is the
Proxmox VE `pve-no-subscription` repository, the only one that carries `pve-qemu-kvm` (the
`pbs-client` repository has only `proxmox-backup-client`; neither it nor the PBS repository is
mixed in). This does **not** pull in a full PVE (no `proxmox-ve`/`pve-manager`): 222 packages incl.
dependencies, ~220 MB, mostly the ceph client libraries `pve-qemu-kvm` links against. The Proxmox
release keyring comes from `https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg`
and is checked against the SHA-256 published in both the PVE wiki ("Package Repositories") and the
PBS docs ("Debian Package Repositories"), `136673be77aba35dcce385b28737689ad64fd785a797e57897589aed08db6e45`
(the docs publish SHA-256 and MD5 for the trixie keyring, no SHA-512). It contains the "Proxmox
Trixie Release Key" `24B3 0F06 ECC1 836A 4E5E FECB A7BC D142 0BFE 778E`, confirmed (gpgv,
2026-10-07) to sign the live `trixie` `InRelease` of both `pve` and `pbs-client`.

**Bundle member.** `build-bundle.sh` resolves the list in a `debian:trixie` container against an
*empty* dpkg status, so `host-packages/` holds the complete dependency closure and installs on any
appliance whatever it already has (apt only installs what is missing or older -- verified offline
in a network-less container; a second run installs nothing). File names are sanitised to the
component-path rules, which are unchanged since 0.2.1 (no `%3a` epochs, no `~`); the `Packages`
index (`apt-ftparchive`) points at the renamed files, and every file is hashed into the signed
manifest like any other component. `bundle.check_host_repo` cross-checks index, files, sizes,
SHA-256s and the list -- at build time and again in the updater before apt sees anything.

**The hook.** The updater (0.2.2+) runs `releases/<new version>/appliance/release/host-setup.sh`
-- shipped *in the new release* (inside the signed `source.tar.gz`), not baked into the updater --
so a future release can change host state without first needing a newer updater. Contract:

- **When:** step `host_setup`, after the new release tree and venv are installed (workers already
  stopped), before anything is switched (links, units, `.env`, compose). A release without the
  file skips the step.
- **How:** as root, `sh host-setup.sh [--repo <extracted bundle>/host-packages --packages
  <extracted bundle>/host-packages.list]` (the arguments only when the bundle has host packages),
  stdin closed, 30-minute timeout, the updater's environment plus `RESURGAM_FROM_VERSION`,
  `RESURGAM_TO_VERSION`, `RESURGAM_RELEASE_DIR`. The repository sits in the updater's root-only,
  already verified `update-work/`, so the hook uses it as an apt `[trusted=yes]` `file:` source with
  its own lists directory; the host's own apt sources are neither used nor touched. The last 40
  output lines go into the status log.
- **Must:** be idempotent -- it runs again on every retry and every later update, and an operator
  may run it by hand (`sudo sh /opt/resurgam/current/appliance/release/host-setup.sh --online`
  installs from download.proxmox.com instead of a bundle); exit 0 on success; exit non-zero with a
  message otherwise. **A non-zero exit rolls the update back** -- before the switch, so the database
  is untouched and the previous release keeps running. It never removes or downgrades packages.
- **What it does today:** installs the packages (`--repo` or `--online`); writes the Proxmox apt
  source (`/etc/apt/sources.list.d/proxmox.sources`, deb822, `Signed-By` the keyring) so a later
  online upgrade stays on the same repository; writes the tmpfiles.d entry that lets the
  unprivileged workers run `proxmox-file-restore` (`/run/proxmox-backup` and
  `/var/log/proxmox-backup/file-restore` writable for `resurgam-worker` -- why: ADR 0011 addendum
  2026-10-07); loads `vhost_vsock`; verifies every tool runs and the micro-VM kernel/initramfs
  exist; refreshes `resurgam-host-probe`. The image build (`packer/scripts/provision.sh`) runs the
  same script with `--online`, so images and updates can't drift apart.
- After the switch the updater also `try-restart`s `resurgam-host-probe.service`, so the API's
  capability report reflects the new release's probe and what the hook installed.

**Host packages are not rolled back.** If a later step fails, the release rolls back but packages
the hook installed stay installed. Acceptable because they are additive (an older release simply
doesn't use them -- 0.2.1 doesn't know they exist), the hook never removes or downgrades, and the
Proxmox CLIs stay compatible within a major release (RESURGAM's wrappers are tested against their
output). A real downgrade would need each release's `.debs` kept (~220 MB per release) and apt
downgrades across the ceph libraries -- far riskier than leaving a newer, compatible tool in place.
A half-finished apt run is completed by the next run's `dpkg --configure -a`.

**Bootstrap: the test appliance (0.2.1) gets there with updates only.** The 0.2.1 updater runs from the 0.2.1
venv and knows nothing about hooks: it extracts and hash-checks `host-packages/` (format and path
rules accept it) but never uses it -- checked against `updater.py`/`bundle.py` at commit `a74ee7c`,
byte-identical to the copies in the shipped 0.2.1 bundle. A one-step path through the 0.2.1 updater
was considered and rejected: it does install the new release's unit files and start the workers, so
a worker unit could `Requires=` a root oneshot running the hook -- but that oneshot would have to
find the bundle by scanning `update-work/`, would only fire if the workers had been running, and
would stay in place forever. Instead, two ordinary updates:

1. **0.2.2**, applied by the 0.2.1 updater: ships the new updater (hook support), the new worker
   units (no capabilities, `ReadWritePaths=-...` so they start before the tools exist), the
   extended host probe and the per-side capability report. Its bundle already contains
   `host-packages/` (unused by 0.2.1). Afterwards file browsing still fails, but the message says
   what is missing on the appliance host and how to fix it. Default `MIN_UPGRADE_FROM` 0.2.0.
2. **0.2.3**, applied by the 0.2.2 updater: its `host-setup.sh` installs the packages from its
   bundle, offline; CT and VM file browsing work right after. Built with `MIN_UPGRADE_FROM=0.2.2`
   (`build-bundle.sh`'s default for every version after 0.2.2): a 0.2.1 updater would apply it
   "successfully" with the hook silently skipped, so the version rule refuses it there instead.

With internet access, `sudo sh /opt/resurgam/current/appliance/release/host-setup.sh --online` on
the console after 0.2.2 is an alternative to step 2. Verified end to end on a local QEMU/KVM VM
(the 0.2.0 image, the real 0.2.1 bundle, then 0.2.2 and 0.2.3 bundles signed with a throwaway test
key) -- see `appliance/UPDATE.md`.

### Configuration export/import
`GET /api/v1/config-backup` (platform admin) returns a versioned JSON document with instance settings, branding,
PBS instances and PVE clusters (connection data and token IDs, **never secrets**), customers and their datastore
assignments, protection policies, pricing plans, users and role assignments (no passwords, no MFA secrets),
notification settings and integrations (without client secrets). `POST /api/v1/config-backup/import` validates it
(dry run first, with a summary of what would be created/updated/skipped) and applies it into a fresh appliance;
secrets are listed as "to re-enter" afterwards. Encrypted full backups (including secrets) are out of scope here.

Implementation notes (format 1): cross-references use natural keys only (customer slug, PBS/PVE host + port,
datastore name, user email, policy name per customer), never UUIDs; the import is additive and idempotent (nothing
is deleted, re-importing the same file only applies what is still missing). Besides the sections above the document
also carries per-backup-group settings (policy, customer override, file indexing) and Recovery Lab application
groups; both reference Collector-owned backup groups, so on a fresh appliance they are skipped until the first sync
and applied by importing the same file again. An imported PBS/PVE connection stores only its token ID as a
secret-less placeholder: the health check reports `auth_failed` ("secret not set") without contacting the server,
the Collector scheduler skips it, `token_secret_set=false` marks it in the API, and entering just the secret in the
edit form completes it. Users are imported without password hashes and MFA; local users get a password via
`POST /users/{id}/set-password` (new), SSO users sign in as before.

## Consequences
- The appliance needs one last full reinstall to get the updater; every later version arrives as a bundle.
- Release builds need Docker and network access (to collect wheels); appliances don't.
- Losing the private signing key means a new key pair and one reinstall (or a manual public-key swap) for every
  appliance — back the key up.
- Downgrades are not supported by the updater (only automatic rollback of a failed update).
- Bundles grow by the host-packages repository (~220 MB on top of the images); release builds also
  need network access to download.proxmox.com.
