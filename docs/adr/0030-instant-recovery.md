# 0030 — Instant Recovery: run a VM straight from its PBS backup, then move it to real storage live

Status: Accepted (2026-11-16) — built in 0.2.13 (2026-11-17), live-tested 2026-11-20, fixes in 0.2.14. Open:
a PBS outage still ends a running VM's disks for good; 0.2.14 detects and reports it (see "Live verification results")

## Context

A full VM restore (Guided Restore, ADR 0012/0014) copies every byte from PBS before the VM can start. For a
500 GB file server that is hours of downtime during an incident. The user asked for a Veeam-style **Instant
Recovery** instead (2026-10-08, chosen over "attach the backup disk to a lab VM"): the VM boots from the backup
within minutes, users work on it, and its disks are moved to production storage while it keeps running.

Most of the mechanism already exists and is live-proven in the Recovery Lab (ADR 0019):

- `proxmox-backup-client map` exposes a snapshot's disk archive as a local, read-only block device without
  copying it (`/dev/loopN`).
- A qcow2 overlay on top of it (`qemu-img create -f qcow2 -b <device> -F raw`) takes the writes; the backup
  stays untouched.
- A VM config rebuilt from the backup's own `qemu-server.conf` (machine type, controller, NIC model, smbios
  UUID kept) boots from that overlay, Windows included.

What the Lab never needed: all disks of a VM, a network on purpose (not only an isolated VNet), and the
step from "running on the backup" to "a normal VM".

## Options

**A — PVE's own live-restore** (`POST /nodes/{node}/qemu` with `archive` + `live-restore=1`). PVE starts the
VM at once and restores in the background, writes go to the target storage directly. No agent needed.
Rejected as the primary path:

- The network can't be chosen. PVE starts the VM *inside* the restore call with the backup's original NIC
  config; a `net0` override is accepted by the API but ignored by that start (found live twice, ADR 0019).
  If the original bridge doesn't exist on the target node the restore fails; if it does, the VM comes up on
  production with the original MAC — possibly next to the still-running original.
- No way back. The PVE docs: "If the live-restore fails for any reason, the VM will be left in an undefined
  state … it is most likely not possible to keep any data". There is no "discard" and no "check it first,
  then commit".
- The copy starts immediately and can't be scheduled or throttled per run.

It stays a candidate for a later "fast mode" (no agent, original network wanted), not for this ADR.

**B — Map + overlay + online disk move** (the Lab mechanism, extended). Chosen.

## Decision

### 1. One more method of Guided Restore, not a separate feature

Instant Recovery is a `method` on the existing restore plan (`full` today, `instant` new). It reuses the
wizard (snapshot, target cluster/node/storage, VMID new/existing, network mode none/isolated/original/custom),
the pre-flight checks, four-eyes approval (`require_restore_approval`) and the audit trail. What's new is
only what happens after approval.

### 2. Where it runs: the lab agent, on the *target* cluster

`proxmox-backup-client map` and the overlay are host-local, so the paired `resurgam-lab-agent` (ADR 0019/0027)
does them, on the node the VM will run on. Today the agent only exists on lab-compute clusters; a production
cluster gets a new switch **"Allow Instant Recovery"** (`PveCluster.instant_recovery_enabled`) and the same
pairing flow. Prerequisites per node, checked by the pre-flight: agent paired with a PBS token, an
overlay "dir" storage (PVE only accepts a qcow2 file as a disk if it lives on a registered storage, ADR 0019),
and enough free space on it for the writes until the move finishes.

### 3. Start

The agent, per approved plan:

