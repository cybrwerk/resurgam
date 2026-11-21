# Recovery Lab: a safe deploy pipeline

Not yet built — this is a design, written after a real, manual, SSH-driven build-and-deploy cycle
(2026-09-21/22, see ADR 0019/0020) surfaced every risk below concretely, not hypothetically. Each
section names the real incident it's responding to.

**Status 2026-10-07:** §1 is built differently than proposed here -- the `.deb`s are built in a pinned
`debian:trixie` stage of the backend image (not CI) and ship with every image/update bundle; §2 is
built as RESURGAM's guided "Build template" (cloud-init first boot with a seed ISO instead of
`virt-customize`, no credentials inside the guest, prober version recorded in the template
description). See ADR 0019's 2026-10-07 addendum. §3-§6 remain open.

**Status 2026-10-11:** a node is now *paired* with a one-time code instead of an SSH install (ADR
0027): the pairing script installs the agent `.deb` this RESURGAM ships, and after pairing the agent
itself builds the probe appliance and share helper templates on its node (structured build jobs,
never commands; build code in `resurgam_worker.lab_build`). Re-running the pairing on a node is the
agent's upgrade path after a RESURGAM update -- still a deliberate, operator-triggered step, as §5
asks (the script keeps a PBS token already set on the node). §7's first item is done: lab-run cleanup
stops and destroys the run's member and probe appliance VMs. §3, §4 and §6 remain open.

## The real risks this session actually hit

1. **Build host and deploy target must share the same Debian release, not just architecture.**
   `dh_virtualenv`'s venv is not truly portable across releases with different default Python
   versions — `bin/python3` is a symlink to the *target* machine's own system interpreter, not a
   bundled copy. A package built on trixie (Python 3.13) installed cleanly on bookworm (Python
   3.11) — `apt` reported success — and then failed at *runtime*, on first import, with no signal
   at build or install time. This is a "worked in CI, broken in prod" class of bug with an unusually
   late failure point.
2. **Manual template rebuilding is slow and has already produced two real bugs.** Cloning a
   template, injecting throwaway cloud-init credentials for build access, SSHing in, installing
   packages, stripping the credentials back out, and re-templating — done by hand three times this
   session — produced a `dh_installsystemd --name=` misconfiguration that silently shipped zero
   systemd units, and a `meta`-key rejection that broke `qm create` outright. Neither was caught
   until a real boot attempt.
3. **A template's actual contents aren't discoverable without logging into it.** Nothing on the PVE
   side records which `resurgam-lab-prober` version, which Debian release, or which extra apt
   packages a given template VMID actually has baked in. Every "what's on 9003 again?" question
   this session was answered by SSHing in and checking, not by reading a manifest.
4. **A template was pointed at as `lab_appliance_template_vmid` and clonable well before it had ever
   been proven to actually work.** The first real boot from the rebuilt (trixie) template still hit
   two more bugs (the config-reconstruction gap, the `meta` key). Nothing structurally prevented an
   operator from pointing a real customer's cluster at an unverified template.
5. **Cleanup doesn't destroy VMs.** Every live run this session left `qm destroy` as a manual
   follow-up step. Left unattended, failed/abandoned runs accumulate real VMs (and, separately,
   orphaned `proxmox-backup-client map` loop-device mappings that block the *next* run's map
   attempt until manually unmapped) on the customer's actual PVE node.
6. **The riskiest component runs as root on infrastructure RESURGAM doesn't own.**
   `resurgam-lab-agent` is a privileged, host-installed daemon on the customer's own PVE node (ADR
   0019's own explicit design choice, for real bandwidth/cost/proof-relevance reasons) — a bad build
   there has a materially worse blast radius than a bad backend deploy, and RESURGAM has no
   automatic-rollback lever for it at all.

## Proposed pipeline

### 1. Build in a pinned container, not an ad-hoc VM

Replace the manual `rsync` + `dpkg-buildpackage` over SSH to `resurgam-dev-vm` with CI (the repository
lives on GitHub, so GitHub Actions or any other runner) building both `.deb`s inside a pinned `debian:trixie` container
image. The image tag is the single source of truth for "what Debian release this build targets" —
eliminates risk #1 structurally (there is no second machine to drift out of sync with) rather than
by operator discipline. Runs the full `restore-worker` test suite (109+ tests as of this writing)
before packaging; publishes versioned `.deb` artifacts (an internal apt repo, or at minimum
predictable per-version artifact URLs) rather than the ad-hoc `/tmp` copies used this session.

### 2. Build the golden template offline, not via a live SSH session

Use `virt-customize` (libguestfs) against the base cloud image directly — no booting, no cloud-init
credential injection, no live network dependency, nothing to remember to strip back out afterward
(directly closes risk #2's credential-handling half). A versioned recipe (a script or a Packer
`proxmox-iso`/`proxmox-clone` template, either is fine) pins:
- the exact base cloud image (URL + checksum — currently fetched ad hoc from
  `cloud.debian.org/images/cloud/trixie/latest/`, not pinned to a specific dated build),
