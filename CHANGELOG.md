# Changelog

Release notes for RESURGAM appliance releases. One `## <version>` section per release, newest
first -- `appliance/release/build-bundle.sh` copies the section for the version in `VERSION` into
the signed update bundle, where the appliance's Updates page shows it before you apply it.

## Unreleased

- **Zabbix monitoring**: a ready-made Zabbix 7.0 template, "RESURGAM by HTTP" -- API availability,
  usage of every datastore (warning at 80 %, high at 90 %, and when RESURGAM stops measuring), and
  status and age of the newest backup, verify, prune, garbage collection and sync task per PBS. New page
  Administration → Zabbix monitoring: a wizard that creates the read-only API token, downloads the
  template with this appliance's address filled in, and lists every value to enter in Zabbix. Manual
  section "Zabbix monitoring". API: `GET /zabbix/setup`, `/zabbix/template`, `/zabbix/ca-certificate`;
  `/zabbix/datastores/usage` now also returns `age_seconds`.

## 0.3.1

Encryption keys, safer sign-in and fixes on top of the first public beta. Update from 0.3.0 in
System Settings -> Updates (upload the bundle, or let the update source find it on GitHub).

- **Backup encryption is visible** (ADR 0031, stage 1): every backup shows whether it is client-side
  encrypted and with which key (fingerprint), the workload list shows it for the newest backup, and a
  datastore lists the keys its backups use. New alarm when a workload's newest backup uses a different
  key, or none, than the one before it -- older and newer backups then need different keys to restore.
  Filled in by the next inventory sync.
- **Encryption key store** (ADR 0031, stage 2), Administration → Encryption keys: any number of PBS
  encryption keys, stored by fingerprint. RESURGAM now uses exactly the key a backup was made with,
  and no key for unencrypted backups -- before, one key per datastore broke every unencrypted backup
  in that datastore. Keys can be downloaded as keyfile or as a printable key sheet (PDF with QR code),
  and all keys exported to one passphrase-encrypted file and imported into another RESURGAM. Each
  download or export needs your password or authenticator code, is written to the audit log and
  announced to the platform admins by mail. Tenant admins see their tenant's keys on the tenant panel
  and may download them only if a platform admin allows it for that tenant. New alarms: backups
  whose key RESURGAM does not have; keys without a confirmed copy outside RESURGAM. Keys uploaded per
  datastore before are moved into the store automatically. Starting an Instant Recovery, Disk Mount
  or lab run on a backup whose key is missing now says so up front.
- **Key downloads resist password guessing**: a wrong password or authenticator code when confirming
  a key download, key sheet or export is written to the audit log (`step_up_confirmation_failed`);
  after 5 within 15 minutes that account cannot confirm anything until the 15 minutes have passed
  (API: `429 confirmation_locked`).
- **Sign-in resists password guessing**: 5 wrong passwords or codes within 15 minutes lock that
  sign-in for the rest of the 15 minutes, even for the right password -- local sign-in (per email
  address), break-glass, authenticator and recovery codes, and changing the password. A successful
  sign-in starts the count again; the sign-in page says how long to wait (API: `429 login_locked`).
  See the manual, "Too many wrong passwords".
- **Host backups with other archive names**: files from a host backup whose archive is not called
  `root.pxar` (for example `proxmox-backup-client backup data.pxar:/srv`) can now be downloaded,
  zipped and indexed -- before, browsing worked but every download failed. A host backup with several
  archives shows each one as a top-level folder.
- **Wrong address**: opened under any name or IP other than `RESURGAM_DOMAIN`, the appliance now says
  which address to use instead of showing an empty page (API: `421 unknown_host`).
- **Ephemeral file indexing** is marked experimental. It waits for the temporary copy to finish
  booting and is more patient with its guest agent, but it needs fast PBS storage.
- Security updates of third-party packages (Pillow, Authlib, PyJWT, brotlicffi, Mako, Vite and
  others).

## 0.3.0 — first public beta

The first public beta. Everything from the 0.2.x test releases, run through a full function test
against a real PVE 9.2 / PBS 4.2 pair before release. Read the [beta notes](docs/BETA.md) before
deploying: tested versions, known limitations, how to give feedback.

