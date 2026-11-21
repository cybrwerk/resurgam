# RESURGAM public beta: notes

RESURGAM is in **public beta**. It is under active development and already runs against real
Proxmox Backup Server and Proxmox VE environments, but it has not yet been run by many different
operators. This page explains what that means for you.

## What "beta" means here

- **Feature-complete enough to use, not yet battle-tested.** The core workflows (inventory,
  monitoring, file restore, Restore Center, Instant Recovery, Disk Mounts, Recovery Lab, reports,
  billing) work end to end and were run through a full function test against a real PBS/PVE pair
  before this release, but they have mostly been exercised in one environment so far. Expect rough
  edges.
- **Things can change between releases.** UI, API and configuration may change without a
  deprecation period. Database migrations run automatically on update. We avoid breaking changes
  where possible and always describe them in [CHANGELOG.md](../CHANGELOG.md).
- **Run it next to your existing setup, not instead of it.** RESURGAM never replaces PBS or PVE;
  they remain the systems of record for your backups. Don't make RESURGAM your only way of
  knowing whether backups work until you trust it in your environment.
- **Best-effort support.** Issues and questions are answered on GitHub as time allows. For
  commercial support, contact [cybrwerk](https://cybrwerk.ch).

## Tested with

- Proxmox Backup Server **4.2**
- Proxmox VE **9.2**

Older versions may work but are not tested. Please report what you run in your bug reports.

## Before you start

- **Use least-privilege API tokens.** The onboarding wizard shows which privileges each feature
  needs. Read-only inventory and monitoring need far fewer rights than restore or Recovery Lab.
- **Back up the appliance itself.** Use *Administration → System Settings → Backup* (configuration
  export) and/or a PVE backup of the RESURGAM VM. Updates take an automatic backup and roll back
  on failure, but a separate copy is cheap insurance.
- **Keep the appliance updated.** Only the latest release gets fixes during the beta (see
  [SECURITY.md](../SECURITY.md)).

## File indexing: what to expect

- **Off until you switch it on.** File indexing runs only for backups that are assigned to a
  customer, with File Index enabled for that customer *and* for the backup itself. A fresh
  install indexes nothing until you set this up.
- **Container backups are fast, VM backups are slow.** A container backup is read in one go
  (seconds). A VM disk is read folder by folder through a small helper VM, so the **first** index
  of a large VM can take an hour or more. Common system folders (e.g. Windows `WinSxS`, Linux
  kernel headers) are skipped by default to keep this down; you can change the list per backup.
- **Scheduled indexing indexes the newest backup.** Each scheduled run indexes the newest
  snapshot of every enabled backup. Older snapshots are not backfilled automatically; index them
  on demand if you need them.

## Known limitations

- **Guest-exec file indexing** (indexing through the QEMU guest agent) is not recommended as the
  primary indexing method. It can fail on some Windows guests, and the root cause is not yet
  isolated. Use the default indexing method where possible.
- **Encrypted PBS backups:** RESURGAM can use a datastore encryption key you upload, but this has
  not been verified against a real encrypted backup yet, and RESURGAM cannot generate keys itself.
- **Restore Center** checks the target, but does not create missing PVE storage definitions for
  you. If the PBS datastore isn't configured as storage on the target PVE node, it tells you what
  to add.
- **Appliance image:** only a qcow2 image is available; an OVA export is planned.
- **Instant Recovery** runs the VM straight from PBS until you move it to storage. While it does,
  the VM is only as fast as PBS can read (on a slow PBS a Windows guest can take ten minutes or
  more to boot), and **if PBS restarts or the connection to it drops, the VM's disks stop working
  for good**: the guest gets I/O errors, even after PBS is back. RESURGAM marks such a recovery as
  failed within about two minutes; only *Discard* is left then. Move the VM to storage soon, and
  don't restart PBS meanwhile. VMs only, no containers.
- **Ephemeral file indexing** (a temporary copy of the VM started from the backup) depends on how
  fast that copy boots from PBS. On slow PBS storage, large guests can still time out.
- **Release update source** is a newer feature and less field-tested than the rest. Please report
  anything unexpected.

The [operator manual](./manual/index.md) has details and further caveats per feature.

## Giving feedback

Feedback from real environments is the most valuable contribution right now:

- 🐞 **Bugs:** [open a bug report](https://github.com/cybrwerk/resurgam/issues/new/choose)
  with your RESURGAM, PBS and PVE versions.
- 💡 **Ideas and questions:** [Discussions](https://github.com/cybrwerk/resurgam/discussions).
- 🔒 **Security issues:** privately, see [SECURITY.md](../SECURITY.md).

Please remove real hostnames, IPs, tokens and customer names from anything you post.
