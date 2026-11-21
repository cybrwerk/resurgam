# 0013 — File-restore becomes an async job queue, consumed by a separate worker process

Status: Accepted (2026-09-16)

## Context

File Indexing/Download (manifest §13) ran synchronously in-process since ADR 0011, inside the FastAPI request handlers in `api/v1/filerestore.py` — a deliberate, documented interim simplification ("no job queue exists yet to route this through one properly, and this is simpler to build and test now").

Two real, live-confirmed problems made that simplification untenable:

1. **CT/host downloads block the HTTP request for minutes, regardless of file size.** Reproduced live: downloading a 947-byte file from a CT snapshot took 2m25s, because `proxmox-backup-client restore --pattern` must stream the entire pxar archive to extract anything from it — inherent to the tool, not fixable by backend-side optimization.
2. **VM file-restore beyond the top-level disk listing requires the calling process to be root.** Confirmed live (2026-09-16, on a dedicated Debian 13 nested-KVM dev VM set up for this purpose): the identical `proxmox-file-restore` command fails `EACCES` unprivileged and succeeds under `sudo`; `/dev/kvm` group membership alone is not sufficient. RESURGAM's uvicorn backend process must not run as root in a real deployment — this is a hard architectural blocker for the VM path staying in-process, not a performance concern.

The user asked for this to become async for **both** paths explicitly — for CT/host this solves the blocking-request problem; for VM it is what makes privilege separation possible at all.

This is also the first real implementation of **ADR 0002** (Postgres-native job queue via `SELECT ... FOR UPDATE SKIP LOCKED`, decided 2026-09-11), which explicitly named "file-restore sessions" as an anticipated job type but was never built (`grep -rn "SKIP LOCKED" backend/src/` returned nothing before this work). It directly extends **ADR 0011**'s own stated design intent: `browse.list_directory`/`browse.download_file` already take a `Session`/`Settings` plus pre-resolved ORM objects with no FastAPI request-context coupling, specifically so that "moving this behind a queue later is a relocation of the call site, not a rewrite of the logic."

Manifest §15 ("Interne Worker-Schnittstelle") is the job contract this was built against — its own example names `job_type: "file_restore_list"`, confirming *listing*, not just downloading, is meant to be an async job. This also settles the VM path's listing case: any listing beyond the trivial top-level "which disk images exist" enumeration hits the same root requirement as download, so it needs the same privileged worker.

## Decision

**The queue-consuming loop lives in `backend/`, as a second OS process, not inside `restore-worker/`.** `restore-worker/`'s own README stated plainly that "any queue/job wiring to a Control Plane" was deliberately not there yet, and its `pyproject.toml` has exactly one dependency (`pydantic`). Giving it SQLAlchemy/DB access to poll `file_restore_jobs` directly would either invert the existing `backend → restore-worker` dependency edge or force it to hand-roll raw SQL against a table schema `backend/alembic` owns. Instead, `backend/src/resurgam_api/worker/file_restore_worker.py` is a new, separate entrypoint (`resurgam-file-restore-worker`, a `[project.scripts]` entry), launched as its own process distinct from `uvicorn resurgam_api.main:app`, reusing `backend`'s existing DB session factory (`db.get_session_factory()`, newly exposed), `Settings`, and `LocalEncryptedSecretsProvider` — all already free of request-scoped coupling. It calls `browse.list_directory`/`browse.download_file` **unchanged**.

Privilege separation is achieved by **process** separation: the worker process runs privileged (root, for the VM path); uvicorn stays unprivileged. Real systemd packaging of this worker stays explicitly out of scope here (Milestone 5, per `restore-worker/README.md`'s existing scoping) — this slice only needed a documented dev-run command (`sudo uv run resurgam-file-restore-worker`, run from `backend/` so `.env` still resolves under `sudo`'s reset environment). **Left deliberately open**: whether Milestone 5's real systemd unit ends up running this `resurgam_api.worker` entrypoint as-is, or whether `restore-worker/` eventually absorbs DB access itself and becomes the real executable. Not resolved here.

**Two tables, mirroring the `SecretReference`/`LocalSecretValue` split** (`models/secrets.py`): `file_restore_jobs` (polled by the frontend roughly every 1.5s while a job is in flight — status, type, path, small JSONB `result` for `list` jobs, error fields, timestamps) stays lean; `file_restore_job_content` (a separate table, `job_id` PK/FK + a `LargeBinary` column) holds a completed `download` job's actual bytes, so the frequently-polled parent table never carries blob weight.

