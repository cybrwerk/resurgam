# 0023 — Backup & Maintenance Job Administration (PVE vzdump + PBS prune/verify/sync/remotes/GC)

Status: Accepted (2026-09-27)

## Context

RESURGAM's control plane has been read-heavy: the Collector (ADR 0006) inventories PBS state, and PVE writes so far have been transient guest/task actions (EPIC 9) plus one existing exception — ADR 0018 added a live enable/disable toggle for a PVE vzdump backup job. The user wants RESURGAM to become the authoring surface for backup and maintenance job *definitions* generally ("like Veeam's approach") — not just vzdump, but every real, separately-schedulable job category a working PBS/PVE backup setup actually has:

- **PVE vzdump jobs** (`/cluster/backup` on PVE) — what actually runs a backup.
- **PBS prune jobs** (`/config/prune`, scoped per datastore+namespace) — retention.
- **PBS verify jobs** (`/config/verify`, same scoping) — integrity checking.
- **PBS remotes** (`/config/remote`) — another PBS server this instance can pull from/push to.
- **PBS sync jobs** (`/config/sync`) — remote replication, referencing an existing Remote.
- **PBS garbage collection** — not a separate job list; a single `gc-schedule` field on the datastore's own PBS config (`/config/datastore/{name}`).

**Explicit, load-bearing correction from the user**: retention must be managed through PBS's own Prune jobs, *not* through a vzdump job's `prune-backups`/`remove` properties. This matches how the real environment is already configured (real PBS prune jobs exist per datastore+namespace with real `keep-daily`/`keep-weekly`/`keep-monthly`/`keep-yearly` values, while the real vzdump jobs carry no meaningful prune settings of their own). **The vzdump job field set deliberately excludes `prune-backups`/`remove`.**

A second scope addition came mid-implementation: sync jobs depend on a Remote existing, so RESURGAM needs full CRUD on Remotes too, not just a read-only picker.

Both systems' capability gating follows ADR 0018's precedent, extended and generalized: **which of import/edit/create RESURGAM offers, per job category, is driven by the configured token's real, granted privileges — checked live via each system's own `/access/permissions`, never assumed.**

## Decision

**No new DB tables — live pass-through only**, following ADR 0018's explicit precedent. Each category's "import" is the same shape: an existing read-only list becomes clickable into a real `GET .../{id}` edit form; "create" is the same form, empty, POSTing new. GC, having no job-list shape at all, is a schedule field read/written directly against the datastore's own PBS config.

**Capability gating is computed live per system, never cached** — `PveCapabilitiesOut`/`PbsCapabilitiesOut` (`{can_read, can_write}`), each derived from that system's own `GET /access/permissions` for the credential already on file. PVE's check is `Sys.Audit`/`Sys.Modify` at `/` (ADR 0018 already found this alone insufficient for a real write — see Findings). PBS's checks are per job category:

| Category | read_privs | write_privs |
|---|---|---|
| Prune | `Datastore.Audit` | `Datastore.Modify` |
| Verify | `Datastore.Audit` | `Datastore.Verify` |
| Remotes | `Remote.Audit` | `Remote.Modify` |
| Sync | `Datastore.Audit` | `Datastore.Backup`, `Remote.Audit` |

The real token this backend uses already holds every one of these, so — unlike ADR 0018's PVE side, which had a real observed 403 to ground `Datastore.Allocate` — these PBS privilege sets are this project's best-grounded expectation from PBS's own documented roles, not individually confirmed by a live permission gap. A write that still 403s for an unpredicted reason surfaces PBS's real raw error text to the operator directly, same discipline as ADR 0018.

**Field sets are common, practically useful subsets, not the full schema**, matching this project's established pattern (documented per-dataclass in `connectivity/models.py`).

**Remote credentials are never persisted**, a deliberate contrast with ADR 0022's PBS-encryption-key handling: `password` is forwarded straight through to PBS on create/update and never stored by RESURGAM in any form. PBS's own scheduler is the only future consumer of a remote's credential — RESURGAM itself never needs to re-present it, unlike an encryption key.

