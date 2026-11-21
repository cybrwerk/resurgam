# 0011 — File-restore runs in-process via an editable `restore-worker` dependency

Status: Accepted (2026-09-15)

## Context

Manifest §13's File Indexing/Recovery capability (browsing and downloading individual files out of a PBS snapshot) needs the same PBS-CLI-wrapping code `restore-worker`'s Milestone-0 work already built (`pbs_client.file_restore`, wrapping `proxmox-file-restore` for VM/qemu snapshots) plus a new CT/host equivalent wrapping `proxmox-backup-client catalog dump`/`restore`. `restore-worker` is a separate top-level component (ADR 0001), with no process boundary or job queue (ADR 0002) yet for it to run as an independent service.

## Decision

`backend/pyproject.toml` depends on `resurgam-worker` as an editable path dependency (`uv add --editable ../restore-worker`), and `resurgam_api/filerestore/browse.py` imports `resurgam_worker.pbs_client.{catalog,file_restore}` directly, invoked in-process from three new endpoints under `/api/v1/backup-groups/{id}/snapshots/{id}/files*` and `/api/v1/file-restore/capability`. This mirrors ADR 0006's precedent for the Collector: reuse the same simplification (in-process now, extract later) rather than inventing a second approach for the second component that needs it.

Dispatch is by `BackupGroup.backup_type`:
- CT/host (pxar-based): `pbs_client.catalog`, wrapping `proxmox-backup-client catalog dump`/`restore`. Built this slice, confirmed live against a real PBS 4.2 instance (2026-09-14/15) — browsing (root and nested directories) and single-file download both verified against a real container snapshot with a ~63k-entry catalog.
- VM (qemu disk images): `pbs_client.file_restore`, wrapping `proxmox-file-restore` directly (standalone CLI invocation, no PVE API involved) — confirmed live (2026-09-15) against a real PVE 9.2 node, browsing and downloading real files from a real Windows Server VM backup. Getting there took real debugging (see below); the working end state is a standalone worker calling the CLI exactly as this module already does, no architectural change needed once the environment is set up correctly.

A `GET /api/v1/file-restore/capability` endpoint reports the live host's ability to do file-restore work at all (manifest §18.12 pattern: never raises, always returns a structured per-check report) — `proxmox-backup-client` version, `proxmox-file-restore` version, `/dev/kvm`, `/dev/fuse` — so the frontend can show "VM file browsing isn't available here" instead of failing opaquely.

## Real bugs found building and live-verifying the CT/host path

Three real, non-obvious behaviors of `proxmox-backup-client` were only discoverable by running it live, not from its `--help` text or documentation:

1. **`--version` doesn't exist.** The client takes `version` as a positional subcommand; `--version` exits 255 ("no such command"). Affected the capability check, which silently reported a working install as `degraded`.
2. **`catalog dump` writes its listing to stderr, not stdout.** It goes through the client's own logging framework rather than being treated as command output; stdout is empty on a successful run. The parser originally read only `result.stdout` and silently returned zero entries for a real, working call — no exception, no error, just an empty (and therefore falsely "confirmed empty directory") result. Fixed by reading `stdout + stderr`.
3. **Extraction needs `--ignore-ownership`/`--ignore-permissions` when not running as root**, and **`--pattern <path>` restricts `restore` to a single matching file** rather than the whole named archive — both discovered live while fixing (1) exposed the download path to real testing. `--pattern` also turns "restore the entire archive to extract one file" (the originally-written, correctness-safe-but-wasteful approach) into "restore just the matching entry," which is both faster to write and avoids downloading unrelated snapshot content to satisfy one file-download request.

None of these came from the tool's `--help` output being wrong or misleading — its documented flags are accurate. They were behaviors no amount of static reading would have surfaced; the manifest's own live-verification discipline for external integrations (see `docs/milestone-1-pbs-pve-api-assumptions.md`) is what caught them.

## Getting the VM path working: a real debugging story (2026-09-15)

`proxmox-file-restore` browses a VM disk by booting a minimal micro-VM (kernel + initramfs from `proxmox-backup-restore-image`, run under `pve-qemu-kvm`) that mounts the backed-up disk read-only via QEMU's own `driver=pbs` block device, and talks to it over `AF_VSOCK`. Getting a real result out of this took three separate, real problems, found in this order:

