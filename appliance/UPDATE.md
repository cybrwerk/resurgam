# Updating the RESURGAM appliance

Appliances update with **signed release bundles** ([ADR 0024](../docs/adr/0024-appliance-updates-signed-bundles.md)):
one file per release, `resurgam-<version>.bundle`, that works offline and air-gapped. Every update
takes a database backup first and rolls itself back automatically if anything fails.

- [For operators: applying an update](#for-operators-applying-an-update)
  - [In the web UI](#in-the-web-ui)
  - [From GitHub (or GitLab), without uploading](#from-github-or-gitlab-without-uploading)
  - [On the console](#on-the-console)
- [What happens during an update](#what-happens-during-an-update)
- [Backups and rollback](#backups-and-rollback)
- [Host packages (the Proxmox file-restore tools)](#host-packages-the-proxmox-file-restore-tools)
- [For the release builder: building and signing a bundle](#for-the-release-builder-building-and-signing-a-bundle)
  - [Publishing the bundle as a GitHub or GitLab release](#publishing-the-bundle-as-a-github-or-gitlab-release)
  - [The signing key -- back it up](#the-signing-key----back-it-up)

> An appliance needs the release layout (`/opt/resurgam/releases/<version>` + `current`) to be
> updatable. Images built from `packer/` at version 0.2.0 or later have it. Older installs need one
> last fresh install from a current image; move their settings over with System Settings ->
> Configuration backup (export on the old one, import on the new one).

## For operators: applying an update

### In the web UI

System Settings -> **Updates**: upload the `.bundle` file, check the version and release notes it
shows after verification, then apply. The page follows the progress (backup, install, migration,
health check) and keeps a history of past updates. The UI stays unreachable for a minute or two
while the API restarts -- that is expected.

### From GitHub (or GitLab), without uploading

System Settings -> Updates -> **Update source** (ADR 0026): the repository URL is pre-filled with
`https://github.com/cybrwerk/resurgam`. That repository is public, so no token is needed. For a
private repository (your own fork or mirror) paste a personal access token too -- a fine-grained
token limited to that repository with **Contents: read-only** is enough (a classic token needs `repo`). **Test connection** checks URL and token before you save;
the token is stored encrypted and never shown again (leave the field empty to keep it, tick
"Remove the stored token" to delete it). Once saved, the panel shows a read-only summary
(repository, provider, whether a token is stored, automatic check interval, last check) with
**Edit** and **Remove**; "Available releases" sits above it, and the "Installed version" panel
shows a badge such as "0.2.8 available" when the last check found a newer release.

After saving, **Check now** lists every release newer than the installed version (with
"Check for new releases automatically" on, the collector scheduler does this every 24 h by
default -- it only looks, it never downloads or installs). **Download and verify** fetches the
release's `resurgam-<version>.bundle` into the bundle list -- stored there as
`resurgam-<version>-<id8>.bundle` (the first 8 characters of the download id, so two downloads of
the same release never collide) -- and runs the normal signature check; from there it is the same
"Update now" as for an uploaded bundle. A release without an attached bundle is listed with
"No bundle attached" and cannot be downloaded from here. A download cut off by a backend restart
shows up as failed after five minutes; its partial file is cleaned up automatically.

A GitLab project works the same way: paste its URL (`https://gitlab.example.com/group/project`),
the provider switches to GitLab, and the token needs `read_api` (an asset in GitLab's package
registry may need `api`). For a self-hosted GitHub Enterprise or GitLab under a neutral host name,
pick the provider by hand.

Firewall: the appliance needs outbound HTTPS (443) to the API host -- for GitHub `api.github.com`
-- and to the storage GitHub redirects asset downloads to. That is currently
`objects.githubusercontent.com` and/or `release-assets.githubusercontent.com` (GitHub has been
moving release assets to the latter); allow both. For GitLab it is the GitLab host itself.

The token only ever goes to the repository's API host, over https (never over plain http, and a
download redirect from https to http is refused), never to the storage a download is redirected
to. Changing the repository to another provider or API host without entering a new token removes
the stored token (the page says so), and **Test connection** against another host never sends the
stored one -- enter the token for the new repository instead. Trust still comes from the bundle signature: a compromised repository or token can
at most offer a bundle the appliance then rejects.

### On the console

Get a shell on the appliance (see `packer/README.md`, "Getting a shell"), copy the bundle over, then:

```sh
sudo resurgam-update status                      # installed version, releases, backups, history
sudo resurgam-update verify resurgam-0.2.1.bundle  # signature, hashes, version rules -- changes nothing
sudo resurgam-update apply resurgam-0.2.1.bundle   # the whole update, with automatic rollback
```

`apply` prints each step as it runs. Exit code `0` = updated, `3` = failed and rolled back (the
previous version is running again), `1` = refused before anything changed, or a failed rollback
(read the message). Console runs show up in the UI's history too.

Rules the updater enforces: the bundle must be signed by the RESURGAM release key, every file in it
must match the signed manifest, its version must be newer than the running one (no downgrades or
re-installs), and the running version must be at least the bundle's `min_upgrade_from` (otherwise
apply the intermediate release first). Only one update runs at a time.

## What happens during an update

The web UI never touches Docker or systemd. It drops the bundle into
`/var/lib/resurgam/updates/incoming/` and a request into `.../requests/`; the host-side
`resurgam-updater.path` unit starts `resurgam-updater.service` (root), which runs the same code as
the CLI and writes its progress to `.../status/<request-id>.json`:

1. **verify** -- extract the bundle into root-only `/var/lib/resurgam/update-work/` and check the
   Ed25519 signature (against `/opt/resurgam/appliance/release-signing.pub`) and every file's SHA-256;
   check the version rules. Nothing has changed yet if this fails.
2. **backup** -- `pg_dump -Fc` of the database to `/var/lib/resurgam/backups/pre-<version>-<time>.dump`.
3. **stop** -- stop the native workers that are running.
4. **load_images** / **install** -- `docker load` the two images (`resurgam-backend:<version>`,
   `resurgam-web:<version>`), unpack the source to `/opt/resurgam/releases/<version>`, build the
   workers' venv `/opt/venvs/resurgam-backend-<version>` offline from the bundled wheels.
5. **migrate** -- switch `/opt/resurgam/current` and `/opt/venvs/resurgam-backend`, set
   `RESURGAM_VERSION` in `/opt/resurgam/appliance/.env`, install the release's systemd units,
   `docker compose up -d` (the API runs its database migrations on start), wait until it is ready.
   *(since 0.2.2)* **host_setup** -- before that switch, run the new release's own
   `appliance/release/host-setup.sh`: it installs the host packages the bundle carries (offline,
   from the bundle) and applies the release's host configuration. A failure here rolls back.
6. **start** / **health** -- start the workers again; check that they stay up, the web container
   runs, and `GET /api/v1/version` reports the new version.

What stays put across releases: `/opt/resurgam/appliance/.env` and the workers' `/etc/default/*`
files (all secrets), the database volume, Caddy's certificates. The two previous releases are kept
on disk (`/opt/resurgam/releases/`, their venvs and images); older ones are removed after a
successful update.

## Backups and rollback

- **Backups:** `/var/lib/resurgam/backups/pre-<version>-<time>.dump`, one per update attempt, the
  last 5 kept. They are full `pg_dump -Fc` dumps -- they contain the encrypted connection secrets
  and password hashes, so treat them like the database itself (root-only, mode 600). The UI lists
  them; it cannot delete or download them.
- **Automatic rollback** happens on any failure after the backup: the links switch back, the
  previous release's units are reinstalled, the previous images start again, the workers restart.
  The database is restored from the pre-update dump **only if a migration ran** (the Alembic
  revision changed) -- a failure before that leaves the database untouched. The run ends as
  `rolled_back` with the reason; the history shows it.
- **If the rollback itself fails** (`failed` with "AND the automatic rollback failed"), restore by
  hand -- previous version `<prev>`, dump `<dump>` from the message:

  ```sh
  cd /opt/resurgam
  ln -sfn releases/<prev> current && ln -sfn resurgam-backend-<prev> /opt/venvs/resurgam-backend
  sed -i 's/^RESURGAM_VERSION=.*/RESURGAM_VERSION=<prev>/' appliance/.env
  cp current/backend/packaging/systemd/*.service /etc/systemd/system/ && systemctl daemon-reload
  cd appliance && docker compose stop backend
  docker compose exec -T postgres psql -U resurgam -d postgres -c 'DROP DATABASE resurgam WITH (FORCE)'
  docker compose exec -T postgres psql -U resurgam -d postgres -c 'CREATE DATABASE resurgam OWNER resurgam'
  docker compose exec -T postgres pg_restore -U resurgam -d resurgam < <dump>
  docker compose up -d
  systemctl restart resurgam-collector-scheduler resurgam-file-index-worker \
    resurgam-file-restore-worker resurgam-recovery-lab-scheduler
  ```

- **Interrupted runs** (power loss, reboot mid-update) are marked `failed` ("interrupted") the next
  time the updater starts. Check `resurgam-update status` and `docker compose ps`; if the appliance is
  not healthy, use the manual steps above.

## Host packages (the Proxmox file-restore tools)

File browsing and download run in the native file-restore/file-index workers on the appliance VM,
which need `proxmox-backup-client` (CT/host backups) and `proxmox-backup-file-restore`,
`proxmox-backup-restore-image` and `pve-qemu-kvm` (VM backups) -- all four from one Proxmox VE
`pve-no-subscription` release for Debian 13 (ADR 0011). Images built from `packer/` at 0.2.2 or later
have them. Every bundle since 0.2.2 also carries them, with all dependencies, as an offline apt
repository (`host-packages/`), and the release's `host-setup.sh` installs them during the update
(step `host_setup`). They are not removed on a rollback (ADR 0024 explains why that is safe).

Check on the appliance: the file browser shows what is missing and how to install it, and
`GET /api/v1/file-restore/capability` reports per backup type which checks fail on the appliance
host; or on the console:

```sh
proxmox-backup-client version && proxmox-file-restore help >/dev/null && echo tools ok
sudo systemctl restart resurgam-host-probe && sudo cat /run/resurgam/host-capabilities.json
```

Repair or install with internet access instead of a bundle:
`sudo sh /opt/resurgam/current/appliance/release/host-setup.sh --online` (idempotent).

### Appliances on 0.2.1 or older (e.g. the test appliance): the release sequence

The 0.2.1 updater predates the host-setup step, so it takes two updates, both in the web UI:

1. **Apply 0.2.2.** Ships the new updater, the new worker units and capability reporting; file
   browsing still fails afterwards, but now says that the tools are missing *on the appliance host*
   and how to install them. (If the report says the host probe "predates file-restore tool
   reporting": `sudo systemctl restart resurgam-host-probe`, or just continue with step 2.)
2. **Apply 0.2.3** (or any later release -- all are built with `min_upgrade_from` 0.2.2, so a 0.2.1
   appliance refuses them until step 1 is done). The 0.2.2 updater runs 0.2.3's `host-setup.sh`,
   which installs the packages from the bundle -- no internet access needed. CT and VM file
   browsing work as soon as the update reports success.

Verified 2026-10-07 on a local QEMU/KVM VM with nested KVM, never against the real PVE/PBS: the
0.2.0 image, updated with the real `resurgam-0.2.1.bundle`, reproduced the test appliance's failure
(`file_restore_unavailable`, no Proxmox tools on the host); a 0.2.3 bundle was refused there
("requires at least version 0.2.2"); 0.2.2 then 0.2.3 (signed with a throwaway test key, the VM's
`release-signing.pub` swapped for it) applied by writing `requests/<id>.json` exactly as the API
does, picked up by `resurgam-updater.path` (the API upload itself wasn't used: its signature
pre-check rightly rejects a test key); the 0.2.3 `host_setup` step installed all six packages from the bundle;
afterwards RESURGAM's own workers -- unprivileged, zero capabilities -- listed and downloaded files
from a CT and a VM backup on a local throwaway PBS. Re-running `host-setup.sh` changed nothing.

## For the release builder: building and signing a bundle

Needs Docker (with network access: base images, npm, PyPI, download.proxmox.com) and `uv`, on an
x86_64 machine.

1. Bump `VERSION` (`MAJOR.MINOR.PATCH`) and add a `## <version>` section to `CHANGELOG.md` -- the
   builder refuses to run without one; that section is what the appliance shows before applying.
2. Build:

   ```sh
   appliance/release/build-bundle.sh
   # -> appliance/release/dist/resurgam-<version>.bundle (gitignored)
   ```

   It builds `resurgam-backend:<version>` and `resurgam-web:<version>` from the same Dockerfiles
   compose uses, `docker save`s them, packs the source exactly like the image build
   (`release/pack-source.sh`: never `appliance/packer`, never `.env`), collects every wheel the
   native workers need (locked set from `backend/uv.lock`, plus the two project wheels, built in a
   `python:3.13-slim-trixie` container to match Debian 13's interpreter), collects the host
   packages from `release/host-packages.list` with their complete dependency closure in a
   `debian:trixie` container (Proxmox VE `pve-no-subscription`, keyring checked against the SHA-256
   from Proxmox's docs) into `host-packages/`, writes `manifest.json` with each file's SHA-256 and
   signs it. Bundles are ~420 MB, about half of it host packages.

   Options (environment): `MIN_UPGRADE_FROM` -- default `0.2.0` up to 0.2.2 and `0.2.2` after it
   (the first updater that runs `host_setup`; older ones would skip it silently); raise it when a
   release can't be applied on top of older ones (e.g. it drops migrations an older release still
   needs); `RESURGAM_SIGNING_KEY`; `OUT_DIR`; `PYTHON_IMAGE` / `DEBIAN_IMAGE` (if the appliance's
   Debian/Python changes -- then also the suite in `host-setup.sh`).
3. Commit and push, then publish the bundle as a release so appliances with an update source find
   it (next section).

### Publishing the bundle as a GitHub or GitLab release

```sh
GITHUB_TOKEN=... uv run --project backend python appliance/release/publish_release.py
```

It creates release `v<version>` at the pushed HEAD, titled "RESURGAM <version>", with this
version's `CHANGELOG.md` section as notes, and attaches `resurgam-<version>.bundle` (the name
appliances look for; GitHub's per-file limit is 2 GiB). The builder token needs
**Contents: read and write** on the repository -- keep it on the builder, never put it on an
appliance (appliances only need read).

Options:

- `--bundle PATH` -- the bundle to attach. Default `appliance/release/dist/resurgam-<VERSION>.bundle`;
  **required when the bundle was built with `OUT_DIR`** set to another directory.
- `--repository URL` -- the repository's web URL (default: the `origin` remote);
  `--provider github|gitlab` -- default: guessed from the host name (needed for a self-hosted
  instance under a neutral name).
- `--replace` -- replace a bundle already attached to the release.
- `--prerelease` -- mark the release as pre-release (**GitHub only**; GitLab has no pre-release
  flag). Appliances skip pre-releases unless "Include pre-releases" is ticked.

For a GitLab repository: `GITLAB_TOKEN` (scope `api`); the bundle goes into the project's generic
package registry and the release links it.

The bundle carries its own signature, so any other transport (download page, USB stick) works
just as well -- upload it in the UI or copy it into `/var/lib/resurgam/updates/incoming/`.

### The signing key -- back it up

- The private key is `~/.config/resurgam/release-signing.key` (mode 600). The first
  `build-bundle.sh` run creates it and writes its public half to
  `appliance/release/release-signing.pub`, which is committed and baked into every appliance image
  (`/opt/resurgam/appliance/release-signing.pub`). The private key never goes into the repository,
  a bundle or an appliance; the builder never prints it.
- **Back the private key up offline.** Losing it means no further bundle verifies on any deployed
  appliance: you'd need a new key pair, and every appliance would need one more fresh install (or a
  manual swap of its `release-signing.pub`). The builder refuses to silently create a new key when
  `release-signing.pub` already exists.
- If the key leaks, anyone can sign bundles your appliances accept: rotate (new key pair, new
  images) and replace `release-signing.pub` on every appliance by hand.
- To test that appliances reject foreign bundles: `RESURGAM_SIGNING_KEY=/tmp/other.key
  ALLOW_FOREIGN_KEY=1 appliance/release/build-bundle.sh` (signs with an unrelated key).