## Real findings from live verification (2026-09-26/27, real PVE 9.2 cluster and PBS 4.2 instance)

**PVE**: `POST /cluster/backup`'s response body is empty even on success, and real PVE-generated job IDs are random UUIDs with no way to match a just-created job back afterward. RESURGAM generates its own id (`resurgam-{uuid4().hex[:12]}`) and passes it explicitly — confirmed live that PVE accepts and uses a caller-supplied id as-is. `GET /access/permissions` (no `userid` param) defaults to the calling token's own identity, confirmed via a direct HTTPS call with the real token auth header, not just local `pvesh` as root.

**PBS**: `GET /access/permissions` is a real, separate endpoint mirroring PVE's own, same `{path: {privilege: bool}}` shape. `RemoteRemote`'s original (pre-session) field set was missing real fields entirely: `auth-id` is required on create, `fingerprint` is a real optional field, and `password` is write-only (never returned by GET, matching PVE's own `encryption-key`/`password` convention). `RemoteSyncJob`'s original field mapping was corrected the same way — `remote` is genuinely optional (an intra-instance/namespace sync omits it).

**A real schema gap in the code shipped earlier this session, found only by attempting the actual write** — same discipline as ADR 0018's `Datastore.Allocate` and maintenance-mode wire-format findings: the code assumed `disable` was a valid field on every PBS job-list category by pattern-matching from Prune jobs (which genuinely have one, confirmed live). It is **not** — PBS's real `/config/verify` and `/config/sync` schemas both reject `disable` outright on create *and* update (`400: parameter verification failed - 'disable': schema does not allow additional properties`), while the identical field on `/config/prune` is accepted without error. A Verify or Sync job can only be removed entirely; it cannot be toggled off in place. `PbsVerifyJob`/`RemoteSyncJob` (and their schemas/API payload builders) were corrected to drop the field entirely, re-verified live, and the full test suite re-run (835/835 passing) after the fix.

**Full live round-trip, confirmed and cleaned up** for every category this session added: Remotes (create → list → update `comment` only → delete, confirmed `password` never round-trips), Sync Jobs (create referencing an existing Remote → get → update `schedule` → delete — the category the original plan flagged as needing "a first 'does this even work as documented' pass," which is exactly where the `disable` gap above was caught), and GC schedule (read `07:45` → set `08:15` → read back → restored to `07:45`). Prune and Verify jobs' CRUD (built and live-verified earlier in this same implementation pass) were re-confirmed reachable through the real RESURGAM API endpoints, not just raw PBS calls. The real PBS instance was left in its exact original state: 3 prune jobs, 2 verify jobs, 1 pre-existing remote (`local`), 0 sync jobs, `gc-schedule: 07:45`.

## Consequences

- New router `api/v1/pbs_jobs.py` (prune, verify, remotes, sync — 20+ endpoints, all `platform_admin`-only and audited), extending the existing `pbs_instances.py`/`pve_clusters.py` write patterns rather than replacing them.
- `connectivity/pbs_api.py` gained its first `_post_data`/`_delete_data` helpers (previously read+`_put_data`-only since ADR 0018's maintenance-mode work), plus full CRUD for four new resource types.
- `connectivity/models.py`'s `RemoteRemote`/`RemoteSyncJob` are now live-verified rather than documentation-derived — a correction that also benefits the Collector's own `sync_instance_sync_jobs` ingestion (`collector/sync.py`), which reads the same `list_sync_jobs` function.
- Frontend work (nesting these in the PVE Cluster detail modal and Datastore detail view) is explicitly **not yet built** — this ADR covers the backend/connectivity layer only, live-verified end-to-end through the real RESURGAM API, not yet exposed in the UI.
- The PBS privilege sets in the capability-gating table above are this project's best-grounded expectation, not individually confirmed by an observed 403 the way ADR 0018's `Datastore.Allocate` was — a different PBS token/environment may reveal a real gap the same way, expected rather than a design flaw.
