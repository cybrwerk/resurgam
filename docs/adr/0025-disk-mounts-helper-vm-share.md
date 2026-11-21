# 0025 — Disk Mounts: a disposable helper VM that shares a backup's disks read-only over SMB/NFS

Status: Accepted (2026-10-08)

> **Amended by [ADR 0027](0027-lab-node-pairing-and-agent-run-template-builds.md) (2026-10-11):** the share
> helper template can also be built by the paired lab agent on its own node (Disk Mounts → Setup → "Build with
> lab agent"), from structured settings only, besides the one-time SSH build described below. The build code now
> lives in `resurgam_worker.lab_build` (`share_helper_template`); the backend's
> `disk_mounts/share_helper_template.py` aliases it.

## Context

`Recovery → Disk Mounts` has been a NotAvailable placeholder since the MVP cut (manifest §12 deferred it). File
Restore (ADR 0011/0013) covers "give me these files"; it does not cover "let me (or a tool of mine) walk a whole
disk": a forensic look at a compromised VM, a database file copied with its own client, a Windows admin browsing
`D:\` in Explorer, an rsync of a whole tree. The user decided (2026-10-08) what the feature is:

- a **helper VM** mounts the selected disk(s) of one VM backup **read-only** and offers them as a **network share**,
  protocols **SMB and NFS**;
- the **network is chosen per mount**: bridge, optional VLAN tag, DHCP or a static address;
- **who**: the same role and optional four-eyes approval as restore plans (`restore_operator` for the backup's
  customer or `platform_admin`; `PveCluster.require_restore_approval`, creator never approves);
- **lifetime** 4 h by default, extendable, cleaned up automatically;
- per-mount **share credentials** generated and shown once; **everything audited**.

"Instant Recovery" (boot the backup as a running VM) is a separate, later feature and not part of this ADR.

What already exists and is reused:

- `resurgam-lab-agent` on the lab node maps a PBS snapshot disk zero-copy as `/dev/loopN`
  (`proxmox-backup-client map`, ADR 0019, live-proven) and talks to the backend only through the outbound
  `/lab-agent/*` HTTPS backchannel with its cluster-scoped token.
- RESURGAM builds the probe appliance template itself over a one-time SSH session (`lab_setup/template_build.py`,
  ADR 0019 addendum 2026-10-07), including the Debian 13 genericcloud image download + SHA-512 check, cloud-init
  first boot, finalize and `qm template`.
- Lab uplink VLAN + static range allocation (`lab_setup/uplink.py`, `lab_uplink_ip_allocations`).
- The approval pattern of restore plans (`restore_execution.approve/reject`, `_require_not_self_approval`).

## Decision

### 1. Where the disks are opened: in a disposable helper VM, never on the node

The agent maps each selected `drive-*.img.fidx` archive on the lab node (`proxmox-backup-client map`, read-only by
construction -- PBS serves the image read-only, ADR 0019) and attaches the loop devices to a freshly cloned helper
VM as extra SCSI disks with `ro=1` (qm.conf: "ro: Whether the drive is read-only") and a `serial=` per disk
(qm.conf: "The drive's reported serial number ... up to 20 bytes"), so the helper can tell which disk is which by
serial instead of guessing device order. `backup=0` keeps the helper's mapped disks out of any vzdump job. A bare
`/dev/loopN` path as a drive is accepted by `qm` (live-proven, ADR 0019; PVE allows arbitrary paths only for
root, which the agent is).

The node's kernel never parses the backup's file systems -- only the helper guest's kernel does. That matters:
PBS's own docs warn for `map` "Only do this with _trusted_ backups!" (`proxmox-backup-client map`, command
syntax reference); a disk from a compromised VM is exactly the untrusted case this feature exists for. The node
only sees a block device it hands through; NTFS/ext4/XFS/btrfs/LVM parsing happens inside a throwaway VM.

Read-only is enforced three times: the PBS loop device is read-only, QEMU gets `ro=1`, and the helper mounts
read-only with the options that also suppress journal/log replay (kernel docs: ext4 `noload` "Don't load the
journal on mounting"; XFS `norecovery` "must be mounted read-only"; btrfs `rescue=nologreplay`, "the tree log is
replayed even with a read-only mount! To disable that behaviour, also mount with nologreplay"). LVM volume groups
are activated with `activation/read_only_volume_list` covering the VG and `--devices` limited to the backup's
disks (vgchange(8): `--devices` "overrides the devices file"; `--readonly` is *not* used because it cannot talk to
device-mapper, i.e. cannot activate).

Per partition/volume the helper tries, in this order: NTFS (`ntfs3` kernel driver, then `ntfs-3g` as fallback --
ntfs3 refuses a volume marked dirty unless `force`, kernel ntfs3 docs; ntfs-3g mounts a dirty/hibernated volume
read-only), ext2/3/4, XFS, btrfs, vfat/exfat, LVM2 PVs (their LVs are then probed the same way). It never mounts
what it cannot read safely and **reports it instead**: `crypto_LUKS` and `BitLocker` ("encrypted -- no key"),
ReFS / unknown signatures ("unsupported file system"), `swap` ("swap, nothing to share"), a failed mount (the
mount error). The report is shown in the UI per disk/partition.

### 2. Which template: a second, RESURGAM-built "share helper" template (not the probe appliance)

Extending the probe template was rejected. The probe appliance is the Lab's router/firewall/DHCP for an isolated
network and calls the backend; adding Samba, the NFS server, ntfs-3g, lvm2, xfsprogs and btrfs-progs would grow
the attack surface of a box whose job is isolation, and couple two features' release cycles (a probe template
rebuild for a Samba fix). A second template costs one more VM template per lab cluster and reuses the whole build
pipeline (download + SHA-512, cloud-init first boot, finalize, `qm template`): `disk_mounts/share_helper_template.py`
reuses `template_build.py`'s command builders and differs only in what the first boot installs (from the Debian
mirror: `qemu-guest-agent samba nfs-kernel-server ntfs-3g lvm2 xfsprogs btrfs-progs dosfstools exfatprogs`) and in
needing no seed ISO (no RESURGAM package is installed into it). The template records
`resurgam-share-helper-template: <n>` in its description; `PveCluster.disk_mount_helper_template_vmid`
(migration 0057) points at it. The build is started from the Disk Mounts page's setup panel (platform admin) via
the same one-time SSH flow (host key confirmed first, credentials never stored) and polled through the existing
`/pve-clusters/{id}/lab-setup/jobs/{job_id}` endpoint.

The helper *logic* is not baked into the template: the backend renders it per mount into the cloud-init user data
(`disk_mounts/share_helper_guest.py`, Python 3 stdlib only -- Debian genericcloud ships python3 for cloud-init), so it
always matches the backend version and is unit-tested in the backend; the template only has to carry packages.

### 3. CT and host backups: out of scope, with a clear message

A CT/host backup is a file-level `pxar` archive, not a disk image -- there is nothing to `map`. The only PBS tool
for it is `proxmox-backup-client mount <snapshot> <archive> <target>`, a FUSE mount ("mount a file archive as a
read-only filesystem to a mount point on your host", PBS client docs). Running it would mean either (a) inside the
helper -- the PBS token would have to go into a VM sitting on a customer network, which this project never does
(ADR 0019: no PBS secret leaves the agent), or (b) on the node and passing the FUSE directory into the helper via
virtiofs -- needs a cluster directory mapping per mount and an extra daemon, i.e. a second mechanism for little
gain, since File Restore already browses and downloads pxar content (single files and ZIPs). So `POST /disk-mounts`
refuses CT/host snapshots with `disk_mount_unsupported_backup_type` and points to File Restore.

### 4. Lifecycle and who does what

All host-local work (map, clone, attach, boot, wait, unmap, destroy) is done by the agent; the backend never calls
the PVE API for a mount (it only reads node bridges for the picker). One mount = one `disk_mounts` row:

```
pending_approval --approve--> queued --claim--> provisioning --ready--> ready --extend--> ready
      |  \--reject--> rejected      |               |   \--failed (cleaned)--> failed
      |                             |               \--failed (leftovers)/stop/timeout--> stopping
      \--stop/24 h--> stopped|expired   \--stop--> stopped      ready --stop/expiry--> stopping
stopping --teardown done--> stopped | expired | failed     (teardown failed: stays stopping, retried with backoff)
```

- `POST /disk-mounts` validates everything (VM backup, 1-8 disk archives that exist in the snapshot, cluster ready
  for disk mounts, network, protocols, client restriction, lifetime 1-72 h), generates the credentials, and creates
  the row `pending_approval` (cluster requires approval) or `queued`. A `cluster_range` address is allocated when
  the mount becomes `queued` (at create or approve) and freed when it reaches a terminal state.
- The agent lists `GET /lab-agent/disk-mount-jobs` (provision jobs: `queued` mounts; teardown jobs: `stopping`
  mounts whose backoff has elapsed), claims a provision job (`POST .../claim`, locked `queued -> provisioning`; the
  response is the only place the share password and the encryption keyfile are handed out), reports progress as
  each resource comes into existence (`POST .../progress`: VMID before the clone, each mapped device right after
  mapping, the snippet volume), and finally `ready` (IPs + per-volume report) or `failed` (with `cleaned_up`).
- Provisioning on the node, in order, each step undone by the agent if a later one fails: check the job targets
  this agent's node; `snapshot files` (archives exist); `map` each archive; `pvesh get /cluster/nextid`; `qm clone`
  (linked, falling back to `--full 1 [--storage]`); `qm set` (net0 = bridge/tag, ipconfig0, `cicustom` user
  snippet, `scsiN=/dev/loopX,ro=1,serial=...,backup=0`, tags `resurgam-disk-mount`); write the snippet; `qm start`;
  wait for the guest agent; poll `/run/resurgam-share-helper/result.json` through `qm guest exec` (10 min); set the
  SMB password (below); delete the snippet.
- Teardown (`stopping`): the agent refuses to touch a VMID whose `qm config` name is not this mount's
  `rsgm-mount-<id8>` (a stale VMID must never destroy someone else's VM -- the Lab's pool-membership guardrail,
  done locally here), `qm stop`, `qm destroy --purge` (no `--destroy-unreferenced-disks`, the loop devices are not
  PVE volumes), then unmaps each recorded device only if `proxmox-backup-client unmap`'s mapping list (PBS source
  `mount.rs`: one `"<loopdev>:\t<name>"` line per mapping, name `<repo>:<snapshot path>/<archive>`) still shows that
  device bound to this mount's snapshot path -- a loop number re-used by another mapping after a node reboot is
  left alone. Failure keeps the row `stopping` with the error, retried after 1, 2, 4 ... 15 min.
- Expiry and orphans: `worker/recovery_lab_scheduler.py`'s existing 15-second loop also runs
  `disk_mounts.lifecycle.sweep` (no new process, no new systemd unit/compose service): `ready` past `expires_at`
  -> `stopping (expired)`; `pending_approval` older than 24 h -> `expired`; `queued` not claimed within 30 min (agent
  offline) -> `failed`; `provisioning` without a report for 45 min -> `stopping (failed)` so whatever was recorded
  is torn down. Because every resource is reported as it is created, a backend or agent restart never loses track
  of a VM or a mapping.
- Extend (`ready` only): `+1..24 h`, capped at 72 h from now. Stop: `pending_approval`/`queued` -> `stopped`
  directly (nothing exists yet), `provisioning`/`ready` -> `stopping`. Stop in the UI uses an in-page confirmation.
- Lifetime counts from `ready`, so slow provisioning or a pending approval never eats into it.

### 5. Credentials

- One SMB user per mount, `rsgm-<8 hex>`, and a random 24-character password (`secrets.token_urlsafe(18)`),
  returned **once** in the `POST /disk-mounts` response (so the requester sees it even when a second operator
  approves, and the approver never does).
- Until the agent claims the job, only Fernet ciphertext is stored (`share_password_ciphertext`, key derived from
  the master key with the purpose `disk_mount_share_password` -- `secrets/key_derivation.py`, deliberately not a
  `SecretReference`, which would list it on the Credentials page). The claim response hands it to the agent and the
  column is nulled in the same transaction; every terminal transition nulls it too.
- The password never touches a file: not the snippet, not the cloud-init ISO. The agent sets it after boot with
  `qm guest exec <vmid> --pass-stdin 1 -- /usr/local/sbin/resurgam-share-helper set-smb-password <user>` (qm(1):
  `--pass-stdin` "read STDIN until EOF and forward to guest agent via input-data"), which pipes it to
  `smbpasswd -a -s` (smbpasswd(8): `-s` "read its old and new passwords from standard input"). Neither the command
  line nor any log line carries it. Inside the helper Samba keeps only its NT hash in passdb, for a VM that is
  destroyed at the end.
- The audit trail records who created/approved/rejected/extended/stopped a mount, cluster/node, snapshot, disks,
  protocols, network and client restriction -- never the password. Tests assert the generated password appears in
  no audit row, no list/detail response and no log record.
- NFS has no password (AUTH_SYS), so a **client restriction is mandatory for NFS** (1-8 IPv4 addresses/CIDRs,
  nothing broader than /8, never `0.0.0.0/0`). The same list, if given, also restricts SMB (`hosts allow`).

### 6. What the helper serves

- SMB (`/etc/samba/smb.conf`, rendered by the helper script): `server min protocol = SMB3_00`, `server smb encrypt
  = desired`, `map to guest = never`, `restrict anonymous = 2`, `disable netbios = yes`, `smb ports = 445`, `load
  printers = no`, one share `[restore]` with `read only = yes`, `guest ok = no`, `valid users = <user>`, `force user
  = root` (so every file of the backup is readable regardless of its owner), `wide links = no` + `follow symlinks =
  yes` (symlinks inside a volume work; an absolute symlink from the backup, e.g. to `/etc/shadow`, can never escape
  into the helper's own file system -- smb.conf(5): wide links "Default: no"). UNC path `\\<ip>\restore`, one folder
  per volume (`disk1-part2-ntfs`, `disk1-vg0-root-ext4`, ...).
- NFS (`/etc/exports`): one line per volume, `<client>(ro,no_subtree_check,no_root_squash,sec=sys,fsid=<n>)`
  (exports(5): `fsid=` needed for file systems "not stored on a device", e.g. ntfs-3g/FUSE; set for every export so
  handles are stable). `no_root_squash` is deliberate: the export is read-only and restricted to the requester's
  clients, and reading a backup's root-owned files is the point; the default `secure` (reserved source ports) is
  kept. Mount: `mount -t nfs -o ro <ip>:/srv/resurgam/<volume> /mnt/x`.
- Network: `net0` on the chosen bridge with the optional VLAN tag (`tag=`, same syntax as the lab uplink); IPv4
  `ip=dhcp`, a manual `ip=<addr>/<prefix>[,gw=<gw>]`, or an address from the cluster's lab uplink static range (then
  bridge and tag are the uplink's). The helper never calls the backend -- readiness is read back through the guest
  agent -- so it needs no route to RESURGAM and can sit in any customer VLAN.

### 7. API surface (all with `description=` on routes and fields)

User side (`/api/v1/disk-mounts`): list (scoped to customers the caller holds restore access for), detail, create,
approve, reject, extend, stop, `GET /disk-mounts/snapshot-disks?backup_snapshot_id=` (PBS
`GET /admin/datastore/{store}/files`, apidoc.js "List snapshot files.", filtered to `*.img.fidx`), `GET
/disk-mounts/placement` (lab clusters with their disk-mount readiness) and `GET /disk-mounts/placement/{cluster_id}/
nodes/{node}/bridges`. Admin: `PUT /disk-mounts/placement/{cluster_id}/helper-template` and `POST
/pve-clusters/{id}/lab-setup/share-helper-template-build-jobs`. Agent (`/api/v1/lab-agent/disk-mount-jobs...`):
list, claim, progress, status, teardown-status.

## Consequences

- New: `disk_mounts` table, `pve_clusters.disk_mount_helper_template_vmid`, `lab_uplink_ip_allocations.disk_mount_id`
  (migration 0057); `disk_mounts/` package (lifecycle, helper script + cloud-init rendering, snapshot disk listing,
  share helper template build); `api/v1/disk_mounts.py`, `api/v1/lab_agent_disk_mounts.py`;
  `resurgam_worker/lab_agent/disk_mount.py` and new `qm` helpers (`clone`, `set`, `config`, guest exec with stdin);
  `DiskMountsPage` replaces the placeholder.
- One agent per cluster remains the supported shape (ADR 0019 addendum); a mount must target the agent's node, the
  agent fails a job for another node with a message naming its own node.
- The helper template must sit on the agent's node (or shared storage), as the probe template does.
- Operator prerequisites per lab cluster: the lab agent installed and checking in, a snippet storage, the share
  helper template built (Disk Mounts -> setup), and -- for clients to reach the share -- a bridge/VLAN where the
  helper's address is reachable from the requester's machine (TCP 445 for SMB; TCP/UDP 2049, 111 and the mountd
  port for NFSv3 -- NFSv4 needs 2049 only).

Not live-verified yet (needs a real lab node): `qm clone` of the share-helper template and the linked->full
fallback, `qm set` with several `/dev/loopN,ro=1,serial=` disks, `qm guest exec --pass-stdin`, the
`proxmox-backup-client unmap` mapping list on PBS 4.x (format taken from its source), and a VLAN-tagged/static
helper being reachable. The in-guest part (mounting NTFS + ext4 read-only from a read-only disk, smb.conf and
exports, SMB3 login and read-only enforcement) was exercised in a local QEMU VM on 2026-10-08:

- Debian 13 genericcloud + `share_helper_template.render_user_data()` as first boot: the build script installed every
  package, left `smbd`/`nfs-server` disabled and wrote `{"status":"ok"}`; `cmd_guest_finalize` ran through the guest
  agent.
- A linked overlay of that disk plus a raw test disk (MBR: NTFS with a file, ext4 with a root-only `/etc/shadow`, an
  absolute symlink to `/etc/hostname` and a relative one, swap, an unformatted partition) attached as
  `scsi-hd,readonly=on,serial=rsgm1`, with `helper_config.render_user_data()` as cloud-init: NTFS mounted via `ntfs3`
  `ro`, ext4 `ro,norecovery`, swap reported `swap`, the empty partition `no_filesystem`.
- Password set through guest-exec `input-data` (what `qm guest exec --pass-stdin` sends): from the host (`smbprotocol`
  via a port forward) dialect SMB 3.1.1 with encryption, wrong password and anonymous refused, both volumes listed,
  files incl. the root-only one readable, the relative symlink followed, the absolute one not (`wide links = no`),
  creating and deleting files `STATUS_ACCESS_DENIED`.
- NFSv3 and NFSv4 mounts from an allowed client read both volumes, writes fail ("Read-only file system", even with
  `-o rw`); a mount from an address outside `allowed_clients` got "access denied by server".
- The password appeared in no file under /etc, /var, /run, /root, /tmp; the guest agent's journal logs guest-exec
  argv only, never input-data. The test disk image's SHA-256 was unchanged afterwards. In QEMU the helper only booted
  its own disk once that disk had the boot index; on PVE the template's `boot: order=scsi0` covers this, which is why
  the agent never touches `boot`.
