# Building the RESURGAM appliance image

Manifest §5.3's "später automatischer Image-Build als QCOW2" -- automates everything
`../DEPLOY.md` otherwise has an operator do by hand on a fresh Debian 13 VM, producing one
generic, deployment-ready qcow2.

## What it builds

A Packer template (`resurgam.pkr.hcl`) starts from Debian's own official `genericcloud` image
(cloud-init preinstalled, so no interactive-installer `boot_command` is needed), then:

- Installs Docker CE from Docker's own official apt repo (not Debian's `docker.io` -- Debian's
  own repos don't carry the Compose plugin at all, and lag upstream Docker releases
  independently of that gap; the official repo keeps both current via plain `apt upgrade`).
- Installs this repository's `backend/`, `restore-worker/`, `frontend/`, `appliance/`, `VERSION`
  and `CHANGELOG.md` (packed by `../release/pack-source.sh`, the same pack an update bundle ships)
  in the release layout the updater needs (ADR 0024): `/opt/resurgam/releases/<version>` with
  `/opt/resurgam/current` pointing at it, and release-independent state (`.env`, the release
  public key, a `docker-compose.yml` link into `current`) in `/opt/resurgam/appliance`. Builds the
  native workers' venv (`uv sync --no-editable`, mirroring `backend/Dockerfile`'s own approach) as
  `/opt/venvs/resurgam-backend-<version>` behind the stable link `/opt/venvs/resurgam-backend`
  that `backend/packaging/systemd/*.service` expect.
- Pre-builds the API/UI images tagged `resurgam-backend:<version>` / `resurgam-web:<version>`
  (compose runs tags, never builds, on the appliance) and pre-pulls `postgres:16` -- so the real
  first boot needs no network access or build time for any of it.
- Installs the Proxmox file-restore tools the native workers run -- `proxmox-backup-client`,
  `proxmox-backup-file-restore`, `proxmox-backup-restore-image`, `pve-qemu-kvm`, all from one Proxmox
  VE `pve-no-subscription` release for Debian 13 (ADR 0011) -- plus the tmpfiles.d entry that lets
  the unprivileged workers use them, by running the release's own `release/host-setup.sh --online`
  (the same hook every update bundle runs offline; the Proxmox keyring is checked against the
  SHA-256 from Proxmox's docs before apt trusts it). Not a full PVE: no `proxmox-ve`/`pve-manager`.
- Installs the host updater: `/usr/local/sbin/resurgam-update`, `resurgam-updater.path`/`.service`
  and `/var/lib/resurgam/{updates,backups}` -- see `../UPDATE.md`.
- Installs all four native worker systemd units, plus one more:
  `resurgam-first-boot.service` (`files/`), `ConditionPathExists=!/opt/resurgam/appliance/.env`.

**No secrets are baked in.** The image is safe to store, clone, or share. Everything
machine-specific (database password, session secret, encryption key) is generated fresh by
`files/first-boot.sh` the first time a real clone of this image actually boots -- see that
script's own comments. Breakglass starts disabled; the browser-reachable setup wizard
(`SetupWizard.tsx`, needs no login at all until a real admin exists) is the intended way in.

## Building it

```sh
cd appliance/packer
./build.sh
```

Requires `packer`, `qemu-system-x86_64`/`qemu-img`, and `/dev/kvm` access on the **build**
machine (`accelerator = "kvm"` in the template just speeds up the build VM itself -- building
does *not* need nested virtualization; only the *deployed* appliance does, for VM-path file
restores). `build.sh` generates its own build-only SSH keypair (`build_key`, gitignored) on
first run, tars up the four source directories (excluding `.git`, `.venv`, `node_modules`,
`__pycache__`), and hands both to `packer build`. Output: `output-resurgam/resurgam.qcow2`
(also gitignored -- a multi-GB build artifact, not something to commit).

A full build takes a few minutes once the base Debian cloud image is cached locally (the first
run also downloads that, a few hundred MB).

### The build user is ephemeral

`http/user-data.tpl`'s `resurgam-build` user (passwordless sudo, build-only SSH key) exists only
for the build: the template's `shutdown_command` -- the last thing Packer runs, over that user's
own SSH session -- deletes it and its sudoers entry, then resets everything else that has to
differ per clone: `/etc/machine-id` (`cloud-init clean --machine-id`; systemd derives the DHCP
client ID from it, so shared machine-ids make clones fight over one lease), the SSH host keys
(regenerated on first boot by `resurgam-ssh-host-keys.service`), and cloud-init's own state.
`build_key` therefore grants access to nothing once a build finishes.

### Getting a shell on the appliance

A finished image has **no login at all** -- root has no password and no user carries a key. The
setup wizard and everything after it are browser-only. For shell access (e.g. to change
`RESURGAM_DOMAIN`), use PVE's normal cloud-init workflow: add a **CloudInit Drive** to the VM
(Hardware → Add), set a user and your SSH public key under the VM's **Cloud-Init** tab, and boot.
cloud-init only runs when such a drive is attached, and then also applies its network settings
(e.g. a static IP) instead of the image's DHCP fallback. Detaching the drive afterwards keeps the
user it created.

## Deploying the result

The qcow2 this produces is a drop-in alternative to `../DEPLOY.md`'s §1-§5 (provision a VM,
install Docker, install the native workers) -- import it into PVE the normal way
(`qm importdisk`/`qm disk import`, attach as the VM's boot disk, set CPU type `host` per
manifest §5.2), boot it, and pick up at `../DEPLOY.md`'s §6 (the setup wizard) directly --
`resurgam-first-boot.service` has already done everything in between by the time you can log in.

