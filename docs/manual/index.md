# RESURGAM operator manual

This manual documents how to run and use RESURGAM as it exists today: first the **Operator guide** (how-tos for the running appliance), then developer/tester setup of the individual components and the detailed history of each feature.

## What RESURGAM is today

RESURGAM ships as an **appliance**: a Debian 13 VM with a Docker Compose stack (API, UI/reverse proxy, PostgreSQL) plus native systemd workers. A reproducible qcow2 image is built with [`appliance/packer/`](../../appliance/packer/README.md); the manual route is [`appliance/DEPLOY.md`](../../appliance/DEPLOY.md). Installed appliances update with signed release bundles, from the web UI or the console ([`appliance/UPDATE.md`](../../appliance/UPDATE.md)). What changed in each release: [`CHANGELOG.md`](../../CHANGELOG.md).

The components are `restore-worker` (PBS file-restore wrappers, the Recovery Lab agent `resurgam-lab-agent` and probe appliance `resurgam-lab-prober`), `backend` (the FastAPI app, the Collector and the background workers) and `frontend` (the React UI, German/English, light/dark).

## End-user documentation

This page (developer/operator setup) is distinct from a real **end-user
manual** of the frontend — how to use the running app, not how to stand
it up. That manual doesn't exist yet; [`frontend-user-manual-concept.md`](frontend-user-manual-concept.md)
defines its scope, audience, structure, and a worked example chapter
(Recovery Lab) before the rest gets written.

## Operator guide

Short how-tos for the running appliance. UI labels are quoted as the English UI shows them.

