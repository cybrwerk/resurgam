# 0018 — PVE Backup-Job Toggle and PBS Datastore Maintenance Mode

Status: Accepted (2026-09-19)

## Context

The user's first pick off a set of parked future-ideas: a central place in RESURGAM to enable/disable PVE's own vzdump backup-job schedules, and put a PBS datastore into maintenance mode — the actual PVE/PBS-side backup jobs, not RESURGAM's own internal worker jobs (that idea stays separately parked).

Every prior RESURGAM write to live infra (EPIC 9, ADR 0014) was a **transient guest/task action** — start a restore, set a VM's live network config, stop a task. This feature is a different category: writing to a **persistent configuration object** PVE/PBS itself owns (a vzdump job definition; a datastore's own config). No existing ADR covers that, so this one does, extending ADR 0014's "first mutation of live infra" framing rather than amending it.

## Decision

**No new DB tables.** Both actions are live pass-through to PVE/PBS's own config — RESURGAM doesn't own or sync this state, same principle as the existing live-probe endpoints (`list_pve_cluster_nodes`/`list_pve_cluster_guests`), which also persist nothing. PBS/PVE remain systems of record.

**`platform_admin` only, both halves**, matching every existing endpoint in `pve_clusters.py`/`pbs_instances.py` (reads and writes alike). `UserRoleAssignment` structurally supports a narrower per-scope grant (e.g. `backup_operator` scoped to one `pve_cluster`), but nothing exercises that today for infra mutation — not introduced here either. Disabling a customer's backup schedule or taking a datastore read-only should stay behind the existing highest bar, not be the first thing to loosen it.

**No new nav entries.** Both actions nest into existing detail views: the PVE Cluster detail modal gains a "Backup Jobs" section; the Datastores page's existing per-datastore tree node gains a "Maintenance" action plus an always-visible status badge when active (must be obvious wherever the datastore is shown, not buried in a modal only).

**The PVE job toggle needs no read-modify-write.** Confirmed live from the existing `set_vm_network_config` precedent (`pve_api.py`) that PVE's `.../config`-style PUT endpoints apply partial updates, not full-object replacement — so `PUT /cluster/backup/{id}` with just `{"enabled": 0|1}` is safe and doesn't risk clobbering a job's other fields (`exclude`, `prune-backups`, `notes-template`, mail settings, ...).

## Real findings from live verification (2026-09-19, real PVE 9.2 cluster and PBS 4.2 instance)

Two real, previously-unpredicted privilege gaps, found only by actually attempting each write — same "grant exactly what a live error reveals" discipline as ADR 0011/0014:

- **PVE**: schema research (PVE's own `apidoc.js`) said `PUT /cluster/backup/{id}` requires `Sys.Modify` at `/`, which the stored token already had. The real call still 403'd: `Permission check failed (/storage/pbs-main, Datastore.Allocate)` — PVE's real enforcement for this endpoint also checks `Datastore.Allocate` on the job's *target storage* path, not just `Sys.Modify` at the top. Not caught by the schema-only research pass; only found by actually running the write against a real job and reading the real error. Granted (`Datastore.Allocate` on `/storage`), then re-verified live: a real job (`backup-cda2bff1-c477`) was disabled, confirmed via a fresh `GET /cluster/backup`, then re-enabled and confirmed restored — a genuinely safe, reversible live test, timed well away from either real job's actual schedule.
- **PBS**: the stored token (`resurgam@pbs!dev`) started with `Audit`+`DatastoreReader` only — no write privilege existed at all for this token before this feature. Granted `DatastoreAdmin` on `/` (includes `Datastore.Modify`).

One real product-format bug, found only by attempting the actual write against the real API rather than trusting the schema/docs-derived assumption — **PBS's `maintenance-mode` wire format needed two corrections in sequence** before it was right:
1. First assumption (from PBS's published API schema alone): plain `type[,message]` free text. Real call: `400 parameter verification failed... 'type': duplicate key` — an unprefixed message value was parsed as a second positional `type`.
2. Second attempt: explicit `type=<mode>,message=<text>`. This one was *accepted* (`200`) — but the follow-up `GET` echoed the value back **without** the `type=` prefix, proving that isn't the real stored/round-tripped shape either.
3. **Real, confirmed format**: `<mode>[,message=<text>]` — a PBS "property string" where `mode` is the schema's bare/positional default key, and only `message` needs an explicit `key=value`. Both `get_datastore_maintenance_mode`/`set_datastore_maintenance_mode` (`connectivity/pbs_api.py`) were corrected to this shape and re-verified with a full live set → confirm → clear cycle against the real `backup-store` datastore, restoring it to unset.

## Consequences

- `connectivity/pbs_api.py` gains its first-ever write helpers (`_put_data`, `PbsWriteError`) — everything in that module before this was read-only.
- Both write paths are live-verified end-to-end against real infrastructure, not mocks alone, matching this project's established discipline for anything that mutates live PBS/PVE state.
- The `Datastore.Allocate` (PVE) / `Datastore.Modify` (PBS) grants are specific to what these two real write paths actually need — a different cluster/instance may reveal further gaps the same way, expected rather than a design flaw.

## Addendum 2026-11-11: disable a backup job for a limited time

**Context.** Disabling a vzdump job is mostly done for a maintenance window, and the risky part is
forgetting to switch it back on: the job stays off silently and backups stop. Requested directly by
the user ("disable should have a reactivate after x hours").

**Decision.**

- The disable action takes an optional duration (`POST …/backup-jobs/{job_id}/disable` with
  `{"reenable_after_hours": 1..168}`; no body still means "until further notice"). More than a week is
  deliberately not offered: that is a decision to switch the job off, not a maintenance window.
- PVE has no "disabled until" concept, so the deadline is RESURGAM-owned state: one row per
  (cluster, PVE job id) in `pve_backup_job_reenables`, existing only while a re-enable is pending —
  the same keying as `pve_backup_job_customer_assignments` (ADR 0023), since a vzdump job has no
  RESURGAM row of its own.
- The re-enable runs in the existing `resurgam-collector-scheduler` tick (60 s), not a new process:
  it is the same kind of periodic, unprivileged PVE HTTP call the scheduler already makes, and adding a
  fifth service for it would add deployment surface for no isolation gain.
- At fire time the job is re-read from PVE first. Unreachable → keep the row, record the error, retry
  next tick. Job gone or already enabled → drop the row (audited as
  `pve_vzdump_job_auto_reenable_dropped` with the reason) without writing. Otherwise enable it
  (`pve_vzdump_job_auto_reenabled`). A failed write keeps the row with `last_error`, shown in the UI,
  and is retried every tick: a pending re-enable is never silently given up, because giving up would
  recreate exactly the failure this feature exists to prevent.
- Any explicit change replaces the timer: manual enable, disabling again, an edit that sets `enabled`,
  deleting the job. `DELETE …/reenable-schedule` cancels it (job stays disabled);
  `PUT …/reenable-schedule` sets one on a job that is already disabled (409 for an enabled job).

**Consequences.**

- If `resurgam-collector-scheduler` is not running, timers don't fire; the UI keeps showing the
  passed deadline, which makes that visible rather than silent.
- The automatic write uses the cluster's stored token and needs the same PVE privileges as the manual
  toggle (see above: `Sys.Modify` on `/` and `Datastore.Allocate` on the target storage).
- Not yet live-verified against a real PVE: covered by unit tests with PVE mocked. The underlying write
  (`set_vzdump_job_enabled`) is the one already live-verified above.