## Live-verified 2026-10-01

Built and boot-tested end to end in this sandbox (which has its own `/dev/kvm` access, so the
whole pipeline -- build *and* boot-test -- ran for real, not just as unverified config):

- `resurgam-first-boot.service` completes in ~1 second on a real boot (no network wait -- the
  postgres image prefetch fix above is why) and generates a fresh `.env` + the four workers'
  `/etc/default/*` files.
- All three Compose containers (`postgres` healthy, `backend`, `web`) and all four native worker
  units come up on their own, no manual step.
- `GET /api/v1/readiness` through the real Caddy TLS reverse proxy returns
  `{"status":"ready","database":"ok"}`.
- `GET /api/v1/setup/status` correctly reports `needs_setup: true` and (in this nested-virt-less
  test VM) the expected `/dev/kvm` warning -- both exactly as `../DEPLOY.md` describes.

## Live-verified on a real PVE node, 2026-10-03

Imported into a real PVE 9.2 node (`qm disk import`, q35/OVMF, CPU `host`, virtio NIC, AMD host
with nesting enabled). Getting there found five real bugs, all fixed here: clones had no network
(cloud-init's MAC-pinned netplan file); the first fallback rule adopted Docker's veths and cut
every container off its bridge; first boot set `RESURGAM_DOMAIN=localhost`; Caddy failed every
bare-IP TLS handshake (no SNI -- fixed with `default_sni`); and the setup wizard's KVM check ran
inside the API container, which never gets `/dev/kvm` (now `resurgam-host-probe.service`, see
`../../backend/packaging/systemd/README.md`). Verified on a fresh clone of the final image:

- DHCP on the PVE NIC (`ens18`), a fresh machine-id (a distinct lease per clone), regenerated SSH
  host keys, `RESURGAM_DOMAIN` set to the VM's own IP.
- The build user is gone: `build_key` is refused (`Permission denied (publickey)`), no sudoers entry.
- Readiness `ready`, the UI served over HTTPS by bare IP, and the setup wizard reporting
  `kvm_available: true` -- including after a reboot.
- With a PVE CloudInit drive attached (user + SSH key + `ip=dhcp`): SSH login and `sudo` work,
  PVE's network config takes over (NIC renamed to `eth0`, still matched), and the whole stack
  still comes up.

## Live-verified locally, 2026-10-07: file restore on the image

Built from this template and booted in local QEMU/KVM with nested KVM (no real PVE/PBS involved):
all four Proxmox tools present (`proxmox-backup-client version` -> 4.2.8, `proxmox-file-restore help`
runs -- the tool has no `--help` flag, it answers "no such command"), none of them in the API
container, the host probe and `GET /api/v1/file-restore/capability` reporting VM, CT and host file
browsing available on the appliance host, and -- against a throwaway local PBS -- RESURGAM's own
workers (unprivileged, zero capabilities) listing and downloading files from a VM and a CT backup,
also after a reboot. (That image predated the `PBS_FINGERPRINT` fix for self-signed PBS
certificates, ADR 0011 addendum, which was copied into its venv for the test; the 0.2.1 -> 0.2.2 ->
0.2.3 update run in `../UPDATE.md` carried it unpatched.) See
`../../backend/packaging/systemd/README.md`.