1. **Docker's default AppArmor profile blocks `socket(AF_VSOCK, ...)`** outright (`PermissionError`), independently of seccomp — disabling seccomp alone (`--security-opt seccomp=unconfined`) wasn't enough; AppArmor also had to be disabled (`--security-opt apparmor=unconfined`). The host itself (outside any container) could create `AF_VSOCK` sockets fine, proving this was a Docker-specific restriction, not a kernel/sandbox-wide one.
2. **No init process to reap the daemonized `qemu-system-x86_64` process** — `proxmox-file-restore` double-forks qemu into a detached daemon; without `docker run --init` (or an equivalent reaper), each failed attempt left zombies behind, and repeated attempts left an accumulating pile of them.
3. **After fixing both of the above, the connection was still intermittent** — sometimes it booted and connected, sometimes it didn't, with different failure messages each time (`starting VM timed out`, `list not finished in time`, `VM exited before connection could be established`). Full unfiltered `strace -f` across every thread of the host process, correlated against the guest's own boot log (`/var/log/proxmox-backup/file-restore/qemu.log`), showed the real picture: the guest boots and is ready to serve requests within ~5-8 seconds every time, but the host-side connection attempt sat blocked on `futex(..., FUTEX_WAIT_PRIVATE, 1, NULL)` — **no timeout at all** — for exactly ~10 minutes before finally trying `connect(AF_VSOCK, ...)`, which by then failed with `ENODEV` because the guest's own independent ~10-minute watchdog had already self-terminated it. Two independent ~10-minute timers racing each other, with the host's side deterministically losing.

That race turned out to be an artifact of this specific sandbox's setup, not a bug in the tool: the sandbox had `proxmox-backup-file-restore`/`proxmox-backup-restore-image` installed from the PBS-only apt repo (`pbs-no-subscription`) and `pve-qemu-kvm` from the separate PVE-only repo (`pve-no-subscription`), on Debian 12 (bookworm). A real PVE 9.2 node was checked directly (via SSH) for comparison: **all four relevant packages — `proxmox-backup-client`, `proxmox-backup-file-restore`, `proxmox-backup-restore-image`, and `pve-qemu-kvm` — come from the exact same repo** (`pve-enterprise`/`pve-no-subscription`), on Debian 13 (trixie), as one co-tested release. Running `proxmox-file-restore list`/`extract` directly on that real node — standalone CLI, no PVE API involved, using RESURGAM's own PBS token — worked immediately and reliably: a real Windows Server VM backup's disk archive, partitions, and full NTFS directory tree all listed correctly (~4s including a cold VM boot), and a real file (`logUploaderSettings.ini`) extracted correctly in under a second.

**Conclusion: the standalone architecture from Milestone 0 is correct.** No PVE API dependency is needed for the VM path. What matters is installing `proxmox-backup-client`, `proxmox-backup-file-restore`, `proxmox-backup-restore-image`, and `pve-qemu-kvm` **all from the same PVE apt repo release**, on a matching Debian release (13/trixie for current PVE 9.x) — not mixing the separate PBS-only repo with the PVE repo, and not assuming an older Debian release. The appliance's own base (manifest: Debian + QEMU/KVM, Milestone 5, still unbuilt) should be planned around this from the start.

### A tangent worth recording: PVE's own file-restore API needs privileges RESURGAM's token didn't have

