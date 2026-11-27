# 0032 — 3-2-1 gap detection: unprotected guests and offsite copy lag

Status: Accepted (2026-11-26, scope chosen by the user: own page, alarms, Zabbix, Recovery Evidence PDF;
offsite copies measured on PBS instances connected to RESURGAM, overridable with an audited reason).
Built 2026-11-26 on `feat/321-gap-detection`; Zabbix template import checked in Zabbix 7.0. Read-only live
check 2026-11-26 against PVE 9.2 / PBS 4.2: `not-backed-up` and `included_volumes` parse as documented and agree
with each other (19 guests, all covered); PBS accepts `sync-direction=all`/`push` and rejects unknown values with
HTTP 400. Positive case live 2026-11-26 with a throwaway VM: excluded from every job it is listed by PVE
(`no_job`); covered only by a disabled job PVE no longer lists it, RESURGAM reports `only_disabled_jobs`.
Not yet seen live: a real sync job (the test PBS has none).

## Context

RESURGAM only knew about workloads that already had a backup. A VM that no backup job covers never
showed up anywhere, so nothing warned about it. For the offsite leg of 3-2-1, the Recovery Evidence
report counted a workload as "has an offsite copy" when *any* sync job pointed at its datastore. It
never checked that the copy exists or how far behind it is.

## Facts this design relies on (checked against the vendor's own docs and code)

- **PVE: `GET /cluster/backup-info/not-backed-up`** (api-viewer schema, 2026-11-26) lists `vmid`, `type`
  and `name` of every guest no backup job covers. Needs `Sys.Audit` on `/`.
- **Disabled jobs count as coverage.** pve-manager `PVE/API2/Cluster/BackupInfo.pm`
  (`get_included_vmids`) walks every vzdump job in `jobs.cfg` and the legacy `vzdump.cron`, without
  checking `enabled`. A guest whose only job is switched off is *not* listed. RESURGAM finds these
  itself with `GET /cluster/backup/{id}/included_volumes` (guests a job covers) per job.
- **Missing permissions hide guests silently.** The same code drops every guest the caller lacks
  `VM.Audit` on (`/vms/<vmid>`), without an error. RESURGAM's PVE token already needs `VM.Audit` on
  `/vms`; the manual says so for read-only tokens.
- **PBS: `GET /config/sync` returns only pull jobs by default.** Its `sync-direction` parameter
  (`pull` | `push` | `all`, default `pull`; PBS api-viewer schema 2026-11-26) has to be `all` to see
  push jobs (PBS 3.3+). The collector so far listed pull jobs only.
- **Where a copy lands** is defined by the job's `store`/`ns` and `remote-store`/`remote-ns` (for push
  the local side is the source), `max-depth` (namespace recursion), `group-filter`
  (`[exclude:|include:]type:<vm|ct|host>|group:<type/id>|regex:<re>`) and `transfer-last`.
  The remote's `host`/`port`/`fingerprint` come from `GET /config/remote`.
- A synced snapshot keeps its `backup-time`, so the same snapshot has the same time on both sides.

## Decision

### 1. Unprotected guests (per PVE cluster, stored by the collector)

Each collector pass also checks every enabled PVE cluster:

- guests from `not-backed-up` → reason `no_job`;
- guests only in disabled jobs (union of `included_volumes` of disabled jobs minus that of enabled jobs)
  → reason `only_disabled_jobs`.

Results go into `pve_guest_backup_gaps` (`first_seen_at` survives later passes, so the UI can say
"unprotected since"); a guest that is covered again is removed. `pve_clusters.backup_gaps_checked_at`
and `backup_gaps_error` record the last check. RESURGAM's own VMs are skipped: the lab appliance and
disk mount helper templates, helper VMs of disk mounts that are not finished, and members of lab runs
started in the last 24 hours. Guests have no customer, so this list is for platform admins only.

### 2. Offsite copies (per backup group, computed on read)

The collector stores the sync job fields above (plus the remote's host, port and fingerprint). From them
RESURGAM builds **copy edges**: source datastore + namespace → target datastore + namespace. A remote is
matched to a connected PBS instance by certificate fingerprint first, then by host and port.

Every current backup group on a datastore that is neither role `offsite` nor itself the target of a copy
edge is checked:

| Status | Meaning | Alarm |
|---|---|---|
| `ok` | an offsite copy exists and holds every snapshot, or the oldest missing one is younger than the warning threshold | – |
| `lagging` | the oldest snapshot not yet copied is older than the warning / critical threshold | warning / critical |
| `missing` | a sync job covers the group, but no copy exists on the target | critical |
| `local_copy_only` | the only copy is on the same PBS instance (a second copy, not offsite) | warning |
| `target_not_connected` | the sync job's target PBS is not connected to RESURGAM, so the copy cannot be checked | warning |
| `no_copy_job` | no sync job covers the group | warning only if the group's protection policy has `sync_dr_required`, else none |
| `excepted` | an operator accepted the gap with a reason (see 3) | – |
| `not_required` | the customer's "offsite" 3-2-1-1-0 leg is marked not applicable | – |

Where a sync job exists, offsite was intended, so every breakage is an alarm. Without any sync job it is
only an alarm when the protection policy requires Sync/DR (`sync_dr_required`, inert until now): otherwise a
single-site setup -- and every tenant with alert mail switched on -- would get a permanent warning per
workload. Those workloads still show on the page and fail the offsite leg in the Recovery Evidence report.

"Offsite" means: on another PBS instance, or on a datastore with role `offsite`. Lag thresholds are
platform settings (`offsite_lag_warning_hours` 36, `offsite_lag_critical_hours` 72 by default).
Alarms are grouped per source datastore ("3 VMs on X have no offsite copy") to keep the alarm list readable.

### 3. Exceptions with an audited reason

`backup_gap_exceptions` is append-only like `compliance_attestations` (database trigger rejects UPDATE and
DELETE). A row targets one of: a PVE guest (`pve_cluster_id` + `vmid`), a whole PVE cluster, a backup group
(its stable identity `datastore_id` + `namespace` + `backup_type` + `backup_id`, so it survives a new
generation) or a whole datastore. It carries `active`, a required `reason`, an optional `expires_at`, the
author and the time. The newest row per target wins; revoking writes an `active = false` row. Every
change also writes an audit event. Only platform admins can create exceptions; everyone who can see the
entity sees the exception and its reason.

### 4. Where it shows

- **Page "3-2-1 Gaps"** (Reporting): tabs *Unprotected guests*, *Offsite copies*, *Exceptions* (full
  history), plus the lag thresholds.
- **Alarms**: `guest_not_backed_up`, `offsite_copy_missing`, `offsite_copy_lagging_warning` /
  `_critical`, `offsite_copy_gap`.
- **Zabbix**: unprotected guest count, offsite gap count, maximum offsite lag, with triggers.
- **Recovery Evidence PDF**: the offsite leg and its per-workload list use the per-group status above;
  excepted workloads count as covered and show the reason.

## Consequences

- A copy RESURGAM cannot see (tape, an unconnected PBS, S3) shows as a gap until someone records an
  exception with a reason. That is intended: the reason ends up in the audit trail and the report.
- `transfer-last` and `group-filter` gaps show as `lagging`/`no_copy_job`; they are real gaps in the copy.
- Jobs that write to non-PBS storage count as coverage for PVE; RESURGAM does not judge the target.