- [Pairing a lab node](#pairing-a-lab-node)
- [Disk Mounts](#disk-mounts)
- [Running a backup job now](#running-a-backup-job-now)
- [Users and security: MFA](#users-and-security-mfa)
- [MFA lockout recovery](#mfa-lockout-recovery)
- [API tokens](#api-tokens)
- [White-label branding](#white-label-branding)
- [Updates and the update source](#updates-and-the-update-source)
- [Configuration export: what is (not) in it](#configuration-export-what-is-not-in-it)
- [Overdue backups on the Dashboard](#overdue-backups-on-the-dashboard)
- [Alert mailer (per tenant)](#alert-mailer-per-tenant)
- [API documentation and the OpenAPI schema](#api-documentation-and-the-openapi-schema)

### Pairing a lab node

Recovery Lab runs, ephemeral file indexing and Disk Mounts need `resurgam-lab-agent` on a PVE node
(ADR 0019, pairing: ADR 0027). Pairing installs it without any SSH login in RESURGAM.

1. Infrastructure → PVE Targets → the cluster → "Set up lab compute". The wizard steps are
   "Prerequisites", "Network", "Storage", "Lab agent", "Probe appliance", "Summary".
2. Work through "Prerequisites", "Network" and "Storage" ("Next" saves your choices to the cluster).
3. In "Lab agent", keep "Pair node with a one-time code (recommended)", pick the node and click
   "Create pairing command".
4. Click "Open node shell (Proxmox)" (you may have to sign in to Proxmox), paste the command
   ("Copy command") and press Enter. The command checks the script's SHA-256 before running it; the
   code works once and expires after 30 minutes ("Create a new code" if it ran out).
5. The script installs the agent and asks for the node's **PBS token** (`user@realm!name:secret`,
   at least `Datastore.Read` on the lab datastores). It stays on the node. Press Enter to skip it: the
   agent then only builds templates; lab runs, ephemeral file indexing and disk mounts wait until you
   add `RESURGAM_LAB_AGENT_PBS_TOKEN` to `/etc/default/resurgam-lab-agent` and run
   `systemctl restart resurgam-lab-agent`.
6. The wizard shows "Paired" by itself.
7. In "Probe appliance", choose "Build with the paired lab agent on <node> (no login)" and
   "Build template". The agent builds the template on its node and reports every step. Rebuild it
   after every RESURGAM update (the wizard warns when the template's prober version is outdated).
8. "Summary" → "Enable as lab compute".

Fallbacks, for nodes that cannot reach the appliance over HTTPS: "Guided (SSH)" (a one-time SSH
login that is neither stored nor logged) or "Manual" (download the `.deb`, install it, fill in the
env file yourself; the variables are documented in
[`restore-worker/debian/resurgam-lab-agent.default`](../../restore-worker/debian/resurgam-lab-agent.default)).
The same SSH and manual choices exist for the template build.

Upgrading the agent after a RESURGAM update: pair the node again (steps 3–6). The script installs the
agent version this RESURGAM ships and keeps a PBS token already set on the node. Pairing replaces the
cluster's agent token, so only one agent per cluster stays active.

### Disk Mounts

Recovery → Disk Mounts attaches a **VM backup's** disks read-only to a temporary helper VM on a lab
node and shares them over SMB and/or NFS (ADR 0025). Container and host backups have no disks — use
Recovery → Files for those.

Prerequisites (platform admin, once per cluster):

1. A cluster set up as lab compute with a paired agent **that has a PBS token** (see above) and a
   snippet storage.
2. Disk Mounts → "Setup" → the cluster → "Build with lab agent" builds the share helper template
   (or build it over a one-time SSH login, or enter an existing template VMID under "Share helper
   template (VMID)"). "Ready" means agent, snippet storage and template are in place.

Starting a mount (`restore_operator` for the tenant, or platform admin):

1. "New disk mount" → "Backup": pick the VM snapshot.
2. "Disks": tick the disks. Each is attached read-only; every readable partition on it is shared.
   Encrypted volumes (BitLocker/LUKS) and unsupported file systems (ReFS, ZFS) are listed but not
   mounted.
3. "Target and network": lab cluster, the node the agent runs on, bridge, optional VLAN tag, and the
   address ("DHCP", "Static address" or "From the lab uplink range").
4. "Share": protocols and "Allowed clients". NFS has no password, so it **requires** allowed clients
   (one IPv4 address or network per line, nothing broader than a /8); for SMB the list is optional and
   restricts access further. "Lifetime": 4 hours by default, 1–72 hours, counted from when the share is
   ready.
5. "Summary" → "Start disk mount on <cluster>". **Copy the password now** — it is shown only once and
   RESURGAM does not store it. Lost it? Stop the mount and create a new one.
6. When the cluster requires four-eyes approval (`require_restore_approval`, set with
   `PATCH /pve-clusters/{id}`; there is no switch in the UI yet), the mount waits in "Awaiting approval"
   until another operator — never the requester — clicks "Approve" (or "Reject"). A request nobody acts
   on within 24 hours ends by itself.
7. Once "Ready", copy the SMB path / NFS export and the user from the list. Your machine must reach the
   helper's address: TCP 445 for SMB; TCP/UDP 2049, 111 and the mountd port for NFSv3 (NFSv4 needs 2049
   only).
8. "Extend" adds hours (at most 72 h remaining in total); "Stop" removes the helper at once. When the
   lifetime ends, the helper is removed automatically (by the `resurgam-recovery-lab-scheduler` service,
   which must be running).

### Instant Recovery (0.2.13)

Instant Recovery starts a **VM** straight from its PBS backup within minutes instead of waiting for a
full restore (ADR 0030). The VM reads its disks from PBS through the cluster's lab agent; writes go to
qcow2 overlay files on the agent's node. Later, while it keeps running, "Move to storage" copies every
disk to real storage, and it becomes an ordinary VM. Containers are not supported.

Prerequisites (platform admin, once per cluster):

1. A paired `resurgam-lab-agent` **with a PBS token** on the node the recovered VMs should run on, and an
   overlay "dir" storage there. Both are set up with the cluster's setup wizard (PVE clusters → the
   cluster → Instant Recovery → "Set up agent and overlay storage"); the cluster does not have to be
   lab compute. Keep enough free space on the overlay storage for everything the VMs write until they
   are moved — the agent refuses to start with less than 20 GiB free.
2. PVE clusters → the cluster → Edit → **Allow Instant Recovery**, and optionally the alarm hours
   (default 24).
3. The cluster's API token needs `VM.Config.Disk` and `Datastore.AllocateSpace` on the target storage
   for the disk move (Guided Restore already needs both).

Starting one (`restore_operator` for the tenant, or platform admin):

1. Restore Center → pick the VM snapshot → "Restore method": **Instant Recovery**. Only clusters that
   allow it are listed; the node is the agent's node. The target storage is where the disks are moved
   to later; EFI vars and TPM state are written there right at the start. The VMID must be free —
   RESURGAM never replaces or touches the original VM.
2. Network: as in a full restore. **Original network is refused while the original VM runs** (on any
   cluster RESURGAM manages); stop the original first if you want to switch over. MAC addresses are kept
   only when the original VM is stopped or gone.
3. Review → "Create restore plan" → (approval, if the cluster requires it) → **Start Instant Recovery**.
   The status goes "Waiting for the agent" → "Mapping the backup's disks" → "Starting the VM" →
   "Running from the backup" (usually a few minutes).

While it runs from the backup the panel shows how much it has written and how full the overlay storage
is. The VM depends on PBS, the network to it and the node. If PBS restarts or one of them is gone even
briefly, the VM's disks stop working **for good**, even once PBS is back (`proxmox-backup-client map` does
not reconnect): the guest gets I/O errors and hangs. The agent reads from each mapping once a minute and
marks the Instant Recovery as failed after two failed reads (about two minutes); then only **Discard** is
left. So move it to storage as soon as you can, and don't restart PBS meanwhile — alarms fire after the cluster's alarm hours and at 80 % overlay storage. The
PBS connection shows a warning, and setting its datastore to maintenance mode asks for confirmation.
Every Instant Recovery that still uses a backup is listed under "VMs running from backups" in the
Restore Center.

- **Move to {storage}** (optional bandwidth limit in MiB/s) moves the disks one after another with PVE's
  own disk move, without a restart. A failed move returns to "Running from the backup"; start Move again
  and it continues with the disks not yet moved. When the last disk is moved, the agent releases the
  backup and the plan is "Completed". Add the VM to a backup job afterwards.
- **Discard** stops and deletes the VM and everything written since the start. The backup is never
  written to. Discard is also how you clean up after a failed start that left something behind.

Left out of the new VM, and listed in the panel: disks that were excluded from the backup
(`backup=0`), PCI passthrough devices and USB host devices. CD-ROM drives stay, without a medium.

### Running a backup job now

Jobs → Backup Jobs → "Run now" on a job row (platform admin) starts that PVE vzdump job immediately,
like a scheduled run or Proxmox's own "Run now": one vzdump task per node holding guests of the job,
with the job's storage, mode, compression, notes and mail settings and its retention (prune) settings —
so older backups are pruned by the job's retention; a job without its own retention falls back to the
storage's. It also works for a disabled job. A live window then shows each node's task log; closing it
does not stop the backup. Guests of the job that are no longer on the cluster (or, for a job bound to a
node, on another node) are listed as not backed up. The action is audited.

### Disabling a backup job for a while

Jobs → Backup Jobs (or a PVE cluster's "Backup jobs" section) → "Disable" on a job row (platform admin)
asks how long: "Never (until someone enables it)" keeps the job off until it is enabled again, or pick
1–72 hours, or a custom duration of up to 168 hours (one week). The job is disabled in PVE at once; with a
duration, its status shows "Disabled (timer)" and when it will be enabled again.

- When the time is up, RESURGAM enables the job in PVE again within about a minute (done by the
  `resurgam-collector-scheduler` service, which must be running). Before that, it re-reads the job from
  PVE: a job that was deleted meanwhile, or already enabled in PVE directly, is left alone.
- If PVE can't be reached or rejects the change, the attempt is retried every minute and the status shows
  the error until it works. The timer never expires silently.
- "Enable now" enables the job immediately and drops the timer. "Change" sets a new duration, counted
  from now (the job stays disabled until then). "Cancel timer" keeps the job disabled without a time
  limit. Disabling the job again, editing its enabled state, or deleting it also replaces
  the timer.
- Everything is audited, including the automatic re-enable (actor "Scheduler (automatic re-enable)").
- "Change" uses `PUT …/backup-jobs/{job_id}/reenable-schedule`, which can also set a timer on a job
  that is already disabled without one.

### Users and security: MFA

Administration → Users & Roles (platform admin):

- **Per user:** in the user's dialog, "Require MFA at next login". A user without a second factor is
  taken through setting up an authenticator app or security key at their next sign-in and shown
  recovery codes once. The MFA column shows Off / Active / Required · active / Required · setup at next
  login.
- **For everyone:** "Multi-factor authentication" → "Require MFA for all local users". Every local
  account then needs a second factor from its next sign-in; running sessions continue. Break-glass and
  SSO accounts are not affected (SSO users get MFA from their identity provider).
- **Reset MFA:** in the user's dialog, "Reset MFA" removes the user's authenticator app, security keys
  and recovery codes — for a lost phone or key. With MFA required they set up a new factor at the next
  sign-in. You cannot reset your own MFA here (use your profile).

> **Before enabling "Require MFA for all local users", keep a second way in:** a second platform admin,
> saved recovery codes for every admin, and a tested break-glass procedure (below). One admin with one
> lost authenticator is otherwise locked out.

### MFA lockout recovery

Try these in order:

1. **Recovery code.** On the sign-in page's second step ("Two-factor verification"), choose
   "Recovery code" and enter one of the codes saved at enrolment (each works once). Then set up a new factor on the profile page and
   generate new recovery codes.
2. **Another admin.** Any other platform admin opens Users & Roles → the locked-out user → "Reset MFA".
3. **Break-glass from the appliance console** (break-glass sign-in never asks for a second factor):
   1. On the appliance console (as root), create a bcrypt hash of a new, strong password inside the
      backend container:
      ```sh
      cd /opt/resurgam/appliance
      docker compose exec backend python -c "import bcrypt, getpass; print(bcrypt.hashpw(getpass.getpass().encode(), bcrypt.gensalt()).decode())"
      ```
   2. Edit `/opt/resurgam/appliance/.env`: `BREAKGLASS_ENABLED=true` and
      `BREAKGLASS_PASSWORD_HASH='<hash>'` — keep the single quotes, the hash contains `$` signs that
      Docker Compose would otherwise try to expand. `BREAKGLASS_USERNAME` is the user name (default
      `admin`).
   3. `docker compose up -d` (in `/opt/resurgam/appliance`) to restart the API with it.
   4. On the sign-in page open "Emergency access (break-glass)" and sign in with that user name and
      password.
   5. The break-glass account is always a platform admin (granted at every break-glass sign-in), so
      Administration is available right away.
   6. Users & Roles → the locked-out user → "Reset MFA".
   7. Switch break-glass off again: `BREAKGLASS_ENABLED=false` in `.env`, then `docker compose up -d`.
      Break-glass sign-ins are recorded in the Audit Log (`breakglass_login`).

### Too many wrong passwords

Five wrong attempts within 15 minutes lock that sign-in for the rest of the 15 minutes — the right
password is refused too, and the sign-in page says how many minutes are left. Counted separately:

- **Local sign-in**, per email address. Addresses without an account lock the same way, so the lock
  gives away nothing about which accounts exist.
- **Break-glass sign-in** (one account).
- **Authenticator and recovery codes** at the second sign-in step, per account (both together).
- **With a session open**, per account: the current password when changing it, and the password or
  code that confirms a key download, key sheet or key export (both together).

A successful sign-in starts the count again. Every wrong attempt is in the Audit Log
(`local_login_failed`, `breakglass_login_failed`, `mfa_totp_failed`, `mfa_recovery_code_failed`,
`password_change_failed`, `step_up_confirmation_failed`). There is no early unlock: the count comes from
the audit log, which cannot be changed, so a locked sign-in simply waits out the 15 minutes. Anyone
can lock an address this way by guessing wrong on purpose; other accounts and SSO sign-in keep working
meanwhile.

### API tokens

For scripts, CI jobs and monitoring (e.g. Zabbix) every user can create personal API tokens under
Profile → "API tokens" → "Create token" (ADR 0029). A token is sent as
`Authorization: Bearer rsg_…` and works on every API endpoint its permissions allow.

- **Roles:** pick one or more roles, each globally or for one tenant. You can only give a token roles you
  hold yourself (that role globally, or exactly that role for that tenant). A platform admin can give
  any role, globally or for any single tenant. Give a token only what it needs: for example
  "viewer — Customer A" for a customer's reporting script.
- **Read-only** (the default) allows only GET requests, whatever the roles. Choose "Read & write" only
  for automation that has to change something.
- **Allowed client addresses** (optional): IP addresses or CIDR ranges; requests from anywhere else are
  refused.
- **Expiry** is required: 30 days to 548 days (18 months) at most.
- The token is shown **once**. RESURGAM stores only a fingerprint of it, so a lost token can't be shown
  again; revoke it and create a new one.

A token always acts as its user and is re-checked on every request: if the user loses a role, the token
loses it too; if the user is disabled, all their tokens stop working. A token can't manage tokens,
change a password or touch MFA settings; those need a real sign-in. The break-glass account can't
create tokens.

The token list shows when and from which address each token was last used. "Revoke" stops a token at
once. Platform admins see every user's tokens under Administration → Users & Roles → "API tokens of all
users" and can revoke any of them. Creating, revoking and every action taken with a token are in the
audit log; actions show as "<user> via API token <name> (rsg_…)".

**Zabbix:** see [Zabbix monitoring](#zabbix-monitoring) -- the wizard there creates a suitable token.

API tokens are not part of the configuration export: create new ones on the target instance.

### Zabbix monitoring

RESURGAM ships a Zabbix template, **RESURGAM by HTTP** (Zabbix 7.0 or newer). Zabbix polls the
appliance's API over HTTPS; nothing is installed on the appliance. **Administration → Zabbix monitoring**
walks through the setup: it creates the API token, offers the template for download with this
appliance's address already filled in, and lists every value to enter in Zabbix.

**What it monitors**

| What | Item(s) | Trigger(s) |
|---|---|---|
| API | readiness, every minute | "API is not ready" (high), "API unreachable" -- no answer for 10 min (high) |
| Each datastore (discovered) | used %, used, available, total size, time since last measurement | "filling up" > `{$RESURGAM.DATASTORE.PUSED.WARN}` (warning), "almost full" > `{$RESURGAM.DATASTORE.PUSED.CRIT}` (high), "not measured for a while" > `{$RESURGAM.DATA.AGE.MAX}` (average) |
| Each PBS × task type (discovered: backup, verify, prune, garbage_collection, sync) | status of the newest task, time since it started | "Last … task failed" (average), "No … task for too long" > `{$RESURGAM.JOB.AGE.MAX}` (warning) |

Values come every 5 minutes (one request per topic; the per-datastore and per-job items read from it).
Discovery runs hourly; items of datastores and job types RESURGAM no longer knows are disabled at once
and deleted after 7 days (Zabbix's defaults).

**Setup by hand** (what the wizard does):

1. Create an API token (Profile → API tokens): role "platform_admin — all tenants (global)", read-only,
   allowed addresses = the Zabbix server, expiry as long as you like up to 18 months. The Zabbix
   endpoints need platform_admin; read-only keeps the token from changing anything.
2. Download the template: Administration → Zabbix monitoring → step 3, or
   `GET /api/v1/zabbix/template` with the token. The file in the repository is
   [`backend/src/resurgam_api/monitoring/zabbix_template.yaml`](../../backend/src/resurgam_api/monitoring/zabbix_template.yaml)
   (address macro still set to the example).
3. In Zabbix: Data collection → Templates → Import, choose the file, keep the default rules.
4. Data collection → Hosts → Create host (e.g. "RESURGAM", any host group, no interface needed), link
   "RESURGAM by HTTP", and set the host macros:
   - `{$RESURGAM.URL}`: the appliance's address as set in `RESURGAM_DOMAIN`, e.g.
     `https://resurgam.example.com`, without a trailing slash. **Any other name or IP gets HTTP 421
     ("wrong address") and no data.** The downloaded template already holds the right value.
   - `{$RESURGAM.TOKEN}`: the token, type "Secret text".
5. Monitoring → Latest data shows values within a few minutes ("Execute now" on the discovery rules
   starts discovery at once).

**Thresholds** are template macros; override them on the host. With a context they apply to one
datastore or one task type only:

| Macro | Default | Meaning |
|---|---|---|
| `{$RESURGAM.DATASTORE.PUSED.WARN}` | 80 | % used for "filling up"; per datastore e.g. `{$RESURGAM.DATASTORE.PUSED.WARN:"store1"}` |
| `{$RESURGAM.DATASTORE.PUSED.CRIT}` | 90 | % used for "almost full" |
| `{$RESURGAM.JOB.AGE.MAX}` | 93600 (26 h) | seconds since the newest task of a type started; `:"verify"` and `:"garbage_collection"` default to 691200 (8 days) |
| `{$RESURGAM.DATA.AGE.MAX}` | 3600 | seconds without a new datastore measurement |
| `{$RESURGAM.JOB.TYPE.NOT_MATCHES}` | `^unknown$` | task types not to discover (regular expression) |

**TLS:** the template's HTTP items do not verify the appliance's certificate, so the appliance's own
certificate (Caddy's internal CA) works as is. To verify it, add the appliance's CA certificate
(wizard step 4, or `GET /api/v1/zabbix/ca-certificate`) to the Zabbix server's CA store and switch on
"SSL verify peer" and "SSL verify host" on the template's HTTP items and discovery rules.

**Troubleshooting** -- an item's error in Data collection → Hosts → Items tells the cause:

- `401`: token wrong, revoked or expired -- create a new one and replace `{$RESURGAM.TOKEN}`.
- `403`: the token lacks platform_admin for all tenants, or Zabbix calls from an address outside the
  token's allowed addresses.
- `421`: `{$RESURGAM.URL}` is not the appliance's `RESURGAM_DOMAIN`.
- Connection refused / timeout: the Zabbix server cannot reach the appliance on port 443.

**Updating the template:** import the new file the same way with "Update existing" ticked; host macros
stay. The template's revision is shown in the wizard and in Zabbix as the template's vendor version.

### White-label branding

Administration → System Settings → "White-label branding":

1. "App title" (up to 60 characters) replaces "RESURGAM" in the browser tab, the top bar, on the
   sign-in page and in PDF report footers. Leave it empty for the default.
2. "Logo": PNG, JPEG or WebP (SVG is refused). It shows on the sign-in page, the sidebar and PDF
   reports. "Remove the custom logo" goes back to the default logo.
3. "Primary color" and "Accent color"; the other shades are derived from them.
4. "Restore defaults" removes the custom logo, both colours and the app title at once.

The app title and colours are part of the configuration export; the logo too when it is at most
512 KiB.

### Updates and the update source

The appliance updates with signed release bundles: Administration → System Settings → "Updates" tab, or
`sudo resurgam-update` on the console, with a database backup first and automatic rollback. Under
"Update source" you can point it at the GitHub (default) or GitLab repository the releases are
published to, so it finds and downloads new bundles itself; nothing is installed without your
confirmation. Details, including air-gapped use: [`appliance/UPDATE.md`](../../appliance/UPDATE.md).

### Configuration export: what is (not) in it

Administration → System Settings → "Backup" tab → "Export configuration" downloads one JSON file;
"Import configuration" on another instance first runs a dry run and then applies it (matched by natural
keys, nothing is deleted; ADR 0024). It contains instance settings (including "Require MFA for all
local users"), branding (colours, app title, a logo up to 512 KiB), the update source (repository URL,
provider and check options), mail and OIDC settings, tenants, PBS/PVE connections, datastore
assignments, policies, pricing plans, users with their roles and per-user MFA requirement, backup group
settings and Recovery Lab application groups. Files exported by older versions stay importable: a
setting missing from the file is left unchanged.

**Never exported:** PBS/PVE token secrets, passwords and password hashes, MFA factors (authenticator
apps, security keys, recovery codes), the OIDC client secret, the SMTP password, backup encryption
keys (use *Administration → Encryption keys → Export all* for those), the lab agent token, the update source's access token and personal API tokens. After an import the result lists each
one to enter again, with a link to the page.

### Overdue backups on the Dashboard

The "Overdue Backups" panel lists the first five workloads without a recent backup. "Show all (N)"
in the panel header (or the "+N more" line) opens a window with every one of them: tenant, datastore
and namespace, last backup and age, sortable by every column, with a search field and a filter for
"Overdue" / "Due soon". "Open list" jumps to the matching Backups page (VMs, Containers, Hosts).
The rule is fixed and the same as on the Backups pages: "Due soon" after 48 hours without a backup,
"Overdue" after 30 days or when a workload was never backed up.

### Capacity forecast: when is a datastore full?

*Reporting → Capacity & Trends* shows two columns per datastore:

- **Net growth / day**: how much used space grows per day, measured over the last 30 days. Each
  day's highest value counts, and the figure is *net*: what prune and garbage collection freed in
  those 30 days is already subtracted. A datastore whose backups grow but whose retention frees the
  same amount shows about zero.
- **Full in**: the available space of the latest measurement divided by the net growth. Hover it for
  the date. Red at 14 days or less, amber at 30 days or less. "Not growing" when the datastore stays
  the same or shrinks; "Not enough history" during the first 7 days after a datastore was added.

The forecast assumes the datastore keeps growing as in the last 30 days. A new VM, a longer
retention or a removed customer changes it; it settles again within a few days.

Alarms: *datastore full soon* (warning at 30 days, critical at 14) shows on the Dashboard and goes
out through the alert mailer like every other alarm. It is not raised when the usage alarm
(80 % / 95 % used) already has the same or a higher severity, so one datastore does not report the
same problem twice.

### Alert mailer (per tenant)

Mails a tenant's alarms to the people responsible for them. Open the tenant (Administration →
Tenants → a tenant, or the tenant's own Customer Panel) → "Alert mailer" → "Edit":

- **Send alarm mails** on, and one or more **Recipients** (one per line or comma-separated, up to 10).
- **Alarms to mail**: "Warnings and critical" or "Critical only".
- **Mail when an alarm is resolved** (on by default).
- **Reminder for open alarms**: re-send still-open alarms every N hours; empty = no reminders.
- **Send test mail** sends a real mail, marked as a test, listing the tenant's open alarms -- in edit
  mode to the addresses typed in, before you save them.

What gets mailed are the tenant's Dashboard alarms: a backup group whose **protection policy** reports
warning or critical (no policy assigned, no alarm -- unlike the Overdue Backups panel's fixed 48h/30d
age rule), and a datastore at 80% (warning) or 95% (critical) used. Alarms on datastores or groups
that belong to no tenant are not mailed. The check runs every 5 minutes, right after the collector's
sync, and sends **one summary mail per tenant per change** -- new alarms, alarms escalated to critical,
resolved alarms and due reminders, with the still-open ones listed below -- never one mail per alarm or
per check. Each alarm is mailed once; it comes again only when it escalates, a reminder is due, or it
clears and comes back.

Mail delivery itself is set up once under System Settings → Mail (and tested there). Without it, or
when the SMTP server refuses, nothing is marked as sent: the panel shows the error, the mailer retries
every 15 minutes, and the alarms go out as soon as delivery works. Switching the mailer off forgets
what was sent, so switching it back on reports every alarm that is open at that moment.

Who can change it: a platform admin, or the tenant's own customer admin. API:
`GET`/`PUT /api/v1/customers/{id}/alert-mailer`, `POST /api/v1/customers/{id}/alert-mailer/test`.
The configuration export includes these settings.

### API documentation and the OpenAPI schema

The account menu (top right) has "API documentation", which opens the interactive API docs
(Swagger UI) in a new tab, and "Download OpenAPI (JSON)", which saves the schema of the running
version as `resurgam-openapi-<version>.json` (e.g. for client generators or Postman). Directly:
`https://<appliance>/api/docs`, `https://<appliance>/api/redoc` and
`https://<appliance>/api/openapi.json`. These pages are readable without a login, as FastAPI serves
them by default; they describe the API but expose no data. To try calls in Swagger UI, sign in to the API
(e.g. `POST /api/v1/auth/local-login`) and paste the `access_token` into "Authorize"; it is kept
for that browser tab. Swagger UI loads its scripts from cdn.jsdelivr.net, so the docs page needs
internet access from the browser; the JSON download does not.

## Running the restore-worker tests

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

```bash
cd restore-worker
uv sync
uv run pytest -q
```

## Running a capability check manually

The capability check reports whether this host can support VM file-restore (requires `/dev/kvm` and, for some operations, `/dev/fuse`) without raising an exception when capabilities are missing — it always returns a structured report:

```bash
cd restore-worker
uv run python -c "
from resurgam_worker.capability.checks import run_capability_check
report = run_capability_check()
for check in report.checks:
    print(f'{check.name}: {check.status} — {check.message}')
"
```

## Running the backend Control Plane

Requires Docker (for a local PostgreSQL) and `uv`.

```bash
cd backend
docker compose --env-file /dev/null -f docker-compose.dev.yml up -d
cp .env.example .env   # fill in SESSION_SECRET, SECRETS_LOCAL_ENCRYPTION_KEY; BREAKGLASS_* and/or OIDC_* to log in
uv sync
uv run alembic upgrade head
uv run uvicorn resurgam_api.main:app --reload
```

Interactive API docs: `http://localhost:8000/api/docs` (schema: `/api/openapi.json`).

Then, with `BREAKGLASS_ENABLED=true` and matching credentials in `.env`:

```bash
curl localhost:8000/api/v1/health
curl localhost:8000/api/v1/readiness   # reports {"status":"degraded",...} if Postgres is unreachable

curl -X POST localhost:8000/api/v1/auth/breakglass-login \
  -H 'content-type: application/json' \
  -d '{"username":"<BREAKGLASS_USERNAME>","password":"<your password>"}'
# -> {"access_token": "...", "token_type": "bearer", "expires_in": 900}

curl localhost:8000/api/v1/auth/whoami -H "Authorization: Bearer <access_token>"
```

### Logging in via OIDC (authentik)

Requires an authentik OAuth2/OIDC Provider + Application already set up with redirect URI `http://localhost:8000/api/v1/auth/oidc/callback` and `OIDC_ISSUER`/`OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET` set in `.env` (see `.env.example` for the exact fields). This is a browser flow, not a plain `curl`:

1. Open `http://localhost:8000/api/v1/auth/oidc/login` in a browser and log in via authentik.
2. The callback redirects (302) to `FRONTEND_SUCCESS_URL` with the session in a URL fragment: `#access_token=...&token_type=bearer&expires_in=900` (never in a query string or JSON body, so it's never sent to any server or logged — ADR 0008). Without a frontend running to catch that redirect, copy the fragment out of the browser's address bar by hand.
3. Use that token with `/auth/whoami` and everything else exactly like a break-glass session — `get_current_user`/`require_role` don't distinguish how the session was obtained (ADR 0004, ADR 0007).

To grant a role automatically at login, put the authentik user in a group named `resurgam-<role-key>` (e.g. `resurgam-platform-admin`, `resurgam-viewer` — hyphens or underscores both work, always granted at global scope). Removing the user from that group and logging in again revokes the role; manually-granted roles (e.g. added directly for testing) are never affected by this reconciliation.

### Local user accounts and MFA (ADR 0010, not in the manifest)

A third way to authenticate, alongside break-glass and OIDC above: a real per-user account with its own password, created by an admin, distinct from the single env-configured break-glass credential.

```bash
curl -X POST localhost:8000/api/v1/users -H "Authorization: Bearer <platform_admin_token>" -H 'content-type: application/json' \
  -d '{"email":"alice@example.com","display_name":"Alice","password":"correct horse battery staple"}'
# A fresh account has zero role assignments -- it can log in but reaches nothing until one is granted:
curl -X POST localhost:8000/api/v1/users/<user-id>/assign-role -H "Authorization: Bearer <platform_admin_token>" -H 'content-type: application/json' \
  -d '{"role":"viewer","scope_type":"global"}'

curl -X POST localhost:8000/api/v1/auth/local-login -H 'content-type: application/json' \
  -d '{"email":"alice@example.com","password":"correct horse battery staple"}'
# -> {"mfa_required": false, "access_token": "...", ...} once no MFA method is confirmed yet -- same shape as break-glass.
```

Once a second factor is enrolled (self-service, requires an existing session from the local account itself — TOTP shown, WebAuthn and recovery codes follow the same `/mfa/*` pattern):

```bash
curl -X POST localhost:8000/api/v1/mfa/totp/enroll -H "Authorization: Bearer <alice_token>"
# -> {"secret": "<base32>", "otpauth_uri": "otpauth://totp/RESURGAM:alice@example.com?..."}
curl -X POST localhost:8000/api/v1/mfa/totp/confirm -H "Authorization: Bearer <alice_token>" -H 'content-type: application/json' \
  -d '{"code":"<6-digit code from an authenticator app or pyotp>"}'
```

`local-login` now hands off to a second step instead of a session:

```bash
curl -X POST localhost:8000/api/v1/auth/local-login -H 'content-type: application/json' \
  -d '{"email":"alice@example.com","password":"correct horse battery staple"}'
# -> {"mfa_required": true, "challenge_token": "...", "available_methods": ["totp"]}

curl -X POST localhost:8000/api/v1/auth/mfa/verify-totp -H 'content-type: application/json' \
  -d '{"challenge_token":"<from above>","code":"<current 6-digit code>"}'
# -> {"access_token": "...", ...} -- a real session, same shape as every other login method.
```

The challenge token above is a short-lived, purpose-scoped JWT — `GET /auth/whoami` (or any other business endpoint) rejects it outright, it's only accepted by `/auth/mfa/verify-totp`, `/auth/mfa/verify-recovery-code`, and `/auth/mfa/webauthn/authenticate-options`/`authenticate`. WebAuthn (security keys/passkeys) needs a real browser (`navigator.credentials`) for both enrollment and login — there's no meaningful `curl` equivalent; use the frontend's `/profile` page and the login page's MFA step. `POST /auth/change-password` (requires a session, current + new password) works for local accounts only — it's explicitly rejected with `no_local_password` for break-glass and OIDC accounts, neither of which has a `password_hash` to change.

### Customer CSV import/export (not from the manifest, requested directly)

```bash
curl "localhost:8000/api/v1/customers/export" -H "Authorization: Bearer $TOKEN" -o customers.csv
# name,slug,debitor_number,match_code,disabled

curl -X POST "localhost:8000/api/v1/customers/import" -H "Authorization: Bearer $TOKEN" -F "file=@customers.csv"
# -> {"created": <int>, "updated": <int>, "errors": [{"row": <int>, "message": "..."}]}
```

Import upserts by `slug` (an existing slug updates `name`/`debitor_number`/`match_code`; a new one creates a customer) using the same validation as `POST /customers`. One bad row — a format error, or a `debitor_number`/`match_code` collision with a *different* customer — is reported in `errors` and skipped, not a whole-file failure. `disabled` round-trips on export but is ignored on import — re-importing an export can't silently re-enable a customer someone disabled on purpose; use `/customers/{id}/disable`/`/enable` for that. Both endpoints are `platform_admin`-only. The frontend's Customers page has Import/Export CSV buttons next to Create. Its "Status" column and detail-modal "Status" field previously showed the raw action-verb badge (`common.enable`/`common.disable`) with the logic inverted from the actual toggle buttons — an active customer displayed a green "Enable" badge and a disabled one a red "Disable" badge, i.e. status read backwards as an instruction rather than a state, unlike the (correct) equivalent on PBS instances/PVE clusters/Users. Fixed with dedicated status-adjective labels ("Active"/"Disabled"); the toggle buttons themselves were already correct and untouched.

### Managing PBS instances and PVE clusters

All of these require a `platform_admin` role assignment on the caller (the break-glass user gets none by default — assign one directly in the database for local testing) and the bearer token from the login above. `/pve-clusters` mirrors the same shape.

```bash
TOKEN="<access_token from login>"

# CA-trusted PBS instance
curl -X POST localhost:8000/api/v1/pbs-instances \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"display_name":"Primary PBS","host":"pbs1.internal.example","port":8007,"tls_trust_mode":"ca","api_token":"user@pbs!token:secret"}'

# Self-signed PBS instance: probe first, then confirm the exact fingerprint back
curl -X POST localhost:8000/api/v1/pbs-instances/probe-tls \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"host":"pbs2.internal.example","port":8007}'
# -> {"fingerprint_sha256": "..."}  — review this before trusting it

curl -X POST localhost:8000/api/v1/pbs-instances \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"display_name":"Secondary PBS","host":"pbs2.internal.example","port":8007,"tls_trust_mode":"fingerprint","tls_fingerprint_sha256":"<the fingerprint just returned>","api_token":"user@pbs!token:secret"}'

curl -X POST "localhost:8000/api/v1/pbs-instances/<id>/test-connection" -H "Authorization: Bearer $TOKEN"
curl -X POST "localhost:8000/api/v1/pbs-instances/<id>/disable" -H "Authorization: Bearer $TOKEN"
curl -X DELETE "localhost:8000/api/v1/pbs-instances/<id>" -H "Authorization: Bearer $TOKEN"
```

A creation request with a `tls_trust_mode: "fingerprint"` fingerprint that no longer matches the host's actual certificate (confirmed a stale value, or a MITM in between) is rejected with `409 tls_fingerprint_mismatch` and nothing is persisted.

### Importing datastores and assigning customers

Discovering and importing datastores requires `platform_admin`, same as PBS instance management above. Reading datastores (`GET /datastores`, `GET /datastores/{id}`) is open to any authenticated user, but server-side filtered to only the customers they hold a role at — a caller without `platform_admin` and without a role at the assigned customer gets a `404` (not `403`) for a datastore outside their scope.

```bash
# see what's on the PBS instance but not yet imported
curl "localhost:8000/api/v1/pbs-instances/<pbs-instance-id>/available-datastores" -H "Authorization: Bearer $TOKEN"

curl -X POST "localhost:8000/api/v1/pbs-instances/<pbs-instance-id>/datastores" \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"customer-a-store"}'

curl -X POST "localhost:8000/api/v1/datastores/<datastore-id>/assign-customer" \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"customer_id":"<customer-uuid>"}'

curl "localhost:8000/api/v1/datastores" -H "Authorization: Bearer $TOKEN"
```

Import re-fetches the datastore's details live from PBS rather than trusting anything beyond the `name` in the request — an import for a name no longer present on the PBS instance is rejected with `404 datastore_not_found_on_pbs`, and importing the same name twice is rejected with `409 datastore_already_imported`.

### Running the Collector (backup inventory + task history)

Inventory sync runs automatically: `resurgam-collector-scheduler` re-syncs every PBS instance on an interval (default 15 minutes, editable under System Settings → Report Retention) and on demand. One `platform_admin` call syncs task history, PBS-native ACL grants, and configured sync jobs for the whole PBS instance, then backup groups/snapshots for every datastore imported under it:

```bash
curl -X POST "localhost:8000/api/v1/pbs-instances/<pbs-instance-id>/sync" -H "Authorization: Bearer $TOKEN"
# -> {"tasks": {"reachable": true, "tasks_created": N, "tasks_updated": N, "error": null},
#     "sync_jobs": {"reachable": true, "sync_jobs_created": N, "sync_jobs_updated": N, "error": null},
#     "acl": {"reachable": true, "entries_synced": N, "error": null},
#     "datastores": [{"datastore_id": "...", "datastore_name": "...", "reachable": true,
#                      "groups_created": N, "groups_updated": N, "snapshots_created": N, "snapshots_updated": N, "error": null}, ...]}

curl "localhost:8000/api/v1/pbs-instances/<pbs-instance-id>/tasks" -H "Authorization: Bearer $TOKEN"
curl "localhost:8000/api/v1/datastores/<datastore-id>/backup-groups" -H "Authorization: Bearer $TOKEN"
curl "localhost:8000/api/v1/backup-groups/<group-id>/snapshots" -H "Authorization: Bearer $TOKEN"
```

`backup-groups`/`snapshots` reads respect the same customer-scope filtering as `/datastores` above; `/tasks` stays `platform_admin`-only since a task isn't tied to a single customer's datastore. Re-running sync is safe — it upserts by PBS's own identifiers (backup-type/id/namespace, snapshot time, task UPID) rather than creating duplicates, but it does not detect or remove snapshots that were pruned from PBS since the last sync. ACL grants are the one exception to "upsert": every sync fully replaces this instance's stored ACL entries (a revoked grant must actually disappear, not linger), not diffed field-by-field.

The synced ACL grants feed the Recovery Evidence report's "PBS-Native Datastore Access" section — raised directly by the user after noticing the report's "Access Control" section only ever listed RESURGAM's own role assignments, missing real access granted natively in PBS (e.g. a PBS user with direct `Admin` on a customer's whole datastore). Without a sync, that section is simply empty for any instance never synced — no live PBS call happens at report-generation time.

#### Getting real workload names instead of bare VMIDs

A backup group's `resolved_name` (shown everywhere a workload label appears — Datastores, Report Center, PDFs) is resolved from three sources, in priority order:

1. **The latest snapshot's own comment** (PBS's per-*snapshot* "Comment" field — the one shown in the PBS web UI's datastore browser). Modern PVE (7.1+) already defaults a vzdump job's `notes-template` to `{{guestname}}`, and **this is the field that value actually lands in** — confirmed live against real customer data: essentially every snapshot already carries it, nothing to configure. (An earlier version of this doc claimed the guest name comes from PBS's per-*group* "notes" field instead — that field is real but distinct, is almost never actually populated, and was the wrong one to document as primary.)
2. **The group's own "notes" field** (`/admin/datastore/{store}/group-notes`), used only when the latest snapshot has no comment — a separate, rarely-used field an operator can set manually per group, independent of any PVE notes-template.
3. **A linked PVE cluster's live guest list**, used only when neither of the above resolves anything — for backups RESURGAM otherwise has no way to name. Link the datastore to the PVE cluster its backups actually come from (PVE Targets → cluster → Edit → "Linked datastores"), or via API:

```bash
curl -X PATCH "localhost:8000/api/v1/pve-clusters/<cluster-id>" \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"linked_datastore_ids":["<datastore-id>"]}'
```

The next sync then falls back to that cluster's live guest list (`GET /cluster/resources?type=vm`) for any group neither source above named. This is a **separate** link from a cluster's "linked PBS instances" (restore capability) — deliberately so: a VMID is only unique within one PVE cluster, and one PBS instance can host datastores for many different tenants/clusters, so name resolution must be scoped to the specific datastore, not "any cluster this PBS instance happens to serve restores from."

If a workload still shows only its bare VMID/CTID after all three, its backup job's notes-template was likely customized or cleared — check/restore it under Datacenter → Backup → edit job → Notes Template (or `notes-template` via the PVE API) so it includes `{{guestname}}`.

#### VMID reuse (a deleted VM's slot reassigned to a different guest)

A VMID can be deleted and later reused for a completely different guest. When a newly synced snapshot's own comment differs from a backup group's already-established name, the Collector treats this as a real identity change, not a rename of the same machine: the current generation is closed out (frozen, its own history untouched) and a fresh generation opens for the same VMID slot, starting with no protection policy, no customer override, and file indexing off — a genuinely new workload needing its own explicit setup, never inheriting the old one's settings. A closed generation's history stays visible in reports covering the period it was active; it's excluded from anything about *current* state (protection status, alarms, billing) once superseded. `GET /datastores/{id}/backup-groups?include_superseded=true` lists every generation, including closed ones; the default listing (and the app's own UI) only ever shows the current one. An `ApplicationGroup` member pointing at a group that gets superseded is never silently re-pointed — it keeps testing the frozen prior workload and the API/UI flag it (`ApplicationGroupMember.backup_group_superseded`) for manual review.

**Which snapshot a scheduled File Indexing run indexes (0.2.12).** One snapshot per group and run, never the whole history (older snapshots can be indexed by hand). A group backed up once a day gets its newest snapshot. A group backed up several times a day (on at least 2 of the last 7 days) gets the last snapshot of yesterday instead — as the PBS server counts days — because that is the one PBS `keep-daily` keeps; the newest intra-day snapshot is often pruned within hours, and its index with it. The index of such a group is therefore up to one day behind its newest backup. The PBS server's timezone is read by every Collector sync and shown in the PBS connection's details ("Server timezone"); until the first sync, days are counted in UTC. If the prune policy has no `keep-daily` (only `keep-last`), no snapshot lives long and the index will keep disappearing — add `keep-daily` to the PBS prune job or the PVE backup job's retention.

### Protection policies (manifest §9.5)

A backup group's protection status (`ok`/`warning`/`critical`/`unknown`, manifest §9.4) is computed on every read, not stored — it needs a policy assigned first, or it stays `unknown`:

```bash
curl -X POST localhost:8000/api/v1/protection-policies \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"customer_id":"<customer-uuid>","name":"Standard","expected_interval_hours":24,"max_time_since_last_backup_hours":48,"min_snapshot_count":1}'

curl -X POST "localhost:8000/api/v1/backup-groups/<group-id>/assign-policy" \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"protection_policy_id":"<policy-uuid>"}'
```

A policy can only be assigned to a group whose datastore is assigned to the *same* customer the policy belongs to — a cross-customer assignment is rejected with `409 protection_policy_customer_mismatch`. Deleting a policy (`DELETE /protection-policies/<id>`) unassigns it from every group using it (`ON DELETE SET NULL`) rather than failing or cascading. `sync_dr_required` is stored on a policy but not yet evaluated against a specific sync job — `sync_jobs` (below) exists now but isn't cross-referenced back to a policy yet.

### Usage and capacity (manifest §9.3)

Every `/pbs-instances/{id}/sync` call also captures one usage measurement per datastore — no separate trigger needed:

```bash
curl "localhost:8000/api/v1/datastores/<datastore-id>/usage" -H "Authorization: Bearer $TOKEN"
# -> {"latest": {...} | null, "history": [...], "daily_change_bytes": <int>|null, "monthly_change_bytes": <int>|null,
#     "forecast": {"status": "growing"|"not_growing"|"insufficient_history", "basis_days": <int>,
#                  "growth_bytes_per_day": <int>|null, "days_until_full": <float>|null, "full_at": <datetime>|null}}
```

`used_bytes`/`available_bytes`/`total_bytes` come from a real PBS call and reflect physical, deduplicated-and-compressed disk usage (manifest §9.3's "PBS-Datastore-Belegung"); `logical_bytes` is the sum of current snapshot sizes with no dedup applied ("logisch geschützte Datenmenge") — the gap between the two is roughly the dedup/compression ratio PBS is providing. Group/snapshot counts and `last_successful_gc_at` are computed from RESURGAM's own already-synced tables. `daily_change_bytes`/`monthly_change_bytes` are `null`, not `0`, until enough sync history has accumulated; `history` holds the newest 500 measurements, the 24 h / 30 day baselines are looked up separately. `forecast` is described in *Capacity forecast* above — this is computed from RESURGAM's own stored history, a deliberately simpler source than PBS's own richer built-in usage history, which is real but unused here (see `docs/milestone-1-pbs-pve-api-assumptions.md`).

### Billing foundations (ADR 0009, not from the manifest)

Not a billing computation yet — just the data model + CRUD for a later slice:

```bash
# A datastore assignment can carry a role — offsite copies still count toward
# per-GB billing but not per-VM (avoids double-counting a DR-replicated VM
# without needing cross-PBS-instance VM identity matching):
curl -X POST "localhost:8000/api/v1/datastores/<datastore-id>/assign-customer" \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"customer_id":"<customer-uuid>","role":"offsite"}'

# A shared datastore can expose individual backup groups to a different
# customer than the datastore's own assignment:
curl -X POST "localhost:8000/api/v1/backup-groups/<group-id>/assign-customer" \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"customer_id":"<other-customer-uuid>"}'

# A global default pricing plan, and a per-customer override. The per-unit
# rate is split three ways -- VM/LXC/Host are typically priced differently
# (2026-09-14) -- price_per_ct_month/price_per_host_month default to 0 if
# omitted:
curl -X POST localhost:8000/api/v1/pricing-plans -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"customer_id":null,"name":"Standard","price_per_vm_month":"10.00","price_per_ct_month":"4.00","price_per_host_month":"2.50","price_per_gb_month":"0.05","currency":"CHF"}'
curl -X POST localhost:8000/api/v1/pricing-plans -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"customer_id":"<customer-uuid>","name":"Enterprise","price_per_vm_month":"8.00","price_per_gb_month":"0.04","currency":"CHF"}'
```

At most one global plan and one plan per customer are enforced at the database level. `GET /datastores/{id}/backup-groups` now filters per group by its *effective* customer (its own override, or the datastore's) rather than requiring the whole datastore be in scope — a shared datastore can show a customer-scoped viewer only their own groups.

### Sync-/DR-Status (manifest §17/§8's `sync_jobs`)

Refreshed by the same `/sync` call as tasks and inventory:

```bash
curl "localhost:8000/api/v1/pbs-instances/<pbs-instance-id>/sync-jobs" -H "Authorization: Bearer $TOKEN"
```

Unlike everything else added this milestone, PBS's `config/remote`/`config/sync` endpoints could not be verified against a real instance (both reachable but empty on the only one available) — the parsed field shapes are best-effort from PBS documentation only, flagged explicitly in `docs/milestone-1-pbs-pve-api-assumptions.md` and in `connectivity/pbs_api.py`'s docstrings. A sync job's "last run" status also isn't correlated from task history — no real sync/syncjob task was ever observed to confirm the `worker_id` format needed for that correlation, so the frontend shows configured jobs and task history side by side rather than merged.

### Alarms (manifest §9.2's Dashboard alarm states)

Computed fresh on every request, nothing stored:

```bash
curl "localhost:8000/api/v1/alarms" -H "Authorization: Bearer $TOKEN"
# -> [{"type": "protection_critical", "severity": "critical", "customer_id": "...", "message": "...", "entity_type": "backup_group", "entity_id": "..."}, ...]
```

Two signals: a backup group's protection status being critical/warning (nothing if no policy is assigned — same "no policy, no verdict" rule as `/backup-groups`), and a datastore's latest usage measurement crossing 80%/95% used. Instance-unreachable and task-failure signals aren't included — both already have their own visible status elsewhere (the PBS Instances page's health badge; task history's status column). Customer-scope filtered the same way as everything else — a `platform_admin` sees every alarm, anyone else only ones whose effective customer they hold a role at. The Overview page (`/`) shows this as a panel, sorted critical-first, with an explicit "no active alarms" badge when clear rather than the panel disappearing.

### Billing (not in the manifest — ADR 0009, the `PricingPlan` computation)

Computed fresh on every request, nothing stored:

```bash
curl "localhost:8000/api/v1/billing/customers" -H "Authorization: Bearer $TOKEN"
curl "localhost:8000/api/v1/billing/customers/<customer-id>" -H "Authorization: Bearer $TOKEN"
```

Per customer: the per-unit fee counts backup groups whose *effective* customer (own override, else the datastore's assignment) is this customer, restricted to `primary`-role datastores (an `offsite`/DR copy doesn't double-count a unit) — split by `BackupType` into `vm_count`/`ct_count`/`host_count` (2026-09-14, each priced separately on the plan). The per-GB fee counts every matching group regardless of role — it's real disk consumed either way — estimating each group's physical footprint by scaling its own logical bytes (its retained snapshots' summed size) by *its own datastore's* dedup ratio (`used_bytes / logical_bytes` from the latest usage measurement), since a customer's VMs can span datastores with different dedup characteristics. A datastore with no usage measurement yet contributes 0 bytes for its groups and sets `physical_bytes_estimate_complete: false` rather than blocking the whole response. The resolved pricing plan (customer override, else the global default, else none) is applied to produce `estimated_monthly_cost`, rounded to 2 decimal places — it's an estimate, not an invoice line. Verified against real data on the dev PBS: 135 backup groups (91 VM/43 CT/1 Host), ~2.16TB estimated physical bytes. Customer-scope filtered the same way as `/alarms`. The Reporting → Tenant Usage page shows this per customer, with a badge when the byte estimate is incomplete; Administration → Customers' own detail view (2026-09-14, was list+create only) shows the same summary for one customer alongside its assigned datastores.

### File-level restore (manifest §13, ADR 0011)

Check whether this host can do file-restore work at all (never fails — always a structured per-check report):

```bash
curl "localhost:8000/api/v1/file-restore/capability" -H "Authorization: Bearer $TOKEN"
# -> {"checks": [{"name": "proxmox-backup-client", "status": "ok", "message": "client version: 3.4.7"},
#                {"name": "proxmox-file-restore", "status": "missing", "message": "proxmox-file-restore not found on PATH"},
#                {"name": "/dev/kvm", "status": "ok", ...}, {"name": "/dev/fuse", "status": "ok", ...}]}
```

Browse and download a file inside a snapshot (requires `restore_operator` or `platform_admin`, scoped to the backup group's effective customer):

```bash
curl "localhost:8000/api/v1/backup-groups/<group-id>/snapshots/<snapshot-id>/files?path=/etc" -H "Authorization: Bearer $TOKEN"
curl "localhost:8000/api/v1/backup-groups/<group-id>/snapshots/<snapshot-id>/files/download?path=/etc/hostname" -H "Authorization: Bearer $TOKEN" -o hostname
```

Dispatches on the backup group's type. CT/host groups (`catalog.py`, wrapping `proxmox-backup-client catalog dump`/`restore`) are live-verified against a real PBS 4.2 instance — browsing and single-file download both confirmed against a real container snapshot with ~63,390 catalog entries. A single-file download can be slow on a large catalog (confirmed: over two minutes for one small file near the end of a ~63k-entry tree, since `restore` walks the archive's chunk stream sequentially rather than jumping to the match) — the backend allows up to 300s for it. VM groups (`file_restore.py`, wrapping `proxmox-file-restore`) are also live-verified, against a real PVE 9.2 node (2026-09-15) — a real Windows Server VM backup's disk archive, partitions, and NTFS directory tree all listed and downloaded correctly, standalone (no PVE API needed). This binary isn't available in *this development sandbox* specifically (calling it here returns `503 file_restore_unavailable`) — real usage needs `proxmox-backup-file-restore`, `proxmox-backup-restore-image`, and `pve-qemu-kvm` all installed from the same PVE apt repo release, on a matching Debian release; mixing repos or Debian versions reproducibly breaks the host↔guest handshake (see ADR 0011 for the full debugging story). See ADR 0011 for the real `proxmox-backup-client` CLI behaviors (stderr vs stdout, `--pattern`, `--ignore-ownership`) live testing caught while building the CT/host path. No frontend UI for this yet — API only.

**PBS client-side backup encryption (ADR 0031; replaces the per-datastore key of ADR 0022).** A backup encrypted with PBS's own `--crypt-mode encrypt` can only be read with the exact key it was made with. Every backup shows whether it is encrypted and with which key fingerprint (backup lists, workload lists, datastore row). Keys live in one store, by fingerprint: *Administration → Encryption keys* (`platform_admin`). **Upload key**: the keyfile in PBS JSON format; for PVE backups that is `/etc/pve/priv/storage/<STORAGE-ID>.enc` on a PVE node. Keys with a passphrase are rejected for now (PVE never makes them). Optionally assign a key to a tenant. From then on every RESURGAM read of a backup (file browse and download, indexing, Disk Mounts, Instant Recovery, Recovery Lab, guided restores) passes exactly the key that backup carries, and no key for an unencrypted one, so one datastore can hold unencrypted backups and backups with several keys. A backup whose key is missing fails with `encryption_key_missing` (409) naming the fingerprint, and the alarm *encryption key missing* lists it per datastore. **Keep a copy outside RESURGAM**: per key, **Key sheet** (a printable A4 PDF with the fingerprint, the keyfile as text and as a QR code, and restore steps) or **Keyfile** (the original file). Then click *Copy is stored safely*; until then the alarm *key copy not confirmed* reminds you. **Export all** writes every key into one file encrypted with a passphrase of at least 12 characters (scrypt + AES-256-GCM); **Import** reads it into another RESURGAM (keys already there are skipped, tenant assignments are not part of the file). Every download, key sheet and export asks for your password or an authenticator code (SSO-only accounts: a sign-in in the last 10 minutes), works only in a signed-in session and never with an API token, is written to the audit log and announced to all platform admins by mail. Tenant admins see their tenant's keys on the tenant panel; a platform admin can allow them there to download their own keys (*This tenant's admins may download their own keys*, off by default, audited). Keys uploaded per datastore under ADR 0022 are moved into the store automatically by the collector. API: `/encryption-keys` (list, upload, `PATCH`, `DELETE` with `force=true` for a key still in use, `/{id}/download`, `/{id}/key-sheet`, `/{id}/confirm-copy`, `/export`, `/import`, `/missing`, `/tenants/{customer_id}/download-permission`); `POST/DELETE /datastores/{id}/encryption-key` remain as deprecated aliases. Not yet: setting up encryption on a PVE storage from RESURGAM (ADR 0031 stage 3).

Running the backend tests (same Postgres container, not started automatically by the test suite):

```bash
cd backend
docker compose --env-file /dev/null -f docker-compose.dev.yml up -d
uv sync
uv run pytest -q
```

## Running the web UI

Requires the backend running (above) with `FRONTEND_ORIGIN`/`FRONTEND_SUCCESS_URL` in its `.env` matching the frontend's dev origin (defaults on both sides already agree: `http://localhost:5173`).

```bash
cd frontend
npm install
npm run dev   # http://localhost:5173
```

Opening `http://localhost:5173` redirects to `/login` when there's no session. The primary form is local-account sign-in (email + password, `POST /auth/local-login`); a login that returns `mfa_required` renders an inline second step in place of the form (method picker when more than one is enrolled, a plain code field for TOTP/recovery codes, or a "use security key" button driving the real browser WebAuthn API). Break-glass sign-in sits behind an "Emergency access" toggle rather than being a separate prominent form — same request/response shape as always, just visually de-emphasized now that local accounts are the expected path. "Sign in via SSO" navigates the whole page to the backend's `/auth/oidc/login`, and authentik redirects back to `/auth/callback`, which reads the session out of the URL fragment and stores it. The avatar menu (top of the left navigation is the brand mark; account controls are top-right) switches language (EN/DE, persisted) and theme (light/dark/system, persisted) without a reload, links to Profile, and signs out.

`/profile` (avatar menu → Profile, not in the manifest's nav tree at all) is where a local account changes its password and manages MFA: TOTP enrollment shows a real, client-rendered QR code (the `qrcode` package, from the server's `otpauth://` URI) plus the raw secret for manual entry; WebAuthn security keys/passkeys register and remove through the same page; recovery codes generate as a batch of 10, shown once with a save-it-now warning, and require an existing TOTP or WebAuthn method to already exist (they back up a second factor, they aren't one alone). Break-glass and SSO accounts see one explanatory message here instead — neither has a local password to manage.

**Mandatory MFA (2026-10-11).** Administration → Users & Roles shows an MFA column (Off / Active / Required · active / Required · setup at next login) for local accounts. In the user dialog, "Require MFA at next login" (`POST /users/{id}/mfa-requirement` `{"required": true}`, also settable on creation with `mfa_required`) makes a second factor mandatory: at the next sign-in a user without one gets no session, only `mfa_enrollment_required` plus a 15-minute `enrollment_token` (`POST /auth/local-login`). The login page then walks them through an authenticator app (QR code) or a security key, shows recovery codes once, and exchanges the token for a session (`POST /auth/mfa/enrollment/complete`). The enrollment token reaches only the factor-adding `/mfa/*` endpoints -- not `whoami`, not any business endpoint, not factor removal. While MFA is required, the last authenticator app or security key can't be removed (`mfa_required_last_factor`, the profile page says why). "Reset MFA" (`POST /users/{id}/reset-mfa`) removes a user's factors and recovery codes when they lost their phone or key; with MFA required they set up a new one at the next sign-in. Neither applies to the break-glass user or SSO users (`mfa_not_applicable`), and an admin can't reset their own MFA here. The flag travels in the configuration export; factors never do.

Administration → Users & Roles (`/administration/users-roles`) is the admin side: create a local account with an initial password, disable/enable it, and manage its role assignments (global or per-customer scope) in a click-to-open modal — the same `Modal` component used for PBS/PVE config below.

Infrastructure → PBS Instances and PVE Targets both gained the same click-to-open `Modal` "window" per row (view config read-only, Edit switches the modal into a form) backed by new `PATCH /pbs-instances/{id}`/`PATCH /pve-clusters/{id}` endpoints — every field optional (a real partial update), `api_token` always starts blank with a "leave blank to keep the current value" hint since no endpoint ever returns a stored token, and TLS-fingerprint mode re-verifies against the live host whenever a connection-relevant field actually changes.

Infrastructure → Datastores is one expand/collapse directory tree — PBS Instance → Datastore → Namespace → Backup Group (VM/CT/Host) → Snapshot — rebuilt from three separate table pages after first review (2026-09-14) asked for a real "directory tree" feel with namespaces visible as their own grouping level, not just a column. Each level fetches lazily on expand rather than loading everything up front (real data is 135 groups/1321 snapshots on one datastore). The customer/role pickers (datastore level) and customer-override/protection-policy pickers (group level) that used to live in separate pages are now inline on each row, unchanged in behavior. An expanded namespace with many groups (the real `site-b/tenant-b` namespace has 75) is a flat, unpaginated list, so it also has a free-text filter (by backup ID or resolved name) right above the group rows — browser-verified against that real namespace: "116" narrows 75 rows to the one real match (`CT 1116`), and a filter matching nothing shows "No backup groups match your filter." instead of an empty-looking gap.

Each snapshot row also has a "Browse files" button (a small folder icon, manifest §13 — ADR 0011) opening a modal with breadcrumb navigation: click a directory to descend into it, click a file to download it. Dispatches transparently to whichever backend the snapshot needs — CT/host groups return real catalog data (browse-tested against a real container's full `/etc`, matching the raw CLI output byte-for-byte); VM groups on a host without `proxmox-file-restore` correctly show "File browsing isn't available here" instead of a generic error or a stuck loading spinner, driven by the same `file_restore_unavailable` (503) code the API returns. A download that's actually in flight shows a per-row "Downloading…" label (not just a disabled row) and a "larger files can take a few minutes" hint, since a single-file download can genuinely take minutes for a large catalog; two downloads started at once are tracked independently, each row only re-enabling once its own request settles.

Folders and multi-selections download as one ZIP (ADR 0013 addendum 2026-10-08): tick entries (or "Select all") and use "Download (ZIP)", or the "ZIP" action on a folder row. The ZIP is built by the file-restore worker as an `archive` job; the list above the entries shows its progress, then its size and any entries that were skipped (unreadable, special files, not found) or renamed (names Windows can't create). The finished ZIP — and every single-file download — is saved through a short-lived download link, so even a multi-GB archive streams straight to disk. API: `POST .../files/jobs` with `{"job_type": "archive", "paths": [...]}`, poll `GET /file-restore/jobs/{id}`, then `POST /file-restore/jobs/{id}/download-ticket` and open its `download_url` (or `GET .../download` with the `Authorization` header).

Recovery → Datei-Recovery (`/recovery/file-recovery`, was a nav placeholder) is now a real page too: three cascading pickers (customer → backup group → snapshot, the group list aggregated client-side across the customer's datastores since no single "backup groups for this customer" endpoint exists yet) end in the same file browser rendered inline, for reaching a specific snapshot's files directly instead of drilling through the full Datastores tree every time. The modal version (above) and this page share the same underlying `FileBrowserPanel` component — no duplicated browsing logic. The backup group and snapshot pickers are a type-to-filter `Combobox`, not a plain `<select>` — real data surfaced this directly (a customer can have 90+ backup groups; a flat dropdown that long is tedious to scan), so it types like a search box and narrows the list as you go, still a controlled `value`/`onChange` pair underneath like any other picker.

Every nav entry has a real page today except Backups → File Backup, which still renders the shared "not available yet" placeholder (`NotAvailablePage`) rather than being hidden.

Backups → Virtuelle Maschinen/Container/Hosts (manifest §9.4 "Backup-Inventar", `/backups/vms`, `/backups/containers`, `/backups/hosts`) are now real too, sharing one `ProtectedWorkloadsPage` component filtered by `backup_type` — the same "share one component, filter client-side" pattern the four Jobs views already use. Each is a flat table across every datastore (Customer, PBS/Datastore, Namespace, Workload, Last backup, Verify status, Protection status — the exact §9.4 field list, all already on `BackupGroupOut`), with an optional customer filter. Same client-side aggregation `FileRecoveryPage` uses for its group picker (no "groups by type across all datastores" endpoint exists), but unconditional here rather than gated behind a customer pick first — this is meant as a full inventory view, and RBAC scope-filtering on `/datastores`/`/backup-groups` already limits what a non-admin sees. Browser-verified against real data: 91 VM / 43 CT / 1 Host rows, matching the counts from the billing verification earlier this milestone exactly. At that scale a flat table is its own version of the giant-dropdown problem, so it also has a free-text search box (filters across workload/customer/datastore/namespace) and clickable column-header sorting (ascending, then descending) — both browser-verified against the real 91-row VM table.

Jobs → Backup-Jobs/Verifizierung/Prune & Garbage Collection/Replikation & Sync (`TasksPage`, shared and filtered by `task_type`) is the worst case of the same flat-list problem: task history is an unbounded, ever-growing log with no backend pagination, and the real Sample PBS instance already has 1472 tasks. It gained a free-text search (by UPID/type/worker/status) and the same per-column sort `DataTable` now supports everywhere — browser-verified against the real 1472-row table: filtering by a specific VM's worker ID (`vm/1204`) narrows it to that VM's 298 real runs, and sorting by Started correctly reorders by date. The backend endpoint itself (`GET /pbs-instances/{id}/tasks`) followed up right after with real pagination: `task_types` (repeated query param) and `limit` (default 1000, max 5000), filtering by type *before* limiting — otherwise a high-volume type (backup, 1472 real tasks) sharing one global recency window with a low-volume one (verification, 26) would silently push the low-volume type's older-but-real entries out of the response even though `/jobs/verification` is asking specifically for those. TasksPage now requests a bounded window and shows "Load more" (advancing the limit by 1000) only when the server actually returned a full page — confirmed live: `/jobs/backup` loads 1000 of 1472 rows first, "Load more" brings in the rest and the button disappears once the page comes back short of the limit; the other three Jobs views still show their exact real counts (26/460/0), unaffected by backup's much larger volume.

Clicking a workload row (any of the three pages above) opens `RestorePointsModal`: lists that workload's snapshots (restore points), each leading further into the same `FileBrowserPanel` used elsewhere — a third entry point into manifest §13 browsing, reachable directly from a workload's own inventory row rather than the Datastores tree or the File Recovery page. Browser-verified end to end against a real CT snapshot: workload row → restore points list → real root directory listing → back to restore points, zero console errors.

```bash
cd frontend
npm run test    # Vitest + React Testing Library
npm run build   # tsc -b && vite build
```

Recovery → Restore Center (`/recovery/restore-center`, was a nav placeholder) is manifest EPIC 8's "geführter Restore" (§11.1) — a 5-step wizard (Snapshot → Restore-Ziel → Netzwerkisolation → Restore-Optionen → Prüfung und Freigabe, per §26.7's UI spec) that ends in a reviewed, human-runnable `qmrestore`/`pct restore` command, never an executed one — the manifest's own MVP scope for this epic ("Im MVP wird der Restore nicht automatisch gestartet"). This was the real foundation the user originally asked for under "Recovery Lab" (EPIC 10); EPIC 8 was buildable immediately and EPIC 10 wasn't at the time (ADR 0012).

Recovery → Recovery Lab (`/recovery/recovery-lab`) is now real too — EPIC 10's full loop: Application Groups (a customer-scoped, reusable definition of several VM/CT backup groups that make up one application, each member picking a startup order and 3-tier check hierarchy) and Lab Runs (execute against a lab-enabled PVE cluster, `resurgam-lab-agent` zero-copy maps the PBS snapshot directly as a local block device — no `qmrestore`, no copy — reconstructs the guest's actual original hardware config from the backup's own `qemu-server.conf.blob` rather than a synthetic one, and boots it; a second component, `resurgam-lab-prober`, runs as a per-run probe appliance that is itself the isolated Lab VNet's router/DHCP/firewall, since PVE is deliberately kept out of that network's data path). Level 1 (`guest_agent`) resolves backend-side via PVE's own guest-agent REST proxy; Levels 2–3 (`icmp`/`arp`/`tcp_port`/`http`/`https`/`dns`/`tls_handshake`/`ssh_banner`/`nmap_scan`) run from the probe appliance over the isolated segment it actually has reachability to. Live-verified end to end against a real Windows production VM: zero-copy mapped, booted with its correct original hardware profile (not the earlier repair-mode-inducing synthetic config), passed guest-agent and ARP checks, and a real `nmap` scan correctly found its actual open RDP port. See ADR 0019 (Phase A: the zero-copy map/boot mechanism) and ADR 0020 (Phase B: the 3-tier probe hierarchy, the self-contained probe appliance, and every real bug found building it) for the full detail.

**Pairing a lab node (ADR 0027, 2026-10-11).** The Lab Compute wizard's "Lab agent" step (now before "Probe appliance") defaults to *Pair node with a one-time code*: pick the node, click "Create pairing command", open the node's Proxmox web shell with the button next to it and paste the command. It downloads a pairing script (its SHA-256 is pinned in the command), installs the shipped `resurgam-lab-agent`, asks for the node's PBS token on the node (Enter skips; template builds work without it, lab runs, ephemeral file indexing and disk mounts need it), registers with the single-use code (30 minutes, `POST /lab-setup/bootstrap/{code}/pair`) and starts the agent; the wizard shows "Paired" by itself. No SSH login or password is entered in RESURGAM. The script carries the appliance's TLS CA (Caddy's internal root) so the agent verifies the appliance. Afterwards the probe appliance template — and the Disk Mounts share helper template (Disk Mounts → Setup → "Build with lab agent") — are built by the agent on its node (`POST /pve-clusters/{id}/lab-setup/agent-jobs`), with the same step log as the SSH build; the agent only accepts structured build settings, never commands. The guided SSH and manual routes remain as alternatives.

PVE connectivity — previously just a version-check probe — gained real node/storage/guest/VMID-check queries, live-verified against the real `Sample PVE` cluster in this dev environment before being trusted (see ADR 0012 for the two real, non-obvious findings: a PBS-backed PVE storage entry is scoped to exactly one PBS namespace, not a whole datastore, and VMID uniqueness has to be checked cluster-wide, not per-node). The wizard's target-selection step queries this live — PVE cluster → node → storage, with the target VMID checked inline (debounced) against the real cluster as the operator types, and the target-storage list correctly excludes the PBS-type storage itself (that's the *source*, not a valid restore *destination*).

Every plan is a real, persisted, auditable row (not a stateless computed response) recording every check result and the exact generated command — manifest calls this a "nachvollziehbarer Restore-Plan" (traceable plan). Checks cover VMID freeness (matching the operator's stated new-vs-existing intent), target storage capacity, whether the source PBS datastore/namespace is actually visible as a storage on the chosen PVE node, name conflicts against the temporary restore name, and network-risk classification (a new `PveCluster.restore_network_rules` field, defined for the first time — an unconfigured cluster never reads as safe by default). The "Restore options" step deliberately has no TTL/automatic-cleanup controls — those apply once automated orchestration (EPIC 9) exists to enforce them; a static note says so rather than showing a toggle that does nothing.

Browser-verified end to end against real data: `Sample Customer AG`'s real VM 116, a genuinely taken VMID (100, correctly flagged inline) and a genuinely free one (999999, correctly flagged), and a created plan whose checks all passed against live PVE state, producing exactly `qmrestore pbs-main:backup/vm/116/2026-06-28T00:45:09Z 999999 --storage local-lvm --unique 1` followed by `qm set 999999 --delete net0` — copy-to-clipboard included, zero console errors. If the source PBS datastore/namespace isn't actually configured as a storage on the target PVE node, the check fails with a direct, actionable message ("PBS store not configured on this PVE node — please configure it in PVE...") rather than a passive description — RESURGAM doesn't provision that PVE-side storage.cfg entry itself (deliberately deferred, a write to shared PVE infrastructure is a different risk class than the read-only checks this slice otherwise does), so the operator is told plainly what to go fix and where. A follow-up pass live-verified the two paths not yet exercised — a real CT restore into an existing VMID (CT 101, `--force 1` instead of `--unique 1`, "VMID 101 exists and will be overwritten") — and caught a real overflow bug when triggering the PBS-store-not-configured message live: the long check sentence ran off the edge of the panel instead of wrapping (a `StatusBadge` styling gap, since its own class is CSS-Modules-scoped and can't be overridden from outside without an explicit passthrough), now fixed.

Administration → Audit Log (`/administration/audit-log`, was a nav placeholder) is now a real page too — manifest §8's append-only `AuditEvent` trail (immutable both in application code and at the DB level) was already being written to from roughly 50 call sites across the backend before this page existed; only the read side was missing. `platform_admin`-only (the model has no `customer_id` of its own to scope by — an event's target can be almost anything), and, like task history, an unbounded, ever-growing log (229 real rows already in this dev environment after one milestone's testing), so it uses the same bounded-`limit` + "Load more" pattern as `TasksPage`, plus a free-text search and `DataTable` sort. The actor column resolves `actor_user_id` to a real display name/email via the existing Users list (client-side lookup, same pattern used elsewhere) rather than showing a bare UUID, and a `null` actor (a handful of real rows, e.g. `breakglass_login_failed`) shows as "System." Browser-verified against the real 229-event log: searching `restore_plan` correctly narrows to the exact two real `restore_plan_created` events from the Guided Restore testing above, with the real actor ("Break-glass (admin)") resolved correctly, zero console errors.

A top-left Customer Switcher (raised directly by the user, "to prepare a Customer Panel") sits above the navigation tree: picking a customer here is a real navigation, not a silent filter — it jumps to `/customers/{id}/panel`, that customer's own `CustomerPanelPage` (billing/usage summary, File Index settings status, quick links into the existing per-type pages), the same way switching accounts in most multi-tenant admin tools jumps to that account's own dashboard. The selection also persists per-viewer (`CustomerContext`) for future customer-scoped pages to read without their own separate picker; picking "Alle Kunden"/clearing the selection returns to `/`. This is a first page, not the full tenant-focused workspace the name implies yet — each per-type page (Backups, Jobs, etc.) still has its own separate "Nach Mandant filtern" picker rather than inheriting this selection.

The left navigation itself got a tidy-up in the same pass: a section whose backend is unconditionally `platform_admin`-gated (`NavSection.requiresRole`, deliberately conservative — only Infrastruktur and Administration qualify; Backups/Jobs/Recovery/Reporting use per-customer scope checks instead and stay visible) is hidden entirely rather than shown-then-403ing for a user without that role. Every category is individually collapsible, state persisted per-viewer in `localStorage`, and a collapsed section auto-expands again if the current route happens to be inside it, so navigating there directly (a bookmark, a back-button, a deep link) never leaves the active page's own entry hidden inside a collapsed group. Cmd/Ctrl+K opens a global command-palette search (`GlobalSearch`, a native `<dialog>`) over every nav entry the current user can actually reach (respecting the same `requiresRole` filter) plus the live customer list — arrow keys to navigate, Enter or a click to jump straight there, closing the dialog either way.

## What's not here yet

- Customer-scoped role grants via OIDC groups — a group name alone can't carry a customer, so this stays a manual action (ADR 0007).
- A real secrets provider (OpenBao) — PBS/PVE tokens are stored via an interim local encrypted store today, see [`../adr/0005-local-encrypted-secrets-provider.md`](../adr/0005-local-encrypted-secrets-provider.md).
- The rest of the Recovery Lab deploy pipeline — an automated smoke test gating template/package builds, a canary cluster and N-1 template retention (`docs/recovery-lab-deploy-pipeline.md` §3, §4, §6). Packaging, agent install by pairing and agent-built templates are done.
- MFA account lockout after repeated failed attempts, "remember this device," and rate-limiting on `/auth/local-login` — stated simplifications (ADR 0010), not silent gaps, worth revisiting before this leaves a development/internal deployment.
- Guest-exec File Indexing (`guest_exec_live`/`guest_exec_ephemeral`, ADR 0021) — **not recommended as a primary method.** Every stage up to final retrieval is live-verified correct (hookscript trigger, job creation, chunked enumeration), and several real bugs in the retrieval path were found and fixed live (an unreliable blocking assumption in `qm guest exec`, unreliable `exec-status` completion tracking, a stale-manifest race). But across four independent live runs against a real production VM, retrieval still fails deterministically at the same point on a small (<1MB) chunk read, and the leading theory (Windows Defender content-scanning) was tested directly — an exclusion was applied and confirmed active — and did not fix it. The true root cause was never isolated despite substantial live investigation. Recommendation: don't invest further in hardening this specific mechanism; the better path for the common unencrypted-disk case is a persistent "mount helper" worker VM that snapshots hot-attach to (discussed, not yet designed or built), reserving guest-exec for the narrower case that actually needs it — a genuinely encrypted disk. See ADR 0021's live-verification addendum for the full history.
- PBS client-side backup encryption, stage 3 (ADR 0031) — generating a key and setting it on a PVE storage from RESURGAM, and checking which key each PVE storage has configured, is not built yet. Generate keys in PVE (`encryption-key=autogen`) and upload them under *Administration → Encryption keys*. Passphrase-protected keys are not supported yet.

Built since these lists were first written (so no longer gaps): the appliance image and signed-bundle updates, the Collector scheduler (`resurgam-collector-scheduler`, periodic sync), PBS-notes/PVE-based workload name resolution, Recovery Lab cleanup destroying the run's member and probe appliance VMs, and the unprivileged file-restore/file-index workers (live-verified 2026-10-07, `backend/packaging/systemd/README.md`). `resurgam-lab-agent` still runs as full root.

## Verifying against a real PBS client

This repository's PBS CLI wrappers were built against hand-written fixtures, not a live PBS instance. If you have `proxmox-backup-client`/`proxmox-file-restore` available on a real machine, see [`../milestone-0-manual-verification.md`](../milestone-0-manual-verification.md) for a step-by-step checklist to confirm (or correct) the assumptions logged in [`../milestone-0-assumptions.md`](../milestone-0-assumptions.md).
