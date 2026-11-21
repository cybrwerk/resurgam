# RESURGAM backend worker systemd units

First-ever systemd packaging for `resurgam-file-restore-worker`/
`resurgam-file-index-worker` (ADR 0013 explicitly deferred this: "real
systemd packaging of this worker stays explicitly out of scope here...
this slice only needed a documented dev-run command"). Written 2026-09-24
as part of a credential-store/worker-isolation security review.
`resurgam-collector-scheduler.service` and
`resurgam-recovery-lab-scheduler.service` followed the same
unprivileged-user pattern once those two workers existed (2026-09-28 and
2026-10-01 respectively) -- see the capability-narrowing discussion below,
which applies only to the two file-restore/file-index workers (privileges below); the two
schedulers need no elevated privileges at all (plain PBS/PVE API calls,
no local disk/VM access).

`resurgam-host-probe.service` (2026-10-03) isn't a worker: it's a boot-time
oneshot that checks `/dev/kvm` (and, since 0.2.2, the file-restore tools) as the workers' own identity
(`resurgam-worker`, group `kvm`) and writes the result to
`/run/resurgam/host-capabilities.json`. The appliance's API runs in a
container that never gets `/dev/kvm`, so without this its setup-wizard
check reported "nested virtualization missing" on hosts that had it.
`appliance/docker-compose.yml` mounts `/run/resurgam` read-only into the
API container and points `HOST_CAPABILITIES_FILE` at the report. After
changing the host's CPU/nesting setup, `systemctl restart
resurgam-host-probe` refreshes it; no container restart needed.

## Privileges: none -- live-verified 2026-10-07

Both workers previously ran as **unattended full `sudo`** (ADR 0013), then (2026-09-24) as
`resurgam-worker` with a *guessed* capability set (`CAP_DAC_OVERRIDE`, `CAP_DAC_READ_SEARCH`,
`CAP_SYS_ADMIN`, `CAP_MKNOD`, `CAP_NET_ADMIN`). Neither is needed. They now run as the
unprivileged `resurgam-worker` user (supplementary group `kvm`) with an **empty**
`CapabilityBoundingSet`/`AmbientCapabilities`, `NoNewPrivileges=true` and the ordinary sandbox
(`ProtectSystem=strict`, `ProtectHome=true`, `PrivateTmp=true`) plus exactly two writable paths:

```
ReadWritePaths=-/run/proxmox-backup -/var/log/proxmox-backup/file-restore
```

### Why ADR 0011 saw "needs root"

Traced on a Debian 13 VM with nested KVM, the four Proxmox packages from `pve-no-subscription`,
against a local PBS (`strace -f` on the unprivileged run): `proxmox-file-restore`'s VM path
(a) creates its per-uid state directory `/run/proxmox-backup/<uid>/`, (b) binds its QMP socket
`/run/proxmox-backup/file-restore-qmp-<cid>.sock` and (c) writes/rotates the micro-VM log in
`/var/log/proxmox-backup/file-restore/`. On a stock install `/run/proxmox-backup` is
`backup:backup 0755` and the log directory `root:root 0755`, so every one of those fails with
`EACCES` for a non-root caller -- and the tool reports that as `{"message": "Permission denied
(os error 13)"}` with exit code 0, which is what ADR 0011 recorded as "requires root". The
top-level disk listing never touches either directory, which is why it alone worked
unprivileged. `/dev/kvm` and `/dev/vhost-vsock` are `root:kvm 0660`, so the `kvm` group covers
the micro-VM itself; `qemu` asking for `SO_SNDBUFFORCE` (`EPERM` without `CAP_NET_ADMIN`) is
harmless, it falls back.

`appliance/release/host-setup.sh` (run by the image build and by every update, ADR 0024) writes
`/etc/tmpfiles.d/resurgam-file-restore.conf`:

```
d /run/proxmox-backup 1775 backup resurgam-worker -
d /var/log/proxmox-backup 0755 backup backup -
d /var/log/proxmox-backup/file-restore 0750 resurgam-worker resurgam-worker -
```

With that, under exactly the units' sandbox and with zero capabilities, all of this worked:
deep VM listing (`/drive-scsi0.img.fidx/raw/data`), VM file extraction to stdout (what
`browse.download_file` uses), CT catalog dump and CT single-file restore -- first via
`systemd-run` with the units' properties, then through the real workers (file-restore jobs
created via the API, `/proc/<pid>/status` showing `CapEff`/`CapBnd` all zero, user
`resurgam-worker`), and again after a reboot (the tmpfiles entry recreates `/run/proxmox-backup`).
The `-` on `ReadWritePaths=` matters: a host without the Proxmox tools yet (an appliance before
its first host setup) has neither directory, and a missing `ReadWritePaths` target would otherwise
stop the unit from starting at all (`226/NAMESPACE`) -- and with it the updater's health check.

Not exercised: an encrypted datastore (`--keyfile`) and a Windows/NTFS guest -- neither changes
which files the tool writes; and `proxmox-file-restore extract <path> <target-dir>` to a local
directory (fails unprivileged with "failed to set ownership"; RESURGAM only uses the stdout form).

Folder and multi-selection downloads (ZIP) keep to the stdout form: a VM directory is extracted
with `extract <path> - --format zip` (the ZIP is built inside the restore micro-VM, as root there),
CT/host selections with `proxmox-backup-client restore --pattern ...` into a staging directory.
Download content is a file in `/var/lib/resurgam/file-restore` (`FILE_RESTORE_OUTPUT_DIR`, set in
the file-restore worker unit, writable via its third `ReadWritePaths`, created by `host-setup.sh`),
bind-mounted read-only into the API container -- never Postgres.

`resurgam-host-probe.service` runs as the same identity with the same two `ReadWritePaths`, and
since 0.2.2 reports every one of these preconditions (tools, restore image, devices, writable
directories -- `resurgam_worker.capability.checks.run_host_checks`) for the API's per-backup-type
file-restore capability report.

## `resurgam-lab-agent`: documented, not changed here

`restore-worker/debian/resurgam-lab-agent.service` still runs as
`User=root`, deliberately left as-is by this pass. Its privilege
requirement is murkier than the two workers above and less safe to guess
at:

- It creates `/dev/loopN` devices (`proxmox-backup-client map`) --
  typically needs `CAP_MKNOD` plus whatever udev/device-manager
  cooperation the host expects, which varies by distribution.
- It runs `qm`/`pvesm` (PVE's own CLI tools) against `/etc/pve`, PVE's
  own FUSE-mounted cluster filesystem. These tools are conventionally
  run as root on a real PVE node in every deployment this project has
  observed; whether they accept a narrower capability set at all (as
  opposed to checking for literal UID 0) is unknown and specific to how
  PVE itself is built, not something this project controls.
- Unlike the backend workers (RESURGAM's own infrastructure), this agent
  runs **on the customer's own PVE node** (ADR 0019) -- the blast radius
  of getting a narrower grant wrong (an agent that silently can't do
  part of its job, on infrastructure this project doesn't operate) is
  worse than getting it right slowly.

Narrowing this one needs its own live investigation against a real PVE
node, ideally alongside whoever eventually owns real Milestone 5
packaging work -- flagged here as a known, real follow-up, not
implemented speculatively.
