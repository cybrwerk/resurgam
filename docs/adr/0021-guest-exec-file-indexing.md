# 0021 — Guest-exec File Indexing: two opt-in methods, one shared foundation

Status: **Not recommended as a primary method.** Every stage up to final retrieval is live-verified
correct (hookscript trigger, job creation, guest-exec dispatch, guest-side pruning, chunked output),
and two further real, decisive bugs found live in the retrieval path were fixed and confirmed
(`exec-status` unreliability, a stale-manifest race). But after all of that, four independent live
runs against a real, moderately-loaded production VM (116, "Office-PC") **still** fail
deterministically at the same point (~120,000 entries / chunk 24) with `"got timeout"` on a single
chunk read well under 1MB. The leading theory (Windows Defender content-scanning) was tested directly
— an exclusion for RESURGAM's working directory was applied and confirmed active — and did **not**
fix it. The true root cause was not isolated despite substantial live investigation; see "Final,
unresolved state" below. Given four rounds of "fix a real bug, hit a new failure" without ever
reaching a clean end-to-end run against real, non-trivial data, this mechanism's reliability profile
for real file transfer over QGA is judged too fragile to invest further in right now. The recommended
path forward — not yet designed or built — is a persistent, always-on "mount helper" worker VM that
snapshots hot-attach to (reusing the same `proxmox-backup-client map` primitive `guest_exec_ephemeral`
already uses), sidestepping QGA file-transfer reliability entirely for the common **unencrypted**-disk
case; `guest_exec_live`/`guest_exec_ephemeral` would then be reserved for the narrower case that
actually requires them -- a genuinely encrypted disk, where nothing outside the guest's own unlocked
OS can read the bytes regardless of transport. Discussed 2026-09-25/26, not yet written up as its own
ADR.

## Context

`external_mount` (ADR 0016) walks a VM's disk externally via PBS's own mount tools, one subprocess
call per directory. Two real, long-standing gaps: an encrypted disk (BitLocker/LUKS) is invisible
to it (skipped as "unmountable," never indexed), and there's no true incremental indexing —
`list_path()`/`dump_catalog()` always return a full listing, no diff API. This was raised and
deliberately parked in an earlier session pending a design pass for what was flagged as "a
materially bigger trust/security boundary than anything else in RESURGAM's design."

## Decision

Offer **both** as sibling, per-group opt-in methods alongside `external_mount`, not a replacement —
different workloads want different trade-offs:

- **`guest_exec_live`**: fires at the completion of the VM's own real backup and runs `guest-exec`
  against the already-running production VM (a PVE VM backup never stops the guest). Runs exactly
  when a real backup happens, and because it runs at every real backup, it can ask for only what
  changed since the last index (`find -newermt`/`LastWriteTimeUtc` filtering) — genuinely
  incremental, closing the second gap too. Trade-off: guest-exec now runs against a customer's live
  VM, not a disposable clone.
- **`guest_exec_ephemeral`**: boots a disposable clone of a snapshot (reusing Recovery Lab's
  zero-copy map/boot machinery, ADR 0019/0020) and indexes that, then destroys it. Never touches
  production; stays fully isolated. Trade-off: heavier per run (a real boot), and — like
  `external_mount` — only runs when explicitly scheduled/triggered, not tied to real backup timing.

Both share the same trust-boundary discipline as Recovery Lab's check types: the executed
enumeration command is fixed and project-authored, never operator- or customer-suppliable, and
strictly read-only.

## Data model (migrations 0041–0044)

- `BackupGroup.file_index_method`: `"external_mount"` (default) | `"guest_exec_live"` |
  `"guest_exec_ephemeral"`. VM-only in practice.
- `BackupGroup.guest_exec_live_pve_cluster_id`: required only for `guest_exec_live` — the cluster
  hosting this specific VM. Scopes which agent (authenticated per-cluster) may see/claim this
  group's jobs and confirms a `backup-completed` report actually came from the right cluster.
- `CustomerFileIndexSettings.guest_exec_pve_cluster_id` / `guest_exec_target_node` /
  `guest_exec_target_storage`: the default lab-enabled cluster/node/storage for
  `guest_exec_ephemeral` boots.