**Both `list` and `download` route through the queue**, not just downloads — per the manifest's own `file_restore_list` job type, and because VM listing beyond the top level needs the same privileged worker as VM download. A narrower alternative (route only the VM path, or only downloads, through the queue) was considered and rejected: it would mean the frontend needs two different code paths for "list" depending on how deep the path is or which backup type answered, for a case (CT listing, or the VM top-level listing) that's fast today only because it does very little work — not worth the bifurcation.

**No lease/heartbeat mechanism.** A job the worker crashes on mid-run has no signal back to the queue other than `expires_at`. The expiry sweep (`_expire_stale_jobs`) therefore covers **both** `pending` and `running` jobs, not just `pending` — this is what makes a crashed job self-heal back to a terminal `expired` state instead of hanging forever (and any frontend still polling it hanging with it). This is a deliberate, stated MVP trade-off: acceptable for a single-worker deployment (this codebase's current target), not a general answer for multiple concurrent workers racing to recover each other's crashed jobs.

**Two transactions per job, not one.** `_claim_next_job` (the `SELECT ... FOR UPDATE SKIP LOCKED` claim) commits immediately, releasing the row lock right away; the actual work (`_run_job`, calling `browse.py`) runs in a fresh, separate session. `download_file`'s CT/host path can run ~2.5 minutes live — holding a `FOR UPDATE`-locked row open idle-in-transaction for that long on a shared Postgres connection would be a real operational problem independent of whether a second worker exists to contend for the lock.

**Content retention is TTL-based, not single-use-delete-on-fetch** (confirmed with the user rather than assumed) — completed download content stays available for the same `expires_at` window that governs job claiming (`Settings.file_restore_job_ttl_seconds`, default 600s), swept by the same worker loop. A narrower "delete immediately after the first successful fetch" policy was considered, given this briefly stores decrypted backup file bytes in Postgres, but rejected as unnecessary complexity for this slice — the existing TTL window is short enough, and matches the expiry-sweep pattern already in place for the rest of the queue.

*Addendum (2026-09-18, manifest §10.3 pass):* the manifest's own §10.3 ("Session-Lifecycle") text asks for download links "kurzlebig und einmalig nutzbar" — short-lived *and* single-use. This is a direct textual conflict with the decision above, re-raised explicitly with the user while implementing §10.3's other session-lifecycle requirements (session-duration enforcement, cleanup, content-size quota, download audit — see `filerestore/sessions.py`). Re-confirmed: leave this as-is, TTL-only. Noted here so a future reader sees this was revisited deliberately, not missed a second time.

**Worker privilege is unattended full `sudo` for now**, not a narrower `setcap`/scoped-sudoers grant (confirmed with the user) — matches Milestone 5's existing scope boundary (packaging/hardening is explicitly future work), not something this slice should pre-empt.

**RBAC**: job creation (`POST .../files/jobs`) and every read of a job (`GET /file-restore/jobs/{id}`, `GET /file-restore/jobs/{id}/download`) re-run `require_restore_access` against the job's own `backup_group_id` — a job id is not itself a capability. A guessed or leaked job id from another customer's scope must 404, the same discipline `restore_plans.py` already established for `RestorePlan` ids.

## Frontend

`FileBrowserPanel.tsx` reworked to create a job then poll `GET /file-restore/jobs/{id}` via TanStack Query's `refetchInterval` — **this app's first use of that pattern** (confirmed: zero existing polling anywhere in the frontend before this). Structured as create-once-then-poll (a `useMutation`/`useEffect` that creates the job, storing its id in state, then a separate `useQuery`/`useQueries` keyed on that id) rather than a single reused query, since a naive `queryFn` that re-creates the job on every interval tick would spawn a new job every 1.5s instead of polling one. Multiple concurrent downloads are still supported (`useQueries` over each in-flight download job id), matching the pre-existing multi-download UX.

## Verification

Backend: 20 new tests (`test_file_restore_worker.py`, extended `test_filerestore_api.py`) plus the full existing suite — 318 tests pass. Includes one genuine cross-connection `SKIP LOCKED` test (two independent `engine.connect()` sessions, bypassing the SAVEPOINT-wrapped `db_session` fixture, cleaning up its own committed rows): connection A claims a job without committing, connection B's claim correctly returns `None` rather than blocking or double-claiming.

Frontend: 88 tests pass (rewritten `FileBrowserPanel.test.tsx`/`FileBrowserModal.test.tsx`/`RestorePointsModal.test.tsx`/`FileRecoveryPage.test.tsx` to mock the new `createJob`/`getJob`/`downloadJob` API shape), `tsc --noEmit` clean, production build succeeds.

Live end-to-end verification against the real dev VM/PBS instance (running the worker via `sudo uv run resurgam-file-restore-worker`, confirming the original >2-minute blocked request is gone and the VM path's previously-`EACCES` deeper listing/extraction succeeds through the job path) is tracked as a follow-up, not yet performed as of this ADR's acceptance.

## Addendum (2026-09-24): first attempt at narrowing "unattended full sudo"

This ADR's own decision ("Worker privilege is unattended full `sudo` for now, not a narrower `setcap`/scoped-sudoers grant... matches Milestone 5's existing scope boundary") stayed unaddressed through this ADR and the two after it (`0016`, `0021`) that also touch these workers. A first attempt lives at `backend/packaging/systemd/` — real systemd units for both `resurgam-file-restore-worker` and `resurgam-file-index-worker` running as a dedicated non-root user with a `CapabilityBoundingSet` in place of full root, plus ordinary systemd sandboxing directives.

The capability set itself is an informed starting point, explicitly not a proven minimal one — this sandbox has never been able to run a real `proxmox-file-restore`/QEMU-KVM environment end to end, the same limitation ADR 0011 already hit trying to pin down the exact reason root is required at all. `packaging/systemd/README.md` carries the full reasoning per capability and a concrete live-verification procedure to run before trusting this in production.

## Addendum (2026-10-07): no root, no capabilities

The privilege separation this ADR introduced turned out to need no privilege at all: the
"EACCES unless root" behind "the worker process runs privileged (root, for the VM path)" was
`proxmox-file-restore` failing to create its state directory, QMP socket and log under
root/backup-owned directories (ADR 0011 addendum 2026-10-07). With a tmpfiles.d entry making
`/run/proxmox-backup` and `/var/log/proxmox-backup/file-restore` writable for `resurgam-worker`
(installed by `appliance/release/host-setup.sh`), both workers run as that user with an empty
capability bounding set -- live-verified for CT and VM listing and download through the job queue.

## Addendum (2026-10-08): content on disk, folder and multi-selection ZIP downloads

**Content leaves Postgres.** The `file_restore_job_content` table (the bytes of a completed
download, capped at 500 MB) is gone (migration 0056). The worker writes content as a file to
`Settings.file_restore_output_dir` -- on the appliance `/var/lib/resurgam/file-restore`, created by
`host-setup.sh` (owner `resurgam-worker`), writable in the worker unit via `ReadWritePaths`,
bind-mounted read-only into the API container like `/var/lib/resurgam/updates`. The API streams it
(`FileResponse`, never read into memory); the content limit is now a disk-sized
`FILE_RESTORE_MAX_CONTENT_BYTES` (default 50 GB) plus a free-space reserve
(`FILE_RESTORE_MIN_FREE_BYTES`, default 2 GB) checked when a download job is created (507), before
extraction, and every couple of seconds during it (`browse._DiskGuard`, via
`pbs_client._subprocess.run_guarded`). Layout and sweep: `filerestore/storage.py`.

**The TTL decision above still holds, with one change:** a completed download's `expires_at` is
reset to completion + TTL, so a long extraction can't use up the download window. The expiry
sweep now also deletes the content file of completed jobs past `expires_at` (the job stays
`completed` with `result_storage_name = null`, audit `file_restore_content_deleted`) and anything
in the directory no live job owns (crash leftovers). (Before, completed content rows were never
swept at all -- only pending/running jobs were.)

**New job type `archive`**: several files and/or directories of one snapshot as one ZIP, names
relative to the selection's common parent. VM: each entry via `proxmox-file-restore extract <path>
- [--format zip]` (the directory ZIP is built inside the restore micro-VM; a local target
directory is not an option unprivileged, see `file_restore.extract_to_file`), merged into the final
ZIP. CT/host: one `proxmox-backup-client restore` with a `--pattern` per entry, retried entry by
entry if the combined call fails. Unreadable/special/missing entries are skipped and reported
(`archive_report`), not fatal; names Windows can't create are made safe and reported. Verified
2026-10-08 with proxmox-backup-client/proxmox-file-restore 4.2.8 against a throwaway local PBS
(ext4 + NTFS VM disk, CT pxar), as `resurgam-worker` under the worker unit's sandbox.

**Downloads stream to disk in the browser too**: `POST /file-restore/jobs/{id}/download-ticket`
returns a 60-second, job- and user-bound signed link (`purpose` claim, never a session) that the
browser navigates to, instead of buffering the body as a Blob. The download endpoint re-runs
`require_restore_access` for the ticket's user, and audits paths, counts and sizes -- never content.