- the exact `.deb` version from step 1 to install,
- the exact extra apt packages (`qemu-guest-agent`, `dnsmasq`, `nftables`, `nmap` — all three real,
  previously-undiscovered-until-boot gaps this session hit are exactly this list),
- and never touches `ciuser`/`sshkeys`/a static `ipconfig0` at all, since nothing needs interactive
  access.

The recipe's output is converted to a template and tagged with its own recipe version + git commit
(PVE supports free-text `tags`/`description` on a VM — `qm set <vmid> --description "..."`),
directly closing risk #3: `qm config <vmid>` (or a small RESURGAM admin-UI read of it) becomes the
answer to "what's actually on this template," no SSH needed.

### 3. Automated smoke test, gating whether a template/package build is ever marked deployable

Reuse the real Recovery Lab machinery itself as the integration test, rather than a bespoke
parallel script — it already proves everything a smoke test would want to prove. A dedicated,
non-production "Lab of the Lab" PVE cluster (see §4) runs one real `RestoreTestPlan` against a
small canary backup on every new template/package build:
- execute → snippet written and confirmed → appliance cloned, dual-homed, started
- member mapped, config-reconstructed, booted, and actually reaches `guest_agent` pass
- at least one Level 2/3 check (`icmp` or `arp`) executed by the real prober and returns a result
- `cleanup` runs and the run's VMs/mappings are gone afterward

Only a build that passes this end-to-end gets its template/package version marked deployable (a
simple flag/tag, not a new subsystem) — directly closes risk #4: no template is ever pointed at by
a real `lab_appliance_template_vmid` before it has been proven to actually boot and probe
correctly, closing the exact gap that let the `meta`-key and config-reconstruction bugs reach a
real customer VM's restore attempt this session.

### 4. A non-production canary cluster, never a live customer cluster, for build verification

This session's testing happened directly against a real home-lab PVE node with real production VMs
as backup sources — acceptable for this environment, but the pipeline this document describes must
not assume that's always available or appropriate. §3's smoke test needs its own small, disposable
PVE cluster (or even a single lab-enabled node) with a synthetic canary backup, kept structurally
separate from any customer's production `PveCluster` rows — a bad `resurgam-lab-agent` build should
never get its first real-world exercise on infrastructure a customer depends on.

### 5. `resurgam-lab-agent` stays a manual, operator-triggered upgrade — by design, not by gap

Keep this session's own discipline: `resurgam-lab-agent` is never auto-updated. It's the one
component in this whole pipeline running privileged on infrastructure RESURGAM doesn't own (risk
#6) — every version change ships with a real, human-readable changelog entry (already this
project's convention) documenting exactly what changed and why, and an operator applies it
deliberately (`apt-get install <version>.deb`, as done by hand throughout this session) after
reading it. Nothing here proposes changing that. What the pipeline *does* add is confidence
*before* that manual step: a version that failed §3's smoke test should never reach the point of
being handed to an operator to install at all.

### 6. Keep N-1, never destroy the last known-good template immediately

Informal practice this session (old templates were kept around, not destroyed, until a replacement
was proven) becomes policy: a new template version is created alongside, not instead of, the
current one; `PveCluster.lab_appliance_template_vmid` is only repointed after §3 passes; the
previous template is only destroyed after the new one has completed one real production run
successfully. Gives a real, fast rollback path (repoint the field back) if a production run
surfaces something the canary cluster didn't.

### 7. Close the two operational gaps this session hit repeatedly, independent of the above

Neither of these is really a "pipeline" concern, but both were real, recurring costs during this
work and belong on the same list:
- `cleanup_restore_test_plan` should destroy member/appliance VMs — **done**: cleanup now stops and
  destroys them (only VMs confirmed as members of the `resurgam-lab` pool). At the time every live run needed a
  manual `qm destroy` pair afterward.
- A failed/interrupted `proxmox-backup-client map` can leave an orphaned loop-device mapping that
  blocks the *next* run's map attempt on the same archive until manually `unmap`'d — hit multiple
  times this session. `resurgam-lab-agent` could reasonably check for and clear a stale mapping for
  the same archive before attempting a fresh `map`, rather than requiring manual intervention.

## Explicitly out of scope for now

- A full internal apt repository / package signing infrastructure — versioned artifact URLs from CI
  are enough to start; a real repo is a natural upgrade once there's more than one Lab-enabled site
  pulling packages.
- Automatic `resurgam-lab-agent` updates — deliberately rejected above (§5), not deferred.
- Multi-cluster staged/canary rollout *of the same build* across several real production Lab
  clusters — relevant once a deployment has more than one, not the case in this environment today.