- `CustomerFileIndexSettings.full_reindex_every_n_runs`: every Nth guest-exec index for a group
  forces a full (`since=None`) walk instead of an incremental one, and — once the new rows land —
  purges any previously-indexed path not seen in that run. An incremental pass only reports
  new/modified files, never deletions/renames; this is the reconciliation step for that, made
  operator-visible rather than silently never happening.
- `FileIndexJob.file_index_method`: snapshotted from the group at job-creation time (same
  "snapshot at creation, don't re-read mutable parent state" precedent as `RestoreTestPlan.
  customer_id`).
- `FileIndexJob.guest_exec_target_vmid` / `guest_exec_target_node`: for `guest_exec_ephemeral`, a
  freshly allocated clone VMID; for `guest_exec_live`, the production VM's own real VMID/node.
- `FileIndexJob.is_full_reindex`: decided once at claim time from the group's guest-exec run
  history and `full_reindex_every_n_runs`.
- `FileIndexJob.trigger` gains `"guest_exec_hook"` (alongside `manual|bulk|scheduled`), for jobs
  created by the hookscript-relay flow.

No changes to `FileIndexEntry` — all three methods write the identical row shape through the
identical write path (`worker/file_index_worker.py`'s `_insert_entries`/`_link_unchanged_rows`/
`_compile_exclude_patterns`/`_is_excluded`, reused verbatim).

## Backend endpoints (`api/v1/lab_agent.py`)

