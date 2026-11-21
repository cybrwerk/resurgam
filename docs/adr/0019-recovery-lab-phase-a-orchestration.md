# 0019 — Recovery Lab, Phase A: mount-based restore via a customer-installed `resurgam-lab-agent`

Status: Accepted (2026-09-19)

## Context

Manifest §22 ("Scheduled Restore Test Orchestrator", EPIC 10) sat as a blocked nav placeholder (`/recovery/recovery-lab`) since ADR 0012, which found it needs a scheduler, SDN isolation, and an unbuilt `resurgam-lab-agent`. This ADR went through three real design iterations in one session, each driven by a live finding, not a preference:

1. **First draft**: single designated PVE cluster, PVE SDN Simple zone, a fresh VNet per run, PVE's `live-restore` flag for a fast boot without the heavier §23.2/§23.3 "Instant Lab Mode." Extended twice further before any code ran: to support several Application Groups per customer (§22.4, missed initially) and several lab-enabled clusters with admin-configurable SDN zones (not a singleton).
2. **`live-restore` was live-tested against a real snapshot and found fundamentally unusable for this feature.** Two real failures: without a network override, PVE tried to reuse the archive's *original* bridge (`vmbr1`, absent on the target node) and failed outright — live-restore starts the VM as *part of* the restore call, so a bad network config isn't a soft misconfiguration, it's a hard task failure. Passing an explicit `net0` override to fix this was accepted by the API schema but **silently ignored by live-restore's internal boot step** (the failure just cited a different, still-wrong bridge name) — confirmed by two separate live attempts, not assumed. This ruled out `live-restore` entirely for Recovery Lab.
3. **The real mechanism, live-proven end-to-end on 2026-09-19**: `proxmox-backup-client map <snapshot> <archive>` — a real PBS client command — maps a snapshot's disk directly as a local block device (confirmed: `/dev/loop0`, a real readable MBR disk, `fdisk -l` output captured) with **zero copy**. A VM config built from scratch (not via `qmrestore`) pointing `scsi0` at that device (`ro=1` — required, PBS serves the mapped image read-only) and `net0` at the real Lab VNet **booted successfully**, confirmed alive via `qm status --verbose` (real QMP blockstat, memory ballooning). This sidesteps the network bug entirely, since RESURGAM controls the VM config directly instead of going through `qmrestore`'s internal machinery — and it's genuinely zero-copy, not "copy sooner," which is what the manifest's heavier §23.2 Instant Lab Mode was actually describing. Cleaned up (VM destroyed, device unmapped, confirmed via `losetup -a`) after the test.

This last step requires local, privileged execution (`proxmox-backup-client map` produces a host-local device; QEMU and that device must be on the same machine) — the PVE REST API has no endpoint for it (confirmed: searched the real apidoc schema for any snapshot-map-as-device resource, found none — only the file-level `file-restore/list`/`download` endpoints already used by `restore-worker`). This is exactly the gap manifest §23.3 names `resurgam-lab-agent` for.

**Where the agent runs was itself a real design correction, not assumed**: the first instinct was "RESURGAM operates its own Lab-compute cluster, so no customer infrastructure is ever touched." That's wrong for this product's actual goals, for three concrete reasons the user gave: (1) **bandwidth** — a centralized Lab-compute host means every block read during boot crosses a WAN link between the customer's PBS and wherever that host lives, largely negating the zero-copy advantage; (2) **compute cost** — centralizing means the RESURGAM operator provisions and scales Lab-compute for every customer's testing load, which doesn't scale for self-hosted/community deployments; (3) **proof-relevance** — testing on a generic RESURGAM-owned environment only proves a backup boots *somewhere*; testing on the customer's actual target cluster proves it boots on the real storage/network a disaster recovery would actually use, which is the entire point of the feature. `restore-worker`'s own real-world deployment (a separate, operator-controlled VM — `resurgam-dev-vm` in this environment — not embedded in any PBS/PVE host) was already proof that RESURGAM's privileged components are meant to be installed by whoever operates the infrastructure, not centrally by RESURGAM itself.

## Decision

**The agent runs on whichever PVE cluster is flagged for Lab use** (`PveCluster.is_lab_compute`) — normally the customer's own cluster, installed and operated by whoever runs that cluster, exactly like `restore-worker` is installed by whoever operates its host. RESURGAM never needs elevated PVE privileges from the customer beyond what already exists (their PBS reachable on 8007, already required for everything else) — `SDN.Allocate`/`Pool.Allocate`/`VM.Clone`, all live-verified this session, are granted on *that* cluster by *its own* operator, the same as every other PVE privilege this project has ever needed.