Highlights since the last public notes:
- **Instant Recovery** (0.2.13–0.2.15): start a VM straight from its PBS backup within minutes,
  move it to storage while it keeps running, or discard it.
- **Restore isolation** covers every network card of a restored guest (0.2.15).
- **Disk Mounts** and **agent-built lab templates** work on PVE 9 (0.2.15).
- **Recovery Lab** waits for guests booting from their backup and cleans up after itself on the
  node (0.2.15).
- **Safeguards:** nobody can lock out the last platform admin; datastore maintenance messages may
  contain commas (0.2.16).

New in 0.3.0:
- **File indexing (guest agent):** starting the file scan is retried when the guest agent does not
  answer in time (a guest still booting from its backup). The scan takes a lock in the guest first,
  so a retry can never run two scans side by side.

## 0.2.16

Pre-beta test release (the public beta will be 0.3.0).

From the second part of the function test (2026-11-21), on the parts that touch production:
- **Users:** nobody can disable their own account, and the last active platform admin can neither
  be disabled nor lose that role (the break-glass account does not count). Before, an admin could
  lock everyone out except break-glass.
- **Datastore maintenance mode:** a message containing a comma (for example "Storage upgrade, back
  at 14:00") was rejected by PBS. Messages are now sent quoted, as PBS expects.
- **File indexing (ephemeral method):** the temporary VM's overlay was placed on the tenant's target
  storage, which PVE refuses for LVM storages; it now always uses the lab agent's overlay storage.

Known problem: ephemeral indexing can still fail on guests that boot slowly from their backup (the
guest agent answers, but starting the file scan times out).

## 0.2.15

Pre-beta test release (the public beta will be 0.3.0).

Fixes from a full function test on a real PVE 9 / PBS 4 appliance (2026-11-21).
- **Restore, network isolation:** a full restore now handles every network card, not just the
  first. "Isolated" without an isolated bridge configured used to change nothing at all, so a
  restored VM kept its production networks (VLANs) and would have joined them when started. Now:
  "none" and "isolated" without a bridge unplug every card in place (`link_down=1`), "isolated"
  with a bridge moves every card there, "custom" connects the first card and unplugs the rest.
  Card models and MAC addresses are kept. Containers go through the same step.
- **Restore Center and Disk Mounts:** backups without any tenant (neither the backup group nor its
  datastore assigned to one) can be picked ("No tenant"). Before, they could not be selected in the
  wizard at all.
- **Recovery Lab:** the guest-agent check (Level 1) waits up to 15 minutes for a guest booting
  from its backup instead of failing on the first try. A run that was never started (for example
  because its lab network is missing) no longer starts by itself when polled.
- **Recovery Lab cleanup:** the lab agent now also removes the backup mappings of a run's VMs and
  the probe appliance's cloud-init file once the run's VMs are gone. Both stayed on the node before.
- **Disk Mounts** work again: the agent's guest-agent check used a command name PVE rejects, so no
  share helper ever became ready, and the helper's script could not be started directly.
- **Lab templates built by the paired agent** (probe appliance, Disk Mounts helper) work on PVE 9:
  the tool check failed under the node's `/bin/sh`, and PVE 9 refuses the seed ISO from a plain
  path (it now sits on the snippet storage as an ISO volume and is removed afterwards).

## 0.2.14

Pre-beta test release (the public beta will be 0.3.0).

Instant Recovery fixes from the first test on a real PVE 9 / PBS 4 (ADR 0030, "Live verification
results"). Tested there: Windows 11 with EFI and TPM starts from its backup, and the live move to
storage, Finish, Discard and an agent restart work.
- VMs with a TPM start again: the TPM state is attached as v2.0 right away (PVE refused changing the
  version after the import).
- Isolated network without a chosen bridge: the NIC keeps its VLAN tag, so it can't end up on the
  bridge's untagged network if someone sets its link up.
- If PBS restarts or the connection to it drops while a VM runs from its backup, the VM's disks stop
  working for good (the backup mapping does not reconnect). The agent now reads from every mapping
  once a minute and marks the Instant Recovery as failed after two failed reads (about two minutes),
  with an alarm; Discard cleans it up. The warning in the panel and the manual says so plainly.
  Keeping such a VM alive through a short PBS outage is not possible yet.

Upgrading the lab agent: pair the node again after the update (PVE clusters → the cluster → Set up
lab compute → Lab agent).

## 0.2.13

Pre-beta test release (the public beta will be 0.3.0).

New: **Instant Recovery** (VMs only, ADR 0030). A VM starts straight from its PBS backup within
minutes instead of waiting for a full restore; its disks are moved to storage later, while it
keeps running.
- Restore Center: choose **Instant Recovery** as the restore method. It uses the same wizard,
  checks and four-eyes approval as a full restore. The VM runs on the node of the cluster's lab
  agent and gets the backup's own hardware config (machine type, controllers, NIC models, UUID).
- **Move to storage** copies each disk to the target storage while the VM runs (PVE disk move,
  one disk after another, optional bandwidth limit). Afterwards it is an ordinary VM.
  **Discard** deletes the VM and everything written since the start. The backup is never written to.
- The original network is refused while the original VM runs, and MAC addresses are only kept
  when the original VM is not running. RESURGAM never stops or changes the original VM.
- "VMs running from backups" in the Restore Center lists every Instant Recovery still using a
  backup, with how much it wrote and how full the overlay storage is.
- Alarms: a VM running from its backup longer than 24 h (adjustable per cluster), overlay
  storage 80 % full, and a failed Instant Recovery that left something behind.
- PBS connections show a warning while a VM reads from them. Setting a datastore to maintenance
  mode asks for confirmation first.
- PVE clusters: new switch **Allow Instant Recovery** (off by default). The cluster needs a paired
  lab agent and an overlay storage, both set up with the existing setup wizard.

Known problems, fixed in 0.2.14: a VM with a TPM fails to start, and a PBS outage while a VM runs
from its backup goes unnoticed.

## 0.2.12

Pre-beta test release (the public beta will be 0.3.0).

File indexing:
- Backups taken several times a day kept losing their index: the scheduled run indexed the
  newest snapshot, and PBS pruned it a few hours later. For such backups the schedule now indexes
  the last snapshot of the previous day, the one PBS keeps as the daily. Backups taken once a day
  are unchanged and still get their newest snapshot.
- RESURGAM now reads each PBS server's timezone (PBS counts prune days in it). It is shown in the
  PBS connection's details as **Server timezone**.

## 0.2.11

Pre-beta test release (the public beta will be 0.3.0).

New in the GUI:
- Datastores: **Encryption key** (admins only). Upload or paste the PBS key file of a datastore
  with client-side encrypted backups, replace it, or remove it. RESURGAM needs it to index, browse
  and restore files from those backups. A stored key shows as "Key stored"; the key itself can
  never be displayed again. Password-protected keys are rejected (PVE doesn't support them either).
- Backup jobs: a job disabled "for N hours" now has **Change** next to "Cancel timer", to set a
  new duration counted from now.

File indexing:
- The Windows Update download cache (Delivery Optimization) is skipped by default. Backups still
  on the unchanged default list get it automatically; edited lists are left alone.

## 0.2.10

Pre-beta test release (the public beta will be 0.3.0).

File indexing (from a live run on a Windows 11 and a Linux Mint VM):
- Windows compatibility folders (`Dokumente und Einstellungen` / `Documents and Settings`,
  `Programme`, `Anwendungsdaten`, `Startmenü/Programme` and similar) are skipped by default. They
  only point to other folders, so `Users` and `Program Files` were indexed twice. English and
  German names are covered; for other Windows languages, add the names to the backup's exclude
  list.
- Also skipped by default: `Windows/servicing/LCU` (unpacked Windows updates), Timeshift system
  snapshots and `/usr/share/icons`. Together these were most of the entries on the test VMs.
  Backups still on the unchanged default list get the new patterns automatically; edited lists
  are left alone.
- A folder no longer goes missing from the index when PBS briefly reports a lock error while
  listing it; the listing is retried.
- The database container gets more shared memory, so maintenance (VACUUM) of a large File Index
  no longer fails.

## 0.2.9

Pre-beta test release (the public beta will be 0.3.0).

New:
- Backup jobs: "Disable" can re-enable the job automatically after a number of hours (up to 7
  days). The timer is shown on the job and can be cancelled or changed; the scheduler re-checks
  PVE before switching the job back on.
- Personal API tokens (Profile -> API tokens): each token carries only the roles you pick (from
  the roles you hold yourself), is read-only by default, can be limited to client IP ranges, and
  always expires (at most 18 months). Admins see and can revoke every user's tokens.

File indexing (from a full live run on a real appliance):
- Scheduled indexing indexes only the newest snapshot of each enabled backup instead of queueing
  its whole history (a VM with hundreds of snapshots no longer gets hundreds of jobs). Older
  snapshots can still be indexed on demand.
- Linux kernel headers (`/usr/src/linux-*`) and kernel modules (`/lib/modules`) are skipped by
  default -- they were 75% of a typical Ubuntu VM's index. Backups still on the unchanged default
  list get the new patterns automatically; edited lists are left alone.
- VM indexing is much faster: the pause between folder listings is down from 0.2 s to 0.05 s.
- The file-index and file-restore workers wait for the database at boot instead of crashing once.

## 0.2.8

Security and reliability fixes from a full review of 0.2.4-0.2.7.

Security:
- Lab node pairing: the one-time code no longer travels in the unverified first download; the
  command passes it to the script, which uses it only over the appliance-CA-verified connection.
  Cluster names with control characters are refused and never reach the pairing script raw.
- Mandatory MFA: an enrollment token is single-use and only valid until its own factor is set up
  (no replay, no stale tokens after another enrollment or an admin reset).
- Branding logo: only PNG, JPEG or WebP (checked by content, max. 1 MB); served with nosniff and a
  sandboxing CSP. The web server sends nosniff, Referrer-Policy and X-Frame-Options headers.
- The backend container now sees only the appliance's CA certificate (new `ca-export` sidecar),
  no longer Caddy's private keys.
- Update source: the stored token is never sent to another host (test with a changed URL, or a
  saved repository on a different host clears it); no token over plain HTTP after a redirect.
- Lab agent: storage volumes it may delete are restricted to its own snippets.
- Break-glass is always a platform admin, so emergency access can actually fix an MFA lockout.

Fixes:
- Lab agent survives backend outages (e.g. during an appliance update) without losing a running
  template build or its result.
- Backup Jobs -> Run now: no more missing log lines, long logs are fetched to the end, the job's
  retention settings now apply as on a scheduled run, guests on other nodes are reported.
- File restore: browsing and downloads run in separate lanes -- a long ZIP no longer blocks
  everyone's file browser; very long file names no longer crash the worker; running jobs can be
  cancelled.
- Disk Mounts: teardowns can no longer hang in "stopping" (gives up after 10 attempts; admins can
  force-finish); static addresses inside the lab uplink range are refused; deleting a cluster
  with ended mounts works (active ones must be stopped first).
- SSH-guided and agent-run lab setup jobs no longer run at the same time on one cluster.
- Mail settings: an SMTP password can be set again after it was cleared.

UX and docs:
- Success messages look like success; Disk Mounts usable on phones; Updates tab shows a summary
  with Edit/Remove and an "available" badge; Run now uses the job's name and respects permissions;
  recovery codes can be copied/downloaded during enrollment; the last required factor can't be
  removed from the profile; white-label app title used in more places; German wording cleaned up.
- Operator guide in the manual (pairing, Disk Mounts, Run now, MFA incl. lockout recovery,
  white-label); config export now includes the global MFA switch, the app title and the update
  source (never its token). ADR 0028 (mandatory MFA).

New:
- Dashboard: "Overdue Backups" opens in a window with every overdue workload -- tenant, datastore,
  last backup and age, sortable, searchable and filterable by status.
- Alert mailer per tenant: alarm mails (overdue/critical backups by protection policy, datastore
  capacity) to one or more recipients, warnings or critical only, optional resolved mails and
  reminders, test mail, delivery status with automatic retry. One summary mail per change, never one
  per alarm. Included in the configuration export.
- API documentation from the WebGUI: account menu -> "API documentation" (Swagger UI) and "Download
  OpenAPI (JSON)". The docs now live at `/api/docs`, `/api/redoc` and `/api/openapi.json` (were
  `/docs` and `/openapi.json`, which the appliance's proxy never forwarded).

API changes: new `GET`/`PUT /customers/{id}/alert-mailer` and `POST /customers/{id}/alert-mailer/test`;
`PUT /customers/{id}/notification-settings` leaves the alarm fields unchanged when they are omitted, and
`alarm_notification_email` may hold several comma-separated addresses. `POST /pve-clusters/{id}/backup-jobs/{job}/run` returns `skipped` (objects with
`vmid` and `reason`) instead of `skipped_vmids`; task progress has `more`; the pairing script is
served at `/lab-setup/bootstrap/p/{pairing_id}/script` (the old per-code URL is gone).

## 0.2.7

- White-label: "Restore defaults" (logo, colors and title back to RESURGAM's own look), "Load
  defaults" and "Remove logo" in the branding form, and a custom app title shown in the browser
  tab, top bar, login page and PDF report footer.
- Report Center: the PDF download shows that the report is being generated and reports failures;
  a new generation appears in the report history right away.
- Users & Roles: "Require MFA for all local users" -- a global switch next to the per-user flag.
- Backup Jobs: "Run now" starts a job immediately (like Proxmox's own button) and opens a live
  status viewer with each node's task log.
- Fix: several confirmation dialogs on one page all announced the first one's title to screen
  readers.

## 0.2.6

- Lab node pairing: the Lab Compute wizard pairs a lab node with one command pasted into the node's
  Proxmox shell (one-time code, script fingerprint pinned, appliance CA included) -- no SSH login or
  password entered in RESURGAM. The paired agent then builds the probe appliance template and the
  Disk Mounts share helper template itself, from structured settings only. SSH and manual setup
  remain as alternatives.

## 0.2.5

- Mandatory MFA: administrators can require a second factor per user (Users & Roles -> user ->
  "Require MFA at next login"). A user without one is taken through setting up an authenticator app
  or security key at their next sign-in; the last factor can't be removed while it is required.
  "Reset MFA" removes a user's factors when they lost their phone or key. The Users list shows each
  user's MFA state.
- Mobile layout: button rows, page and panel headers and tabs wrap instead of running off the
  screen; touch-sized controls (40px) and 16px inputs on phones; table row actions stay pinned to
  the right edge; key/value lists stack on phones; Datastores tree, dialogs and forms fixed for
  narrow screens.

## 0.2.4

- Update source (System Settings -> Updates): point RESURGAM at its GitHub repository (or any
  GitHub/GitLab repository) with a personal access token; it checks for newer releases
  (automatically every 24 h or on demand) and downloads a release's signed bundle straight into
  the verify/apply flow -- no more uploading bundles by hand.
- Disk Mounts (Recovery -> Disk Mounts): a disposable helper VM shares the disks of a VM backup
  read-only over SMB or NFS, with per-mount network, credentials, lifetime and approval.
- File restore: folders and multi-selections download as one ZIP; every download is saved through
  a short-lived link, so large files stream straight to disk.

## 0.2.3

- Installs the file-restore tools on the appliance host (offline, from this bundle): CT/host and VM
  file browsing and single-file download work after this update. No other changes.

## 0.2.2

- File restore on the appliance: the workers and update bundles now bring the Proxmox tools
  (proxmox-backup-client, proxmox-file-restore and the micro-VM it boots). This release installs the
  new updater; the tools themselves arrive with the next update (0.2.3), which this updater installs
  offline from the bundle.
- File restore works with PBS servers using a pinned (self-signed) certificate fingerprint.
- Container and host snapshots open directly in the file browser; only VM snapshots ask before
  mounting.
- Recovery Lab setup: guided agent install over SSH (or manual download), the probe appliance
  template built by RESURGAM, and VLAN tag + DHCP or static IP range for the probe uplink.
- Clear messages per backup type when file-restore tools are missing.

## 0.2.1

- First release delivered to an appliance as an in-place update (signed bundle via System Settings ->
  Updates), verifying the update path on a real appliance. No functional changes.

## 0.2.0

- Appliance updates via signed release bundles (System Settings -> Updates, or `resurgam-update` on
  the console), with an automatic database backup before every update and automatic rollback on
  failure (ADR 0024).
- Configuration export/import for moving settings to a fresh appliance (secrets excluded).