- `GET /lab-agent/file-index-jobs` — pending jobs for either guest-exec method on the
  authenticated agent's own cluster, including the group's own `exclude_patterns` (added live,
  2026-09-24 — see the addendum's finding #6) so the agent can prune matching subtrees during
  enumeration instead of walking and transferring everything unfiltered.
- `POST /lab-agent/file-index-jobs/{id}/claim`, `/entries` (batched rows, calls the reused
  entry-write path and carries forward unchanged prior-run entries — skipped on a full-reindex
  run, which is what actually reconciles deletions), `/status`.
- `POST /lab-agent/backup-completed` — the hookscript's trigger relay (`guest_exec_live` only):
  resolves the `BackupGroup` for `(cluster, vmid)`, re-checks the method/enablement independently
  rather than trusting the trigger payload, triggers a scoped re-sync of just that group's
  datastore, and creates a `FileIndexJob` with `trigger="guest_exec_hook"`.

There is deliberately no per-agent "reconcile target" endpoint — see the addendum below for why.

## `restore-worker` primitives (`pve_client/qm.py`, `lab_agent/file_index.py`, `lab_agent/client.py`)

- `guest_exec(vmid, command)` — `qm guest exec` (PVE's blocking wrapper around
  `guest-exec`+`guest-exec-status` polling).
- `guest_file_read_all(vmid, path, *, node)` — `pvesh get /nodes/<node>/qemu/<vmid>/agent/file-read
  --file <path>` (PVE's own REST API, called via the local `pvesh` CLI). See the addendum for why
  this is *not* `qm guest cmd`'s `guest-file-open`/`-read`/`-close`.
- An OS-aware enumeration-command builder with an optional `since` cutoff: Linux
  `find … -newermt … -printf …`, Windows an equivalent PowerShell `Get-ChildItem`, both prefixed
  with a low-priority hint (`nice`/`ionice`, `-Priority Idle`) since `guest_exec_live` runs against
  a VM serving real traffic.
- TSV/CSV parsers for both enumeration outputs.
- `LabAgentClient` methods wrapping every endpoint above (`list_file_index_jobs`,
  `claim_file_index_job`, `submit_file_index_entries`, `report_file_index_status`,
  `report_backup_completed`).

## Live-verification addendum (2026-09-24)

Both methods were run against real infrastructure (a real PVE 9.2 node, a real Windows VM —
"Office-PC," this project's long-standing test subject — and a real PBS 4.2 instance). Several
real, decisive findings surfaced only by actually running this, none of which static review or unit
tests could have caught:

**1. `guest_exec_ephemeral`'s VM builder discarded the original NIC model.** The first version
hardcoded `net0 = "model=virtio,bridge=vmbr0,link_down=1"`, replacing the guest's real NIC model
(here, `e1000`) outright — the exact class of mistake `_build_member_vm_config`'s own docstring
already documents and fixes for Recovery Lab (a synthetic config instead of the guest's real
hardware once sent a real Windows restore into repair mode). Live-confirmed here too: a clone with
a forced `virtio` NIC never became guest-agent-reachable and showed a persistently black display
for 10+ minutes, while `info blockstats` showed continuous, heavy disk I/O the whole time —
consistent with Windows stuck doing new-hardware detection rather than a normal boot. Fixed by
preserving the original NIC's model/MAC and only touching `bridge=`/`firewall=`/`link_down=`,
matching `_build_member_vm_config`'s own precedent. After the fix, the guest agent did come up on
a retry.

**2. Ephemeral cleanup called `destroy_vm()` without stopping the VM first.** `qm destroy` refuses
outright on a running VM ("VM N is running - destroy failed") — the common case, since most real
failures (e.g. a guest-agent timeout) leave the VM running; only the readiness *check* failed.
Fixed by calling `stop_vm()` (a hard power-off — appropriate for a disposable clone already being
destroyed) before `destroy_vm()`.

**3. `wait_for_guest_agent`'s 180s default was too short.** A real Windows boot from a zero-copy
PBS-mapped device is genuinely slow (confirmed via `info blockstats`: continuous, substantial disk
I/O well past 180s, not a hung VM — every block potentially round-tripping to PBS on first read).
Made operator-configurable (`RESURGAM_LAB_AGENT_GUEST_EXEC_AGENT_WAIT_TIMEOUT_SECONDS`, default
600s) rather than a bigger hardcoded guess.

**4. `guest_exec_live`'s entire hookscript mechanism targeted the wrong PVE feature.** The original
design (and this ADR's first version) used the VM-level `hookscript` property
(`qm set <vmid> --hookscript ...`), reconciled per-VM by the agent. Live testing found this never
fires at all: confirmed against PVE's own shipped reference scripts —
`/usr/share/pve-docs/examples/guest-example-hookscript.pl` (the real `hookscript` property) only
supports `pre-start`/`post-start`/`pre-stop`/`post-stop`, never anything backup-related. The
mechanism that actually fires on backup phases is vzdump's own, separate, **node-level** `script:`
option (confirmed against `/usr/share/doc/pve-manager/examples/vzdump-hook-script.pl`, whose
`backup-*`/`log-end`/`pre-stop`/`pre-restart`/`post-restart` branch is called as
`<script> <phase> <mode> <vmid>` — phase first, not vmid first, a second real arg-order bug the
first hookscript draft also had). Corrected design, genuinely simpler than the one it replaced:

- The shipped hookscript (`debian/resurgam-guest-exec-live-hook.sh`) reads `$1`=phase, `$3`=vmid,
  and PVE's own `$HOSTNAME` env var for node — no `hostname` subprocess call needed.
- It's referenced directly from `/etc/vzdump.conf`'s `script:` line — a **one-time, node-level**
  operator/packaging step (documented in `debian/resurgam-lab-agent.default`), not something
  `resurgam-lab-agent` deploys or reconciles at runtime. `/etc/vzdump.conf`'s `script:` accepts a
  plain local filesystem path directly — no PVE snippet-storage indirection needed at all (the
  per-VM `hookscript` property required that; the corrected mechanism doesn't).
- The whole per-VM reconcile loop (`reconcile_guest_exec_live_hookscripts`, `list_local_vmids`,
  `set_hookscript`/`clear_hookscript`, `GET /lab-agent/guest-exec-live-vmids`,
  `PveCluster.lab_snippet_storage_id` reuse for this feature) was removed entirely — nothing to
  reconcile per VM once the trigger is node-wide. The script fires on every real backup of every
  VM on the node; the backend's own re-validation in `backup-completed` (already there from the
  start) is what scopes it down to VMs that actually opted in.
- Live-confirmed after the fix: a real backup of Office-PC correctly wrote a trigger file, the
  agent's `poll_backup_triggers` picked it up and relayed it, the backend created a real
  `FileIndexJob` (`trigger="guest_exec_hook"`), and the agent claimed and processed it.

**5. `guest_file_read_all` was built on a QGA command family `qm guest cmd` doesn't expose.**
`guest-file-open`/`guest-file-read`/`guest-file-close` are real, documented QEMU Guest Agent
commands, but confirmed live that PVE's `qm guest cmd` CLI wrapper only exposes a curated subset
(`qm help guest cmd`: `fsfreeze-*`, `fstrim`, `get-*`, `info`, `network-get-interfaces`, `ping`,
`shutdown`, `suspend-*` — no `file-*`, no `exec`/`exec-status` either, those go through `qm guest
exec` directly). Every real call failed ("400 too many arguments"). PVE's REST API (confirmed via
`pvesh get /nodes/<node>/qemu/<vmid>/agent`, which lists real supported commands including
`exec`/`exec-status`/`file-read`/`file-write`) exposes a higher-level `file-read` that wraps the
whole open/read-loop/close sequence into one call and returns already-decoded `content` directly —
confirmed live reading a real file off a real Windows guest. Rewritten to shell out to
`pvesh get /nodes/<node>/qemu/<vmid>/agent/file-read --file <path>` instead — still a local CLI
tool (PVE's own API over its local socket), consistent with this module's "local tools only"
precedent; `qm guest exec` itself needed no change; `guest_file_read_all` gained a required `node`
parameter (PVE's REST path is node-scoped, unlike `qm`'s own cluster-wide vmid resolution).

**6. No exclude-pattern awareness at all — guest-exec walked (and kept) everything.** Raised
directly by the user watching a live test ("95mb for a catalogindex?"): the enumeration command had
no knowledge of `BackupGroup.file_index_exclude_patterns`, walking the *entire* drive unconditionally
and transferring the full result back before any filtering happened. Worse: the submit endpoint
(`submit_lab_agent_file_index_entries`) never applied exclude patterns server-side either -- every
excluded path was being inserted into the index anyway, a gap `external_mount`'s own worker doesn't
have. Live-confirmed impact: a real backup's enumeration walked `Windows/WinSxS` (already excluded
by default for `external_mount`, for exactly this reason) and produced a 95MB listing, with the
single blocking `guest_exec` call running **9+ minutes unattended against the real production VM**
before being force-killed -- directly undermining this method's whole "low-impact against a live
guest" design premise. Fixed two ways: (a) `build_enumeration_command` now accepts
`exclude_patterns` and **prunes** matching subtrees during the walk itself -- PowerShell via a
manual recursive `Walk` function (`Get-ChildItem -Recurse` has no native pruning), Linux via GNU
`find`'s own `-regextype posix-extended ... -prune -o` construct; (b)
`submit_lab_agent_file_index_entries` now also filters server-side as defense-in-depth, matching
`worker.file_index_worker`'s existing belt-and-suspenders precedent for `external_mount`. Patterns
are operator-editable content (not a trusted literal), so both the PowerShell and shell embeddings
are properly quoted (`_powershell_single_quote`/`_posix_shell_single_quote`) -- naive interpolation
here would have reopened the exact "no arbitrary scripts" trust boundary this project holds
everywhere else. Live-confirmed effective: the same real backup's listing dropped from 95MB to 44MB
with the full default exclude set applied -- a real, if partial, win.

**7. PVE's `file-read` API appeared to have a real ceiling well under 44MB (later corrected --
see below).** Even after finding #6's fix, the same real VM's guest-exec-live job still failed --
consistently, deterministically, not intermittently -- with `qga command 'guest-file-read' failed -
got timeout`, at the time thought independent of the earlier AV-lock issue (no lock-contention message
this time, and a genuine network interruption during one attempt was ruled out as the cause by
repeating the test cleanly). Checked directly: `pvesh get /nodes/<node>/qemu/<vmid>/agent` lists every
QGA command PVE's REST layer actually exposes -- `file-read`/`file-write` only, no `file-open`/
`file-read`-with-offset/`file-close` at any level, via either `qm guest cmd` or `pvesh`. PVE's own
`file-read` is an all-or-nothing call with no documented size limit and, at the time, appeared to have
a real practical one well under 44MB -- and no lower-level primitive exists to manually chunk around
it regardless. **This size-ceiling theory was disproven by further live testing on 2026-09-25 -- see
"Root cause" below.** Chunking the output was still the right fix (any single all-or-nothing transfer
is fragile against exactly the AV-scanning behavior that turned out to be the real cause), just not
for the reason originally believed.

**This is the one remaining real blocker for `guest_exec_live` against any VM with a non-trivial
amount of data**, and every other stage of the mechanism is proven correct up to this exact point:
a real backup correctly fires the corrected node-level hookscript, the trigger is correctly relayed
and consumed, the backend correctly creates a `trigger="guest_exec_hook"` job scoped to the right
cluster/node, the agent correctly claims it and runs `guest_exec` (which itself completes
successfully, producing a real, correctly-pruned output file) -- only the final retrieval of a
real-sized file is blocked. The correct fix is **not** a bigger retry budget (already tried: 3
attempts, then 8, then 40 -- a deterministic ceiling doesn't respond to retrying) but genuine
chunking at the *source*: have the guest-side enumeration write multiple smaller files (e.g. one per
top-level subtree, or split at a fixed row/byte count) and have the agent read and submit each
incrementally, rather than one all-or-nothing walk-then-read. This is real, separate scope --
touching the enumeration script's output strategy, the read/submit loop in `daemon.py`, and likely a
small per-job "which chunks are done" bookkeeping -- not implemented in this session.

**Current state (as of finding #7's discovery, 2026-09-24)**: `guest_exec_ephemeral`'s mechanism is
proven correct piece by piece (map, COW overlay, real-hardware-preserving boot, guest-agent
readiness, guest-exec, file-read against a small file) but a full clean run was never sat through to
completion end-to-end in this environment (this specific test infra's zero-copy boot I/O was slow
enough, independent of this ADR's own findings, that doing so wasn't practical in-session) -- the
ephemeral path would hit the exact same file-read ceiling as `guest_exec_live` once a real listing
is produced, so it needs the same chunking fix. `guest_exec_live`'s mechanism is fully proven
end-to-end through job creation and guest-exec execution; only the final chunked retrieval remained.

**Chunked retrieval fix (2026-09-25)**: rather than a bigger retry budget or a different transport
(the real alternative considered was Veeam's guest-agent-push-via-admin-share model, rejected
outright -- deploying a helper binary into the guest over SMB is architecturally the same shape as
lateral-movement tooling, triggers exactly the AV/EDR friction this ADR already hit once over a much
smaller issue, and needs an admin-share attack surface this project has no reason to open. Nakivo and
most maturer tools instead avoid guest injection for bulk file listing entirely -- mounting the disk
externally and only using an official hypervisor guest-interaction channel, QGA's own equivalent,
for what genuinely needs guest cooperation. That's the shape this project already had), the fix stays
entirely on the existing QGA/`pvesh` channel and chunks the *output*: `build_enumeration_command`
now has the guest write many small chunk files (~5000 rows each, comfortably under a megabyte
regardless of total listing size) plus one tiny manifest recording the chunk count, instead of one
big listing. Linux pipes `find`'s own output through GNU `split -d -a 4` (no custom chunking logic
needed); Windows switched from a single `ConvertTo-Csv | Out-File` pipeline to a manual
`System.IO.StreamWriter`-based `Walk`/`Emit`/`NextChunk` loop that rolls over to a new file every
`$chunkSize` rows -- and, since the CSV format's quoting machinery no longer bought anything once
this was already manually building the row string, Windows output switched to the same plain TSV
shape Linux already used, so both OSes now share one parser (`parse_windows_csv_output` removed).
The manifest is written *last*, once every chunk file is already closed, so its mere existence is the
completion signal -- `daemon._read_and_submit_chunks` reads it first to learn the chunk count, then
reads/parses/submits each chunk in order, so entries start reaching the backend as each chunk arrives
rather than only after the whole listing is retrieved. Deliberately not a "probe chunk N+1, treat
file-not-found as done" loop -- an earlier finding this same session (guest-agent error text observed
in German on one real VM) already showed that telling a genuine failure apart from an expected
condition by matching `pvesh`'s stderr text is not reliable across locales. `guest_file_read_all`'s
own retry budget was reduced (40 attempts/~200s down to 10/~30s, later widened to 20/~100s -- see
below) to match: it was tuned generously for the old single-giant-file AV-scan-lock case, but
retrying that long makes little sense per chunk now that there can be dozens of them, and a small
chunk should clear a brief lock far faster than a 95MB one did. Unit-tested
(`test_lab_agent_file_index.py`, `test_lab_agent_daemon.py`), then live-verified through several
rounds of real testing against VM 116 (below) -- chunking itself worked cleanly from the first real
run (18-74 chunk files produced and read back correctly across multiple attempts), which is what
made it possible to isolate the two further real bugs described next.

**8. `qm guest exec-status` is unreliable for a genuinely long-running command on a real, busy
Windows guest (found live, 2026-09-25).** Two prerequisite bugs had to be fixed before the chunking
fix could even be tested end-to-end: first, `guest_exec()`'s assumption that plain `qm guest exec`
blocks until completion was itself wrong -- `qm help guest exec` reveals its real default is
`--synchronous 1 --timeout 30`, waiting at most 30 seconds before returning just `{"pid": N}` while
the guest-side command keeps running, completely unsupervised, in the background. Live-confirmed:
a real full-drive enumeration (several minutes) hit exactly this, and a second trigger for the same
VM raced a second, independent background PowerShell process against the first, corrupting both
runs' chunk output (both processes found via `Get-Process`, killed, guest cleaned up). Fixed with
explicit `--synchronous 0` plus an own `exec-status` polling loop (`guest_exec`), and a second,
separate issue found in that same loop -- an isolated poll failure ("got timeout") must not be fatal,
only a poll still failing once the overall deadline passes should raise.

Even with both of those fixes, a live run still got stuck: `exec-status` reported `exited: 0` (or
errored) for **20+ minutes** *after* the guest-side script had already finished and its manifest
already existed on disk (confirmed directly: the manifest's own `LastWriteTime` was 20+ minutes in
the past while the agent was still blocked polling). QGA's own pid-exit tracking simply never caught
up, for reasons never fully isolated (a QGA service restart did not clear it). The fix: stop trusting
`exec-status` for this specific case entirely. `guest_exec_launch()` (extracted from `guest_exec`)
dispatches fire-and-forget; `lab_agent.daemon._wait_for_manifest` polls for the manifest file itself
to become readable instead, which is the *actual thing being waited for* rather than an indirect,
apparently-unreliable proxy for it. `guest_exec` (the polling version) is kept for other, faster
commands where it has worked reliably all session; only the enumeration dispatch stopped using it.

**AV-scanning theory, tested and disproven (2026-09-25/26).** With both of the above fixed, live runs
against VM 116 consistently got through 24 chunks (120,000 entries) -- the same exact number, three
times in a row at first -- before failing on one specific chunk with the same `"got timeout"` error
finding #7 was originally about. Direct investigation seemed to isolate a clear cause: a synthetic
900KB test file (`'f' * N`) read back instantly every time; the actual failing 951KB chunk did not,
even after a fresh `qemu-ga` service restart; and copying that exact chunk's content to a brand-new
filename failed identically, pointing at content-triggered AV scanning (a large plain-text listing of
real file paths being flagged by Defender's real-time heuristics for deeper content inspection) rather
than a size limit. **This theory was then tested directly and disproven.** A Defender exclusion for
RESURGAM's working directory was applied on the guest and confirmed active
(`Add-MpPreference -ExclusionPath 'C:\Windows\Temp\resurgam-index-chunk-*'` /
`...-manifest.txt`, `RealTimeProtectionEnabled: True` otherwise) -- a *fourth* live run still failed at
the exact same point. Worth noting for anyone re-investigating this: a separate, real bug (finding #9,
below) means the first three "repeat failures at the same point" may not even have been independent
fresh runs at all -- they could have been re-reading the same stale, never-refreshed chunk file each
time, which would make the earlier AV-scanning evidence (including the copy-to-new-filename test, run
in the same short window as the original failure) far less conclusive than it appeared at the time.
The fourth run, after that bug was also fixed, was confirmed fresh (verified via `chunk-0000`'s own
`LastWriteTime`) and still failed identically. **The true root cause was not isolated.** Applying the
Defender exclusion is still worth keeping as a documented, low-cost precaution (every mainstream
backup agent requires the same class of exclusion for its own working path, for good general reasons
even if it didn't fix this specific failure), but it should not be presented as a confirmed fix.

**9. A stale-manifest race, found live while trying to get a clean fresh run (2026-09-25).** Cleanup
of a previous run's chunk/manifest files was originally the enumeration script's own first statement.
Since the enumeration is launched fire-and-forget (`guest_exec_launch`, finding #8), it hasn't
necessarily reached that cleanup line yet by the time `_wait_for_manifest`'s very first poll runs --
so a *stale* manifest from an earlier run could be found readable immediately and mistaken for this
run's own fresh result, silently submitting old data as if it were new rather than failing loudly.
Live-confirmed: a chunk file's `LastWriteTime` was hours older than the run that supposedly just
produced it. Fixed by making cleanup its own separate, confirmed-complete step
(`file_index.build_cleanup_command`, run via the reliable polling `guest_exec`, not
`guest_exec_launch`) that the caller runs *before* ever dispatching the real enumeration -- by
construction, no stale manifest can exist by the time the fresh one is polled for. This is a real
correctness bug independent of finding #7's unresolved mystery, and stays fixed regardless of what
happens with the rest of this ADR.

**Final, unresolved state (2026-09-26):** four independent live runs against VM 116, across several
rounds of real bug fixes (fire-and-forget dispatch + polling instead of an unreliable blocking
assumption, poll-tolerant retries, manifest-based completion detection instead of unreliable
`exec-status`, a stale-manifest race fix, a Defender exclusion), all still fail at the same point
(~120,000 entries / chunk 24, `"got timeout"` on a single sub-1MB chunk read). Every other stage --
hookscript trigger, job creation, guest-exec dispatch, guest-side pruning, chunked output, most of
chunked retrieval -- is proven correct. Only this one, specific, reproducible-but-unexplained failure
remains, and it has resisted every hypothesis tested so far (size ceiling: disproven; content-
triggered AV scanning: tested directly, disproven; stale/racing data: ruled out on the last attempt).
**Recommendation: stop investing further in hardening this specific mechanism.** Four rounds of
"fix a real bug, hit a new failure" without a single clean end-to-end completion against real,
non-trivial data is a strong signal of a deeper reliability problem in QGA-based bulk file transfer
on this real-world guest, not a sequence of independent, nearly-exhausted edge cases. The better use
of further effort is very likely the persistent-worker/hot-attach approach discussed 2026-09-25 (see
Status above) for the common unencrypted-disk case, reserving `guest_exec_live`/`guest_exec_ephemeral`
for genuinely encrypted disks where guest cooperation cannot be avoided -- accepting that this
narrower use case may keep exactly this reliability profile until/unless someone identifies the actual
cause.

**Also still open:**

- Finding #9's stale-manifest-race fix and finding #8's `exec_status`-unreliability fix are both real,
  confirmed, independent improvements and should be kept regardless of what happens with the
  unresolved chunk-24 mystery above -- they're correctness/robustness fixes to the mechanism, not
  band-aids over it.
- `guest_exec_ephemeral`'s mechanism is proven correct piece by piece (map, COW overlay,
  real-hardware-preserving boot, guest-agent readiness, guest-exec-launch, chunked file-read) but a
  full clean run was never sat through to completion end-to-end (this test infra's zero-copy boot I/O
  is slow enough, independent of every other finding above, that doing so wasn't practical this
  session) -- it uses the exact same `guest_exec_launch`/`_wait_for_manifest`/chunked-read path as
  `guest_exec_live`, so every fix above applies equally, and it would very likely hit the exact same
  unresolved chunk-24-style failure once a real listing is produced.
- `report_backup_completed`'s scoped re-sync can race PBS's own post-backup indexing -- one real
  attempt found the brand-new snapshot not yet queryable seconds after vzdump reported success,
  even though a fuller sync moments later found it fine. Not yet handled with a retry; a group's
  guest-exec-live job creation could silently miss its own triggering backup's snapshot under this
  timing.
- The frontend method/cluster picker (`FileIndexExcludePatternsModal.tsx`) and the customer-level
  ephemeral-boot settings (`CustomersPage.tsx`) are built and tested but were not part of this
  session's live verification (the live runs above were driven directly through the API).
