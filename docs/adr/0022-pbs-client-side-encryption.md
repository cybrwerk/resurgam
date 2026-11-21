# 0022 — PBS Client-Side Backup Encryption Key Support

Status: **Phase 1 implemented (2026-09-26)** — operators can upload an existing PBS encryption
keyfile for a datastore, and every RESURGAM code path that reads backup content directly (Recovery
Lab boot, `guest_exec_ephemeral` file indexing, `external_mount` file indexing, the file-browsing
API) now uses it. Phase 2 (RESURGAM generating a key itself and applying it to a real PVE storage
definition) is deliberately out of scope here — see "Phase 2" below.

## Context

Proxmox Backup Server supports client-side ("zero-knowledge") backup encryption: a backup created
with `--crypt-mode encrypt` is unreadable to PBS itself, and to anyone without the exact keyfile used
to encrypt it. Before this ADR, RESURGAM had **zero** awareness of this — every
`proxmox-backup-client`/`proxmox-file-restore` call site across the codebase passed only a PBS API
token, never an encryption key. Against a real encrypted backup, every one of these would fail
outright.

This is a materially bigger gap than ADR 0021's guest-disk-encryption problem: that one only blocked
*indexing* a VM's files. PBS-level backup encryption blocks **reading backup content at all** for
every non-PVE-native path RESURGAM has.

One path is *not* affected by this gap: the automated restore-plan flow
(`backend/src/resurgam_api/restore/{command,execution,checks}.py`) never shells out to
`proxmox-backup-client` directly — it drives PVE's own `qmrestore`/`pct restore` via the PVE API, and
PVE decrypts internally using its own storage-level key (`/etc/pve/priv/storage/<id>.enc`). Checked
directly against PVE's own source (`PVE::Storage::Plugin::sensitive_properties`, which lists
`encryption-key` alongside `password` as a deliberately write-only, API-redacted property, same as
every other PVE storage config field of that class): PVE never exposes this key back out through its
API, by design, no matter how privileged the calling token is. So RESURGAM cannot retrieve it
passively — an operator must supply a copy, or RESURGAM must generate one itself and apply it (Phase
2). This also means the automated-restore path already works transparently today *if* the target
cluster/storage has the key configured — restoring to a *different* cluster than the one holding the
key still breaks, a real but narrower, separate limitation not fixed here.

The paths that *do* need this work all go through `restore-worker`'s `pbs_client` package directly:

- `pbs_client/map.py` (`map_snapshot`, `list_snapshot_archives`, `fetch_original_vm_config`) — used by
  Recovery Lab's own boot (`lab_agent/daemon.py::process_job`) and `guest_exec_ephemeral` file
  indexing (`process_guest_exec_ephemeral_job`).
- `pbs_client/catalog.py` (`dump_catalog`, `restore_archive`, `extract_file_to_bytes`) — CT/host
  content, used by `external_mount` file indexing and the file-browsing API.
- `pbs_client/file_restore.py` (`list_path`, `extract`, `extract_to_bytes`) — VM disk content, used by
  the same two callers.

## Decision

**Key storage: on `Datastore`, not `BackupGroup` or `Customer`.** PVE encrypts per storage
*definition* — one key covers everything backed up through it, cluster-wide. A RESURGAM `Datastore`
row (one PBS datastore under one `PbsInstance`) is the closest structural match to that granularity.
Every `BackupGroup` already has a `datastore_id` FK, so resolving a group's key is one join, not an
inheritance chain. New nullable `Datastore.encryption_key_secret_ref_id` (FK to `secret_references.id`,
`ondelete="SET NULL"`, mirroring `OidcSettings.client_secret_ref_id`'s "optional secret" shape — most
datastores have no key at all) and a new `SecretPurpose.PBS_ENCRYPTION_KEY` value, stored the same
way every other RESURGAM secret is (`SecretReference` + `LocalSecretValue`, purpose-derived HKDF
subkey — see the 2026-09-24 credential-store hardening this session built on).

New endpoints on the existing `datastores` router: `POST /datastores/{id}/encryption-key`
(create-or-rotate, mirroring `pbs_instances.py::create_pbs_instance`'s exact secret-store shape) and
`DELETE /datastores/{id}/encryption-key`. `DatastoreOut` gains `encryption_key_configured: bool` —
never the key content or even its secret reference, matching PVE's own write-only convention for this
class of field.

**Delivery differs by where the call actually runs**, and this split is deliberate, not an
inconsistency:

- The backend's own in-process callers (`filerestore/browse.py`, `worker/file_index_worker.py`, used
  by `external_mount` indexing and the file-browsing API) resolve the datastore's key the same way
  they already resolve the PBS token — in-process, straight from the credential store
  (`repository_token_and_keyfile`, renamed from `repository_and_token` to reflect the new third return
  value).