**`resurgam-lab-agent`→RESURGAM communication is an outbound-only HTTPS backchannel, never direct database access.** The first instinct here was also wrong: reuse `restore-worker`/`file-index-worker`'s DB-polling pattern (ADR 0011/0013). That pattern fits *their* topology — infrastructure the same operator controls, on the same trusted network as Postgres — but not a component that, in the normal case, runs on separate infrastructure (possibly a different site, different trust zone) from the backend. Direct Postgres access would mean distributing DB credentials to every Lab-enabled site and opening the Postgres wire protocol broadly — a materially worse security posture than one more outbound HTTPS path reusing the same auth/API surface every other component already trusts. The agent polls `GET /lab-agent/jobs` and reports via `POST /lab-agent/jobs/{member_id}/status`, authenticated by a long-lived, cluster-scoped bearer token (`PveCluster.lab_agent_token_hash`, bcrypt-hashed at rest, shown once on generation — same hash-and-compare shape as the per-run appliance token, just cluster-scoped and long-lived instead of per-run and short-lived).

**Split of responsibility between RESURGAM's backend and the agent**: everything reachable through the PVE REST API stays in the backend, unchanged from the original design — zone/pool/VNet provisioning, the probe appliance's clone. Only the part that's inherently host-local moves to the agent: mapping a member's snapshot and attaching+booting it. The backend creates a `RestoreTestPlanMember` row in a `PENDING` state once its VNet/pool prerequisites are ready; the agent picks it up via `GET /lab-agent/jobs`, runs `proxmox-backup-client map` + builds the VM config (disk from the mapped device, `net0` at the run's VNet) + starts it, and reports status back — the backend never calls `qmrestore`/`live-restore` for Application Group members again.

**Deployment stays flexible, matching the rest of this project's component model**: `resurgam-lab-agent` is a new, separately-installed component (its own `[project.scripts]` entrypoint, mirroring `restore-worker`) — co-locatable on an all-in-one RESURGAM box that's also KVM-capable (which an all-in-one deployment already needs, since `restore-worker`'s own file-restore mechanism needs `/dev/kvm`), or installed directly on a customer's dedicated PVE node for a distributed deployment. Never bundled into the core "just run the backend" path — Recovery Lab stays fully opt-in, same as every privileged component in this project so far.

**A COW overlay, also live-proven end-to-end on 2026-09-19**: the earlier `ro=1` proof showed the mount mechanism worked but not that a guest could actually write during boot (journal replay, swap, temp files). A writable layer — `qemu-img create -f qcow2 -b <mapped-device> -F raw <overlay>` — was attached as `scsi0` instead and booted successfully; `qemu-img info` on the overlay showed its size growing (196KiB → 388KiB) while the mapped backing device was confirmed untouched, proving writes land only in the overlay. This surfaced one more real PVE constraint: `qm` accepts a bare `/dev/loopN` path for `scsi0` directly, but rejects a bare qcow2 *file* path outright ("unable to associate path ... to any storage") — the overlay must live under a PVE-registered "dir" storage (`pvesm add dir <id> --path <dir> --content images`) and be referenced with PVE's own image-naming convention, `<storage-id>:<vmid>/vm-<vmid>-disk-0.qcow2`. This is now a required, operator-provisioned prerequisite on every Lab-compute node (`PveCluster`-level, mirroring `lab_appliance_template_vmid`'s existing operator-setup precedent) — `resurgam-lab-agent` does not provision this storage itself, since storage management is a broader PVE privilege than the agent otherwise needs. `lab_agent.config.LabAgentConfig.overlay_storage_id` (env `RESURGAM_LAB_AGENT_OVERLAY_STORAGE_ID`, default `resurgam-lab-overlays`) carries this.

**Probe checks are a 3-tier escalating hierarchy, not a flat list of independent check types** (user guidance, 2026-09-19, important for real usability): Level 1 — guest agent reachable (cheapest, no network path needed); Level 2 — pingable (requires IP info, from the guest-agent report or the Lab VNet's DHCP lease); Level 3 — a scripted service-level test (e.g. nmap, PowerShell `Test-NetConnection`, `curl`), which is where "the restored backup actually works" gets proven, not just "the VM exists." Each tier gates the next — Level 2 can't run without Level 1's IP info, Level 3 implies network reachability already confirmed. `ApplicationGroupMember.check_types` and `RestoreTestPlanMember.probe_results` should model this as an ordered escalation, not an unordered bag; Level 3 should stay a small extensible set of built-in check kinds, not an arbitrary user-supplied script executor.

## Consequences

- `connectivity/pve_api.py`'s SDN/pool/clone functions (added this session, live-verified 2026-09-19: zone creation via `PUT /cluster/sdn/zones` + `PUT /cluster/sdn` apply, VNet creation, pool creation, a real linked-clone-unsupported finding on `usb-ssd` storage followed by a successful full clone with inline `pool` membership) remain exactly as built and stay in the backend's responsibility.
- `start_vm_restore`'s `live_restore`/`net0` params (added this session) are **not removed** — they're real, tested, correctly-behaving additions to `connectivity/pve_api.py` — but Recovery Lab no longer calls them with `live_restore=True`; that path is now understood to be unsuitable for this feature specifically, not deleted as dead code, since a future feature might still have a legitimate use for plain `live-restore` semantics.
- `restore_test/execution.py`'s member state machine gains agent-hand-off states (mount requested → mounted/attached → booting) between `RESTORE_REQUESTED` and `BOOTING`; `next_startable_member`'s ordering logic is unchanged, only what happens inside each state changes.
- A real, previously-undocumented PVE finding worth carrying forward: `proxmox-backup-client map`'s snapshot-path argument takes no namespace prefix (namespace is a separate `--ns` flag) — the first live attempt 400'd on a naively-prefixed path before this was corrected.
- `PveCluster.lab_appliance_template_vmid` (already added) stays as-is — the probe appliance is still cloned via the PVE API, unaffected by this change.
- **Frontend landed 2026-09-20**: `/recovery/recovery-lab` (previously a blocked nav placeholder) is now `RecoveryLabPage`, with two tabs on one page rather than a separate nav entry — Application Groups (define once per customer: name, members each picking a VM-only backup group, a startup order, and check types grouped by the 3-tier hierarchy above) and Lab Runs (create against a lab-enabled cluster/node/storage, then execute/poll/clean up, showing the one-time appliance run token and each member's live status and probe results). `PveClustersPage`'s cluster edit form gained the `is_lab_compute`/`lab_sdn_zone_name`/`lab_appliance_template_vmid` fields and a "generate/rotate" control for `lab_agent_token_hash`, shown once like the appliance run token.

## Addendum (2026-10-06): guided Lab Compute setup

Configuring lab compute through the bare `PveCluster.lab_*` fields proved impractical, so the PVE target
page gained a guided "Set up lab compute" flow backed by `GET /pve-clusters/{id}/lab-readiness`
(`restore_test/lab_readiness.py`), pickers (`/sdn-zones`, `/nodes/{node}/bridges`, `/storage-config`,
`/templates`), two audited auto-fixes and the agent install info (`api/v1/pve_lab_setup.py`). What it asks
for, reconciled with the code that actually runs a lab:

- **SDN zone**: optional. Empty means `_ensure_zone` creates RESURGAM's own Simple zone `resurgam` on the
  first run (needs `SDN.Allocate`); a configured zone is only ever read, never created or changed. Because
  `create_sdn_vnet` sends no `tag`, only `simple` zones work today -- PVE rejects an untagged VNet in
  VLAN/QinQ/VXLAN/EVPN zones (pve-network `VlanPlugin::vnet_update_hook`: "missing vlan tag"). The wizard
  therefore never creates zones and marks other zone types unusable.
- **Uplink bridge, snippet storage, probe appliance template**: picked from live lists. RESURGAM never creates
  bridges or templates. The template must sit on the lab node, since the clone is requested on the run's
  first member's node.
- **Overlay storage** (the agent's `RESURGAM_LAB_AGENT_OVERLAY_STORAGE_ID`): RESURGAM may create it on an
  explicit, confirmed admin click (`POST /storage`, type `dir`, path `/var/lib/resurgam-lab-agent/overlays`,
  content `images`, restricted to the lab nodes); it refuses if the id already exists. It may likewise add
  `snippets`/`images` to an existing storage's content (`PUT /storage/{id}` with the full existing list plus
  the new type and PVE's `digest`). Both need `Datastore.Allocate` on `/storage` and are audited.
- **Agent**: `PveCluster.lab_agent_last_seen_at` (migration 0054) is stamped whenever the agent authenticates
  (30-second resolution); the wizard polls readiness until it checks in.


## Addendum (2026-10-07): RESURGAM installs the agent, builds the template, configures the uplink

> **Superseded in part by [ADR 0027](0027-lab-node-pairing-and-agent-run-template-builds.md) (2026-10-11):** the
> recommended install is now pairing a node with a one-time code (no SSH login in RESURGAM), and after pairing the
> agent builds the templates itself; the agent also starts without a PBS token. The SSH install and SSH template
> build below remain as alternatives; the build code moved to `resurgam_worker.lab_build`.

The guided setup above still left three things by hand: building and installing `resurgam-lab-agent`, building the
probe appliance template, and an uplink that only worked untagged with DHCP. All three are now driven by RESURGAM
(`backend/src/resurgam_api/lab_setup/`, `api/v1/pve_lab_provisioning.py`, migration 0055).

- **Packages ship with the appliance.** `backend/Dockerfile` stage `lab-packages` builds both `.deb`s on
  `debian:trixie` into `/app/artifacts` (+ `manifest.json`), so every image and update bundle (ADR 0024) carries the
  versions matching its backend. `debian/changelog`'s upstream version must equal `VERSION` (restore-worker/README.md,
  "Versioning"). `GET /pve-clusters/lab-setup/downloads[/{artifact}[.sha256]]` serves them (platform admin).
- **One-time SSH, never stored.** Step 1 (`POST .../lab-setup/ssh/host-key`) connects *without authenticating* and
  returns the host key's OpenSSH SHA256 fingerprint; the admin compares it on the node. Step 2 re-sends the
  credentials with the confirmed fingerprint; paramiko checks the presented key against it before any credential is
  sent (no known_hosts, no auto-accept). Root or `sudo -n` is detected, neither is a clear `privilege_missing`.
  Credentials (and the generated agent token, and an optional PBS token) exist only in the job thread's memory; every
  step message/output is redacted against them; audit entries record node/host/port/auth method only.
- **Jobs, not one long request.** Install and template build run as in-memory jobs in the API process
  (`lab_setup/jobs.py`) and the wizard polls `GET .../lab-setup/jobs/{id}` for a live step log. The Postgres job queue
  was ruled out because its separate worker processes would need the credentials persisted; one long request would
  show nothing for minutes. Trade-off: jobs are lost on an API restart (single uvicorn process, as on the appliance).
- **Agent install** uploads the shipped `.deb`, checks its SHA-256 on the node, refuses a Debian release other than
  the build's (ADR 0020), `apt-get install`s it (`--force-confold`), writes `/etc/default/resurgam-lab-agent` (mode
  600, via SFTP, never on a command line) and restarts the service. The new agent token's hash is committed only after
  the file is written, so a failed install never locks out a working agent. Note: `GET /lab-agent/jobs` does not
  filter by node, so one agent per cluster remains the supported shape; installing on a second node rotates the token.
- **Template build** replaces the hand procedure (docs/recovery-lab-deploy-pipeline.md §2): Debian 13 genericcloud
  image downloaded on the node and SHA-512-checked (needs HTTPS to cloud.debian.org; offline: an image path or an
  `import` volume already on the node), a seed ISO (`genisoimage`) with the shipped prober `.deb` and a build script,
  `qm create` with `import-from`, cloud-init drive, serial console, `agent: 1` and the lab uplink NIC; first boot runs
  the build via `cicustom` user data and leaves a result file read through `qm guest exec`; then `cloud-init clean`,
  machine-id/host keys reset, build-only options removed, `resurgam-lab-prober-version: <v>` written into the VM
  description, `qm template`, and `lab_appliance_template_vmid` set. A failed build destroys only the VM it created.
  The readiness check compares that marker with the shipped prober version (`template_outdated` /
  `template_version_unknown`, both warnings). Manual mode renders the same commands as a script.
- **Uplink VLAN + IP.** `PveCluster.lab_appliance_uplink_vlan_tag` (1-4094, `tag=` on the netX device -- apidoc.js
  `net[n]`) and `lab_appliance_uplink_ip_mode` `dhcp` | `static_range` with range start/end, prefix, gateway and DNS
  (validated: same subnet, start <= end, gateway inside the subnet and outside the range, 1-3 DNS servers). Static
  addresses are allocated per run in `lab_uplink_ip_allocations` (unique per cluster + address, allocation locks the
  cluster row) at execute time -- before any PVE object is created -- and applied as cloud-init `ipconfig1` /
  `nameserver`; they are freed at cleanup once the appliance is gone (or when its clone fails). A group lab subnet that
  overlaps the static uplink subnet is refused. Readiness item `uplink_network` checks the settings and whether the
  bridge carries the tag (VLAN-aware Linux bridge incl. `bridge-vids`, OVS; a non-VLAN-aware Linux bridge is a warning
  because PVE then falls back to its "traditional" `vmbr0v<tag>` method -- pve-docs "VLAN for Guest Networks").

Not live-verified yet: the SSH flows against a real node, the in-guest build (cloud-init `runcmd` + seed ISO label,
`qm guest exec` result read-back, `cloud-init clean`), `genisoimage` being present on PVE 9 nodes, `qm set --delete
nameserver` on a VM that has it, and a VLAN-tagged / statically addressed appliance reaching the backend.