While comparing against PVE's own `file-restore/list`/`/download` REST endpoints (`GET /nodes/{node}/storage/{storage}/file-restore/{list,download}`) as an alternative architecture, RESURGAM's `resurgam@pve!dev` token got a clean, fast `403` — not a hang — on two privileges it was missing: `Datastore.AllocateSpace` (on the storage) and `VM.Backup` (on the VM). Both were granted to the token's custom PVE role (`_Custom_RESURGAM`) during this investigation. RESURGAM ended up not needing PVE's API for the VM path, but if a future design does use it (e.g. as a fallback, or for VMs whose PBS namespace/token RESURGAM doesn't itself hold), these are the privileges required — `Datastore.Audit`/`VM.Audit` alone (read-only) are not sufficient for `list`, let alone `download`.

## A dedicated Debian 13 nested-KVM dev VM (2026-09-16): a capability-check bug, and a new root-privilege finding

A separate, dedicated Debian 13 (trixie) VM was set up specifically for this kind of testing — nested KVM enabled, all four packages (`proxmox-backup-client`, `proxmox-backup-file-restore`, `proxmox-backup-restore-image`, `pve-qemu-kvm`) installed from the same correctly-configured `pve-no-subscription` repo (the GPG key's fingerprint verified against the real signature on the live `InRelease` file before being trusted, not assumed) — matching this ADR's known-good configuration exactly, but as a controlled, repeatable environment instead of ad hoc SSH access to a production-adjacent node.

Running `restore-worker`'s own `run_capability_check()` here first caught a real bug in the checker itself, not the environment: `proxmox-file-restore` reported `DEGRADED` ("version exited with code 255") on a perfectly healthy install. Confirmed live: unlike `proxmox-backup-client` (which takes `version` as a positional subcommand, per the fix above), `proxmox-file-restore` has **no version-reporting command at all** — neither `--version` nor `version` exist ("no such command"). Fixed by parameterizing `_check_binary_version`'s probe command; `proxmox-file-restore`'s check now probes `help` (a real, side-effect-free subcommand every well-formed CLI supports) and doesn't try to extract a version string the tool was never going to produce.

With that fixed, `run_capability_check()` reported a clean `OK` across all four checks (`proxmox-backup-client`, `proxmox-file-restore`, `/dev/kvm`, `/dev/fuse`) — and then a real, new finding surfaced while actually exercising the VM path: **listing/extracting *inside* a VM's disk image via `proxmox-file-restore` requires the calling process to be root** — `/dev/kvm` group membership (`rw` access confirmed working) is not sufficient on its own. Confirmed precisely: the *top-level* listing (`list_path("/")`, which only reads PBS's own backup metadata to enumerate disk images — no QEMU involved) works fine unprivileged; the moment a deeper path is requested (which spins up `proxmox-file-restore`'s internal QEMU-based restore helper), a non-root call fails with `{"message":"EACCES: Permission denied"}` — a raw PBS-side error object that `file_restore.list_path` didn't previously detect, surfacing instead as a confusing low-level `TypeError` from trying to iterate it as a list of entries. The identical call under `sudo` succeeds immediately. This matches how real PVE nodes run this tool in practice (as root, via already-privileged services), so it was never exposed as a constraint before — but it's a real, previously-undocumented requirement for any future deployment where the process calling into `file_restore` isn't already root (relevant to whatever eventually replaces the current in-process-with-the-backend arrangement).

Both paths were then live-verified end to end on this VM, against the same real Sample Customer AG data used throughout this project:
- **CT** (`catalog`, pxar): listed the real 63,370-entry catalog for CT 100/tenant-a in 0.55s; extracted `/etc/hostname` (4 bytes) in 15.25s — consistent with the "fixed cost dominated by total archive size, not target file size" diagnosis below. Notably ~10x faster here than the ~2m25s observed through the actual RESURGAM backend for a similarly small file — but this VM sits on the same LAN segment as PBS (direct, low-latency connection), while the backend's real network path to PBS is almost certainly worse, so that gap should be read as network locality, not evidence that the backend itself adds meaningful overhead (a claim this ADR is not making, having only measured one side of that comparison).
- **VM** (`file_restore`, qemu): listed the real VM 116/tenant-a disk's partition table (as root) in 1.27s including a cold QEMU boot, found the same `logUploaderSettings.ini` real Windows Server file discovered during the original PVE-node verification, and extracted it correctly in 0.045s once the QEMU helper was already warm.

## Consequences

- No duplicated PBS-CLI-wrapping code between the two components — `restore-worker`'s `pbs_client` package is the single implementation, shared by whatever eventually consumes it (a real worker process, the backend, tests).
- The backend now has a real, non-editable-only dependency on a sibling directory outside its own tree; `uvicorn --reload` (which only watches `backend/`) does not pick up `restore-worker` source changes automatically — a manual restart is needed after editing it during development.
- Extraction path once a job queue exists: `browse.py`'s functions take a `Session`/`Settings` plus already-resolved `BackupGroup`/`BackupSnapshot`/`Datastore` objects — the same "plain functions, no FastAPI request-context dependency" shape ADR 0006 used for the Collector — so moving this behind a queue later is a relocation of the call site, not a rewrite of the logic.
- Single-file downloads on the CT/host tool can be slow for large/deep catalogs (confirmed: extracting one small file from a ~63k-entry catalog took over two minutes) — `restore_archive` uses a 300s subprocess timeout, longer than `catalog dump`'s (which only reads metadata and stays fast). This is a real, if unfortunate, characteristic of the underlying tool, not something the wrapper can hide.
- `file_restore.py`'s Milestone-0 field-shape guesses were wrong on every field, now corrected against real captured output: `list`'s entries carry `filepath` (base64-encoded, not a plain `path`), `text` (display name), `leaf` (bool), a single-character `type` (`f`/`d`/`l`/`v` — `v` is the disk-archive/partition-selector level, mapped to `DIRECTORY` since it behaves like one), and `mtime` as a Unix epoch integer (unlike `catalog.CatalogEntry.mtime`, which is an ISO8601 string — `browse.py` normalizes both to the same string shape for `FileEntryOut`). `status`'s real shape is `{"Qemu": {"ids": {"<session-name>": {cid, pid, timeout, uptime}}}}`, not the originally-guessed flat list with a `state` field.
- This original sandbox still cannot reliably run the VM path itself (the version/repo-mismatch problem above is specific to how packages were installed here, not something fixable by retrying); the dedicated Debian 13 dev VM set up 2026-09-16 fills that gap as a real, repeatable environment. `restore-worker`'s own automated tests still mock the CLI boundary using real captured fixtures, the same pattern already used for the CT/host path's tests, rather than requiring a live run on every CI machine.
- The VM path's root-privilege requirement (found 2026-09-16) is a real constraint on any future architecture where `file_restore` calls happen outside an already-privileged context — today's in-process-with-the-backend arrangement means the *backend process itself* would need root for the VM path to work in a real (non-PVE-node) deployment, which a securely-run web service normally shouldn't have. Worth resolving explicitly (e.g. a narrowly-privileged helper process, or specific Linux capabilities instead of full root) before this leaves a development context — not yet a blocker since every real verification so far has run on an already-root-accessible node/VM.

## Addendum (2026-10-07): root is not required -- two directories were; and the appliance never had the tools

Two findings while making file restore work on the first real appliance (the test appliance at 0.2.1, "file
browser not available here"):

1. **The appliance image never installed any of the four packages**, and nothing in the API
   container needs them: every PBS CLI call (CT and VM) runs in the native file-restore/file-index
   workers since ADR 0013. The capability endpoint, still running `run_capability_check()` inside
   the API container, answered for the wrong side. Fix (ADR 0024, "Host packages and the host-setup
   hook"): the image build and every update bundle install the four packages from `pve-no-subscription`
   (trixie) -- exactly the known-good configuration above -- via `appliance/release/host-setup.sh`;
   the host probe reports every precondition as the workers' identity and the API reports per
   backup type which side lacks what.
2. **"Listing/extracting inside a VM disk requires root" was a file-permission problem, not a
   privilege one.** Reproduced on a fresh Debian 13 VM (nested KVM, the four packages from
   `pve-no-subscription`, a throwaway local PBS 4.2.8 with a 64 MiB ext4 disk image backed up as
   `vm/999`): as `resurgam-worker` (group `kvm`) the deep listing returned exactly
   `{"message":"Permission denied (os error 13)"}`. `strace -f` showed, in order:
   `mkdir("/run/proxmox-backup/<uid>")` -> `EACCES` (the per-uid state dir; `/run/proxmox-backup` is
   `backup:backup 0755`); then the micro-VM log rotation/creation in
   `/var/log/proxmox-backup/file-restore/` (`root:root 0755`) -> `EACCES`/`EEXIST`; then QEMU's
   `bind()` of `/run/proxmox-backup/file-restore-qmp-<cid>.sock` -> `EACCES`. With
   `/run/proxmox-backup` group-writable for the worker group (sticky, `1775 backup:resurgam-worker`)
   and the log directory owned by the worker -- a tmpfiles.d entry -- the identical call succeeded
   with **zero capabilities** under the worker units' full sandbox, in ~2.7 s including a cold
   micro-VM boot; so did stdout extraction and the CT catalog dump/restore. `/dev/kvm` and
   `/dev/vhost-vsock` are `root:kvm 0660` on Debian 13, so the `kvm` group covers the micro-VM. The
   guessed `CAP_SYS_ADMIN`-and-friends set (backend/packaging/systemd, 2026-09-24) is gone; see that
   README for the full trace and what was and wasn't exercised. The CID the tool picks is
   `base + uid`, so per-user state can't collide with root's.

A third, independent gap surfaced once the tools were present: the workers never passed the PBS
certificate fingerprint to the CLI tools. Against a PBS with its default self-signed certificate
(RESURGAM's `fingerprint` trust mode) every call failed with "certificate validation failed -
Certificate fingerprint was not confirmed" -- the dev VM had only worked because root's
`~/.config/proxmox-backup/fingerprints` held an interactively confirmed entry, which the sandboxed,
non-interactive workers can't have. `pbs_client` now passes the instance's pinned fingerprint as
`PBS_FINGERPRINT` (colon form), `browse.pbs_fingerprint()` resolves it per datastore. Also:
`proxmox-file-restore list` reports such failures as a bare `{"message": ...}` with exit 0, which
`list_path` now raises with that message instead of a confusing parse error.

Verified end to end on local QEMU/KVM VMs only (never the real PBS/PVE): RESURGAM's own workers,
driven through the API, listed `/drive-scsi0.img.fidx/raw/data` and downloaded `test.docx` from the
VM backup and listed/downloaded from the CT backup, on a freshly built image and on a 0.2.1 install
updated through 0.2.2 and 0.2.3 -- also after a reboot. Still to verify on the test appliance: a real Windows/
NTFS VM disk and a real CT through the real PBS.