- `resurgam-lab-agent` (Recovery Lab, `guest_exec_ephemeral`) is a separate, remote process that by
  explicit existing design (`RESURGAM_LAB_AGENT_PBS_TOKEN`'s own comment: "Never sent to RESURGAM —
  used only to authenticate this host's own `proxmox-backup-client` calls") never receives secrets
  pushed from the backend — it holds its own locally-configured PBS token. **This feature makes a
  deliberate, narrow exception to that principle**: the encryption key is pushed per-job, as a new
  `encryption_keyfile` field on `LabAgentJobOut`/`LabAgentFileIndexJobOut` (and the matching
  `LabAgentJob`/`FileIndexJob` models in `restore-worker`, kept as duplicates per that package's own
  "no backend dependency" convention). The reasoning: the encryption key is job-scoped and ephemeral —
  used once, for one snapshot, then discarded — unlike the token, which is a broad standing credential
  authenticating every call an agent instance ever makes. Per-host static config (the token's own
  model) also can't cleanly support multiple datastores/keys on one cluster, which a real deployment
  can have.

**Materialization**: `keyfile` is passed through every `pbs_client` function as raw plaintext content
(matching how `token` is already passed as content, not a file reference), and only ever touches disk
via a new shared helper, `pbs_client/_subprocess.py::materialized_keyfile` — a context manager that
writes it to a mode-0600 temp file for the duration of one subprocess call and unlinks it in `finally`
(including on exception). `catalog.py`/`file_restore.py` previously each defined their own local
`_run`/`_run_json` instead of using the already-shared `_subprocess.py` (a pre-existing inconsistency
its own docstring already claimed didn't exist) — folded onto the shared helpers in this same change,
so this keyfile logic exists once, not three times.

**v1 always passes `--keyfile <path>`** whenever the target `Datastore` has a configured key, rather
than pre-checking each snapshot's own `crypt-mode` first (confirmed live that `proxmox-backup-client
snapshot list --output-format json` exposes `crypt-mode` per file, so per-snapshot detection is
possible later if ever needed). Simpler, no new parsing/branching code. Confirmed live (2026-09-26)
that `map`, `restore`, `file-restore list/extract`, and `catalog dump` all list `--keyfile` as a real
CLI option — **not yet confirmed** whether passing an unnecessary `--keyfile` against a genuinely
unencrypted snapshot errors or silently no-ops, since no real encrypted PBS backup exists in the
current test environment to compare against. If it turns out to error, the fix is exactly the
per-snapshot `crypt-mode` check this version deliberately skipped, not a redesign.

## Consequences

- Reading an encrypted backup through any of RESURGAM's own direct-client paths now works once an
  operator uploads the matching keyfile — previously impossible.
- The credential store gains a fourth secret purpose, extending the existing purpose-separation
  hardening (each purpose still gets its own HKDF-derived subkey) rather than reusing an existing one.
- `resurgam-lab-agent` now receives one class of secret pushed from the backend, a narrow, explicit,
  documented exception to an otherwise-held isolation principle — worth remembering if that principle
  is ever revisited or tightened further.
- `repository_and_token` → `repository_token_and_keyfile`'s rename touched every call site
  (`filerestore/browse.py`, `worker/file_index_worker.py`, `worker/file_restore_worker.py`) — a
  deliberate one-time churn to keep the function's name honest about what it now returns, rather than
  leaving a stale name or bolting the third value on as an oddly-named extra parameter.

## Phase 2 (not built here)

The user's stated requirement is broader than "read an already-encrypted backup": RESURGAM should
also be able to *generate* a key itself and apply it to a real PVE storage definition, for a
datastore that isn't encrypted yet — tying into the separate, not-yet-built "RESURGAM creates/manages
PVE backup jobs" idea (Veeam-style authoring, not just inventory).

This is real, separate, higher-risk scope, deliberately not designed in detail here:

- Generating a *correctly-formatted* PBS keyfile should shell out to the real
  `proxmox-backup-client key create <path>` rather than hand-rolling PBS's key format in Python —
  guarantees exact compatibility with whatever PBS version is in use. That binary, and the PVE storage
  config it needs to update (`pvesm set <storage-id> --encryption-key <path>`), both only exist on a
  PVE node — this has to run **via `resurgam-lab-agent`**, as a new one-off action (not a recurring
  poll-cycle job), similar in shape to the existing `ApplianceSnippetJob` one-off pattern.
- **Open, unresolved scoping question**: a RESURGAM `Datastore` row is a PBS-side concept; a PVE
  storage definition (`pvesm`'s own `<storage-id>`, a *per-cluster* config entry that merely *points
  at* a PBS datastore) is a different, potentially many-to-one concept — multiple clusters' storage
  entries could reference the same underlying PBS datastore, each independently encrypted or not.
  Nothing in the current data model derives "which PVE cluster + storage-id backs this `Datastore`
  row" automatically. Phase 2's generate endpoint should take that `(pve_cluster_id, pve_storage_id)`
  pair as explicit operator input rather than guessing — this also happens to be exactly the pair the
  future PVE-job-creation feature will already know at job-creation time, so this endpoint would
  double as a primitive that feature calls later.
- This is a real, consequential mutation of production PVE storage config, affecting every future
  backup through that storage cluster-wide — the triggering endpoint should require explicit UI
  confirmation, not be an implicit side effect of anything else.

## Verification

Unit-tested: `materialized_keyfile` (content, permissions, cleanup including on exception),
`--keyfile` threading through every `map.py`/`catalog.py`/`file_restore.py` function, the new
datastore encryption-key endpoints (set/rotate-in-place/delete, audit events, permission scoping,
never returning key content), `repository_token_and_keyfile`'s resolution (`None` when unconfigured,
correct plaintext when configured). Migration `0048` is exercised by the full backend test suite
(which runs real `alembic upgrade head` against a real Postgres test database, not
`Base.metadata.create_all()`).

**Not yet live-verified — no real PBS-encrypted backup exists in the current test environment.**
Before trusting this in production: create a real `--crypt-mode encrypt` test backup, confirm the
always-pass-`--keyfile` design choice doesn't break reads of *unencrypted* snapshots on the same
datastore, and run one real Recovery Lab boot plus one real `external_mount` file-browse against the
encrypted backup end to end.