1. Fetches the backup's VM config and lists its disk archives (`drive-<slot>.img.fidx`).
2. Maps **every** disk archive (the Lab maps only the first) and creates one overlay per disk.
3. `efidisk0` and `tpmstate0` are **restored to the target storage before start**: PVE refuses to move TPM
   state while the VM runs (`clone_disk`: "cannot move TPM state while VM is running"). *Changed while
   building:* they are not mapped but restored to a file (`proxmox-backup-client restore <snap>
   drive-…img.fidx <file>`) and imported with `qm disk import … --target-disk <slot> --format raw` --
   `qm disk import` refuses anything but a regular file (qemu-server `qm.pm` `importdisk`: "non-existent
   or non-regular file"), so a mapped loop device can't be imported. PVE's own live-restore restores
   exactly these two drives before its start as well (`QemuServer.pm`). Both are a few MiB.
4. Builds the config like the Lab (original config, disks → overlays) but keeps all disks and NICs, and
   applies the plan's network mode (below).
5. Starts the VM and reports `running_from_backup`.

Encrypted backups work as in the Lab (key file passed to `map`, ADR 0022).

### 4. Network: never two of the same machine on one network

- Default VMID is **new**. "Existing" (replace the original's VMID) needs that VM to be stopped or absent.
- **Original network** is refused while the original VM runs on any cluster RESURGAM manages (status read via
  the PVE API) — two identical machines with the same IP and MAC on production is the one failure this
  feature must not cause. The operator stops the original first, or picks another mode. Unknown status
  (cluster unreachable) → warning, explicit confirmation.
- **Isolated / custom / none** as in Guided Restore. With isolated or custom the original MAC is kept unless
  the original runs (then a new one, `unique`).
- RESURGAM never stops, changes or deletes the original VM. Switching over is the operator's decision.

### 5. Finish: "Move to storage" (live) or "Discard"

**Move to storage** — the backend calls PVE's move-disk API per disk
(`POST /nodes/{node}/qemu/{vmid}/move_disk`, `storage=<target>`, `delete=1`, optional `bwlimit`). Verified in
the PVE source (`qemu-server` `move_vm_disk` → `clone_disk`): for a running VM this is a QEMU
`blockdev-mirror` with `sync: full`, i.e. it copies everything the guest sees — overlay *and* backup data —
into a self-contained disk, then switches the VM to it without a restart. It needs `VM.Config.Disk` on the
VM and `Datastore.AllocateSpace` on the target storage (the endpoint's own permission description). After
the last disk the agent
unmaps every device and deletes the overlay directory. The VM is now an ordinary VM; RESURGAM offers to add
it to a backup job.

Disks are moved one after the other (less load on PBS and storage); the operator can start the move right
away or later, and set a bandwidth limit.

**Discard** — stop the VM, destroy it, delete the overlays, unmap. Everything written meanwhile is gone (the
confirmation says so). The backup itself was never written to.

States: `pending_approval` → `approved` → `queued` → `mapping` → `starting` → `running_from_backup` →
`moving_disks` → `finishing` → `completed`; `discarding` → `discarded`; `failed` from any step (with
cleanup of what was created). `queued` (waiting for the agent) and `finishing` (all disks moved, the
agent unmaps) were added while building. A failed disk move returns to `running_from_backup`; Move can be
started again and skips disks already moved.

### 6. The risky phase: while the VM runs from the backup

Until the move finishes the VM reads from PBS through a userspace mapping on one node. If PBS, the network to
it, the mapping process or the node goes away, the guest gets I/O errors. Measures:

- **The mapping must outlive the agent.** The agent's unit uses systemd's default `KillMode=control-group`,
  so restarting the agent (e.g. during an update) would kill the `map` process with it and break the
  running VM. Each mapping runs in its own transient **scope** (`systemd-run --scope
  --unit=resurgam-ir-<plan>-<slot>`): the process `map` forks to serve the device lives in that scope,
  not in the agent's cgroup. *Changed while building:* a scope rather than a service, because a scope
  keeps the caller's environment (`PBS_PASSWORD`) and stdout/stderr, so the "mapped on /dev/loopN" line
  is read exactly as without it. Nothing needs adopting: every mapping (slot, device, scope) is stored
  in the backend as it is created, and the agent checks `systemctl is-active <scope>` once a minute.
- **Visible everywhere it matters.** The VM's row shows "Running from backup since …"; the PBS instance and
  its datastore show a warning while an Instant Recovery reads from it, and setting that datastore to
  maintenance mode asks for confirmation first. *Changed while building:* no confirmation before an
  appliance update -- the mapping and the VM live on the PVE node, not on the appliance; an appliance
  update only pauses the disk-move driver and the heartbeats for a minute. RESURGAM does not update PBS.
- **Not forever.** An alarm after 24 h (configurable) in `running_from_backup`; the overlay's size is
  reported and an alarm fires at 80 % of the overlay storage.
- A node reboot ends the mapping; the plan goes to `failed` with a clear message (data written since the
  start lives in the overlay file and is kept, not deleted, so an expert can still recover it).

### 7. Who may do what

`restore_operator` of the tenant or platform admin, as for Guided Restore; four-eyes approval when the
cluster requires it; every step audited (start, network decision incl. the original-VM check result, move,
discard).

## Confirmed by the user (2026-11-16)

- VMs only; containers stay out of scope.
- `restore_operator` (of the tenant) or platform admin may start, move and discard, as for Guided Restore.
- Alarms as proposed: 24 h in `running_from_backup` (configurable), overlay storage at 80 %.

## Implementation (0.2.13)

- Backend: `restore_plans.method` + `ir_*` columns, `pve_clusters.instant_recovery_enabled` /
  `instant_recovery_alarm_hours` (migration 0070); `instant_recovery/` (original-VM check, pre-flight
  checks, lifecycle: disk-move driver and timeouts, run by the Recovery Lab scheduler every 15 s);
  `POST /restore-plans/{id}/instant-recovery/move|discard`; agent backchannel
  `/lab-agent/instant-recovery-jobs` (list, claim, progress, status, heartbeat); three alarm types;
  `PbsInstanceOut.instant_recoveries_reading`; `confirm_instant_recovery` on datastore maintenance mode.
- Agent: `lab_agent/instant_recovery.py` (start, watch, finish, discard), `map_snapshot(scope_unit=…)`,
  `restore_image`, `qm.import_disk`, `qm.vm_status`.
- UI: method step in the Restore Center wizard, Instant Recovery panel (status, written bytes, overlay
  storage, Move / Discard), "VMs running from backups" list, cluster switch and panel, PBS warning,
  maintenance confirmation.
- Timeouts: not claimed within 15 min, or a start silent for 30 min → `failed`. The agent refuses to
  start with less than 20 GiB free on its overlay storage, or when the target storage *is* the overlay
  storage, or when the overlay directory of that VMID already holds files.
- The VM is created with `onboot`/`startup`/`protection` removed (a VM whose disks are a userspace
  mapping must not autostart after a node reboot, and protection would block Discard), CD-ROMs without
  medium, passthrough devices and disks excluded from the backup left out and listed to the operator,
  tag `resurgam-instant-recovery`.

## Live verification needed before release (on real PVE 9 / PBS 4)

1. Multi-disk VM (2 disks + `efidisk0` + `tpmstate0`, Windows 11): boots from the overlays.
2. `proxmox-backup-client restore` of `drive-tpmstate0-backup.img.fidx` / `drive-efidisk0.img.fidx` to a
   file, then `qm disk import … --target-disk tpmstate0 --format raw` and `qm set --tpmstate0 <vol>,version=v2.0`.
3. Online `move_disk` from a qcow2 overlay whose backing file is a mapped loop device: result is a
   standalone disk (`qemu-img info` shows no backing file), VM keeps running, guest checksum of a test file
   unchanged.
4. `delete=1` removes the overlay volume; afterwards unmap succeeds while the VM runs.
5. Agent restart during `running_from_backup`: VM keeps running, the `map` process stays in its
   `resurgam-ir-*.scope`, the next heartbeat arrives.
6. PBS stopped for 30 s during `running_from_backup`: what the guest does (pause vs I/O error), so the
   warning text is accurate.
7. Original-VM check: refused while the original runs, allowed after it's stopped.

## Live verification results (2026-11-20, PVE 9.2 `pve1`, PBS 4 `pbs-main`, test appliance on 0.2.13)

Test VM: backup of VM 105 (Windows 11, OVMF, `efidisk0` + `tpmstate0` v2.0, one 120 GiB SATA disk),
isolated network, target `local-lvm`. The agent was paired with the appliance by one-time code (ADR 0027,
first live pairing; it worked).

1. **Pass (single data disk).** Running from backup about 30 s after Execute; Windows reached the lock
   screen after about 11 min. Boot reads from the backup ran at about 2 MB/s (random chunk reads; PBS
   datastore on Synology iSCSI), so "instant" means minutes until the VM runs and longer until the guest
   is usable on slow PBS storage. No VM with 2 data disks + EFI + TPM was available.
2. **Failed, fixed, pass.** `qm disk import --target-disk tpmstate0` attaches the volume as TPM v1.2 (the
   default); the following `qm set --tpmstate0 <same volume>,version=v2.0` is refused ("cannot change TPM
   state version after creation", `Qemu.pm` `prohibit_tpm_version_change`). Fix: import EFI/TPM without
   `--target-disk` (lands as `unusedN`) and attach it once with the original options; PVE drops the
   `unusedN` entry itself. Result: `tpmstate0: …,version=v2.0`, no `unusedN` left, and `qmstart` logged
   "swtpm_setup: Not overwriting existing state file" (the restored TPM state is used). A failed start
   cleaned up completely (VM, volumes, scope, loop device, overlay).
3. **Pass (without guest checksum).** `move_disk` 120 GiB to `local-lvm` took about 55 min (about 20 MB/s
   sequential from the mapping) while the VM kept running; result is a raw LV, no backing file; the guest
   still answered Ctrl+Alt+Del with its sign-in screen afterwards. The guest agent never started inside
   the recovered copy (it runs on the original), so no file checksum was taken.
4. **Pass.** "removing source volume … after successful copy", then Finish unmapped while the VM ran: no
   scope, loop device, map process or overlay left; tag removed, description set.
5. **Pass.** Agent restart while running from backup: the map process kept its PID inside its
   `resurgam-ir-*.scope`, the VM kept running, the next heartbeat came about 65 s later.
6. **Fails — open.** All PBS services stopped for about 60 s while Windows booted from the backup: reads
   on the loop device fail at once (`I/O error, dev loop0`), the VM is not paused (QEMU passes read errors
   to the guest), and **after PBS is back the mapping stays broken** (`proxmox-backup-client map` does not
   reconnect; every read still fails). The guest hung. RESURGAM did not notice: the scope stays active, so
   the status stayed `running_from_backup` with fresh heartbeats. Discard still worked and cleaned up
   everything. Done in 0.2.14: (a) once a minute the agent's watch reads 4 KiB with O_DIRECT at a random
   offset of each mapping (`timeout -s KILL`, so a hanging read can't block the agent; a disk already
   moved to storage is skipped) and reports `lost` after two failed reads in a row, which fails the plan
   and raises the "failed with leftovers" alarm; (b) the risk text in the panel and the manual says a PBS
   restart or a short outage ends the VM's disks for good. Still open: (c) evaluate `rerror=stop` on the
   overlay drives plus a re-map onto a stable backing path, so a short PBS outage pauses the VM instead
   of killing it.
7. **Pass.** Original network refused for VM 105 (running), allowed for VM 104 (stopped, MACs kept);
   the VM 104 plan was only created, never executed.

Also found and fixed: on the isolated network without a chosen bridge the NIC stayed on the original
bridge but lost its VLAN tag (would land on the untagged network if its link were set up); the tag is
now kept unless another bridge replaces the original one. Confirmed live on the second run (`tag=5`).

## Consequences

- New: `restore_plans.method` and the new states, `instant_recovery_*` fields (mapped devices, overlay paths,
  move progress), `PveCluster.instant_recovery_enabled`; agent job types `instant_start`, `instant_cleanup`;
  migration; wizard step "Method" and an Instant Recovery panel (status, overlay size, Move / Discard).
- The lab agent becomes something a production cluster can run, not only a lab cluster. Its privileges on
  that cluster are the restore privileges Guided Restore already needs, plus storage for the overlays.
- Out of scope: containers (no block-device backup to map), HA-managed placement, automatic switch-over of
  the original VM, PVE live-restore as a fast mode (option A, maybe later), file-level instant access (that
  is Disk Mounts, ADR 0025).
