# 0020 — Recovery Lab, Phase B: 3-tier probing via a self-contained probe appliance

Status: Accepted (2026-09-22)

## Context

ADR 0019 (Phase A) proved the zero-copy map-and-boot mechanism end-to-end but left every
Application Group member sitting in `PROBING` forever — nothing implemented the 3-tier check
hierarchy the schema already anticipated (`RestoreTestPlanMember.probe_results`,
`ApplicationGroupMember.check_types`, `RestoreTestPlan.appliance_run_token_hash`,
`PveCluster.lab_appliance_template_vmid`). This ADR covers everything built and learned closing
that gap, all live-verified against real infrastructure (the same `pve1` PVE node and PBS instance
ADR 0019 used) on 2026-09-21/22 — a real Windows production VM ("Office-PC") restored,
booted correctly, and had real Level 1–3 checks run against it and pass/fail correctly.

### PVE stays out of the lab's network data path (a direct user correction)

An earlier draft of this design had RESURGAM's own SDN provisioning a subnet, gateway, DHCP
server, and SNAT rule directly on the run's isolated VNet — rejected outright: *"No sane engineer
would like the PVE abuse like this... there are too many security principles not respected."*
The corrected design: PVE's only role in the Lab VNet is exactly what Phase A already had it do —
create an isolated Simple-zone VNet, a pure L2 bridge, nothing more. **The probe appliance VM
itself is the router, DHCP server, firewall, and SNAT gateway** for that isolated segment, entirely
self-contained inside the appliance guest. This is a real security boundary: the PVE host never
becomes part of the lab's data path, never runs NAT/DHCP/firewall services for traffic originating
from restored (potentially compromised) backup VMs.

This makes the appliance **dual-homed**: `net0` on the isolated Lab VNet (gateway/DHCP/DNS/NAT for
restored members), `net1` on an existing, already-connected PVE bridge (operator-chosen, e.g.
`vmbr0`, confirmed live to need its own VLAN tag when the bridge is an OVS trunk — `vmbr0,tag=5` in
this environment) purely as its own uplink to reach the RESURGAM backend — a classic jump-box
shape. `PveCluster` gained `lab_appliance_uplink_bridge` for this and `lab_snippet_storage_id` (see
next section) as new operator-provisioned prerequisites, following `lab_appliance_template_vmid`'s
existing precedent.

### No PVE REST endpoint uploads a cloud-init snippet file

The appliance needs its per-run backend URL/token and router config (gateway/subnet/DHCP range)
delivered somehow at clone time. The obvious mechanism — `cicustom` pointing at an uploaded
snippet — hit a real wall: `POST /nodes/{node}/storage/{storage}/upload`'s `content` parameter
schema is a hard `enum: iso, vztmpl, import` on this PVE version; `snippets` is rejected outright
even after the target storage's own content-type list was updated to include it (confirmed live,
`pvesh create ... --content snippets` still 400s).

**`resurgam-lab-agent` writes the snippet file directly** to the cluster's local snippet-storage
path instead (`pvesm path <storage>:snippets/<file>` resolves the real filesystem path even before
the file exists) — host-local work, matching ADR 0019's own "inherently host-local work belongs to
the agent" principle. This makes appliance provisioning a second, agent-driven job type
(`GET /lab-agent/appliance-jobs`, `POST /lab-agent/appliance-jobs/{plan_id}/write-snippet`),
processed before the deferred `clone_vm` + dual-NIC `qm set` the backend performs once the agent
confirms the write. `RestoreTestPlan` gained `PROVISIONING_APPLIANCE`, a state between `PLANNED`
and `RUNNING` for exactly this window.

Two real ordering bugs surfaced building this hand-off, both fixed:
- `clone_vm`'s returned UPID must be polled to completion (`get_task_status`) before the follow-up
  `qm set` — PVE holds a clone lock on the target VM until the (multi-GB) copy actually finishes;
  configuring immediately after firing the clone hit a real `VM is locked (clone)` 500.
- The clone was correctly configured but never actually **started** — nothing in the original flow
  called `start_guest`. Found live: the appliance sat `stopped` after a fully successful
  clone+configure.
- A plan `FAILED` at the appliance-provisioning level (infrastructure, independent of any member)
  was silently overwritten back to a member-derived status the moment the next member callback
  arrived, since both `report_lab_agent_job_status` and `poll` unconditionally recomputed
  `plan.status` from `aggregate_run_status`. Both call sites now skip that recompute once a plan is
  already `FAILED`.

### The lab VM's config must come from the backup itself, not be synthesized

The first working boot (a Linux guest) used a from-scratch VM config (`memory`/`cores`/`net0`/
`scsi0` only, hardcoding `scsi0` as the disk slot). It happened to work for that guest — but the
first real Windows restore fell straight into Windows's repair/recovery mode, confirmed via the
real console. Root cause, found by diffing against the real original config
(`proxmox-backup-client restore <snapshot> qemu-server.conf.blob -` — every PBS VM backup carries
its own real `qm config`): the synthetic config had silently dropped `machine` type
(`pc-i440fx-10.0+pve1`), the guest's *real* disk controller (this guest used `sata0`, not the
hardcoded `scsi0` — a completely different, wrong controller), NIC model (`e1000`, not `virtio`),
`ostype`, real socket/core/memory counts, and the `smbios1` UUID. Windows is far more sensitive to
this class of boot-environment mismatch than the Linux guest the synthetic config happened to boot.

**`resurgam-lab-agent` now reconstructs the lab VM from the original config wholesale**, remapping
only two things: the disk slot the mapped archive actually belongs to (derived from the archive's
own filename, `drive-<slot>.img.fidx`) now points at the local COW overlay instead of the original
production storage reference, and `net0`'s `bridge=` now points at the run's isolated Lab VNet
(NIC model/MAC preserved for fidelity; `firewall=1`, if present, is dropped — no rules exist for an
ad-hoc Lab VNet, so leaving it enabled risks silently dropping the DHCP/probe traffic the whole
feature depends on). Every other original disk slot and NIC (`net1`, `net2`, ...) is dropped —
Phase A only ever maps one archive, and passing the rest through would reference production storage
IDs the Lab cluster doesn't (or, worse, coincidentally does) have.

One more real gap in the same pass: not every key a real backup's config carries is a valid
`qm create` CLI option. `meta` (creation-time housekeeping info) isn't a top-level `create` option
at all (`qm help create` confirms it only exists as an unrelated `--cicustom` sub-key) — passing it
through made `qm create` fail outright (`Unknown option: meta`) before a VM was ever created.
`lock`/`digest`/`parent` are the same class of informational/computed-state field, stripped
defensively even though only `meta` has actually been observed live.

### The 3-tier hierarchy, split by what's actually reachable from where

Level 1 (`guest_agent`) and IP resolution are always backend-driven — both are PVE-REST-proxied
guest-agent management-channel calls (`POST .../agent/ping`, `GET .../agent/network-get-interfaces`
— confirmed live that `ping` is a POST, not GET, contrary to an earlier assumption), not
network-path operations, so they stay legitimately backend-driven even though Level 2/3 are
delegated to the appliance. IP resolution priority: `ApplicationGroupMember.expected_ip` (static)
if set, else the guest agent's own reported address (no other source exists) — confirmed live this
resolves a real DHCP lease the appliance's own dnsmasq handed the restored guest.

Level 2/3 run from the probe appliance, over the isolated segment it actually has reachability to,
polling `GET /lab-runs/{id}/probe-targets` and posting to the existing
`POST /lab-runs/{id}/probe-results` (now **merged by `check_type`**, not replaced wholesale, since
both the backend (`guest_agent`) and the appliance (everything else) write into the same
`probe_results` list — a member's status only finalizes once every requested check type has a
result, gated by `member_probe_plan`: a `guest_agent` failure short-circuits everything else
immediately, matching "each tier gates the next."

A member with a target IP outside the appliance's own configured subnet (a baked-in static
production IP) gets a secondary `/32` alias added on the appliance's own `net0` first — purely a
self-contained change to the appliance's own interface, no PVE/host involvement.

### Two more check types, added after a real, decisive finding

The first fully successful Windows run exposed a real limitation: `icmp` failed outright (Windows
Firewall's default profile blocks ICMP echo while still allowing RDP through — confirmed this is
exactly this guest's real configuration), but `ip neigh` showed the guest had answered ARP the
entire time. ARP resolution happens in the kernel below any userspace firewall, making it a
materially more reliable "is this host actually up" signal than ICMP for this common case.

- **`arp`** (Level 2): triggers ARP resolution via `ping` (its own ICMP result is ignored, used only
  as a trigger — confirmed live that a `ping` reporting 100% loss still leaves a fresh, correctly
  resolved `ip neigh` entry immediately after) then reads the resulting state. No new `arping`
  binary needed.
- **`nmap_scan`** (Level 3): a bounded top-100-ports `nmap` scan (never an operator-suppliable range
  or script, keeping with manifest §22.6's "no arbitrary scripts" philosophy), reporting real
  open-port evidence in the result detail. Confirmed live against the real Windows guest: correctly
  found `3389/tcp open (ms-wbt-server)` — the guest's actual, known-open RDP port.

## Decision

Phase B is built and live-verified as described above. Summary of the concrete shape:

- `ApplicationGroup` carries the probe appliance's own router config (`lab_network_mode`,
  `lab_subnet_cidr`, `lab_gateway_ip`, `lab_dns_ip`, `lab_dhcp_range_start/end`) — configured once
  per group, not per run. `ApplicationGroupMember` carries `expected_ip` (static IP override).
- `PveCluster` carries `lab_snippet_storage_id` and `lab_appliance_uplink_bridge` — both required
  before a run's appliance can ever be provisioned or call home.
- `RestoreTestPlan` carries `appliance_snippet_pending` and a `PROVISIONING_APPLIANCE` status for
  the agent-driven snippet hand-off window.
- `RestoreTestPlanMember` carries `resolved_ip`, persisted so `probe-targets` doesn't need a live
  PVE round-trip per appliance poll.
- New backend connectivity functions: `agent_ping`, `agent_network_get_interfaces`,
  `configure_lab_appliance_network`, `get_task_status` (already existed, now used for the clone
  wait) — all in `connectivity/pve_api.py`.
- New backend endpoints: `GET /lab-agent/appliance-jobs`, `POST
  /lab-agent/appliance-jobs/{plan_id}/write-snippet`, `GET /lab-runs/{id}/probe-targets`.
- New component **`resurgam-lab-prober`**, packaged from the same `resurgam-worker` source tree as
  `resurgam-lab-agent` (`Source: resurgam-worker`, two binary packages, each its own
  `dh-virtualenv`). Runs on the cloned appliance: a router/DHCP/firewall bootstrap
  (`resurgam-lab-router.service`, a shell script — `dnsmasq` + `nftables`, interface discovered by
  matching the guest's own assigned `GATEWAY_IP` rather than assuming a fixed name, since PVE's
  `net0` config-slot label isn't the guest's own predictable interface name) and the poll-loop
  daemon (`resurgam-lab-prober.service`) implementing Level 2/3 checks in pure-stdlib Python plus
  `nmap`.
- `resurgam-lab-agent` gained the snippet-write responsibility and the config-reconstruction fix
  (`pbs_client.map.fetch_original_vm_config`, `lab_agent.daemon._build_member_vm_config`,
  `pve_client.qm.create_vm` now takes a full config dict rather than a fixed parameter set).

## Consequences

- **A real cross-Debian-version packaging lesson, distinct from ADR 0011's architecture/build-host
  lesson**: `dh_virtualenv`'s venv is not truly portable across Debian releases with different
  default Python versions — `bin/python3` is a symlink to the *target* machine's own system
  interpreter, not a bundled copy, so a venv built on trixie (Python 3.13) silently fails to import
  its own site-packages when installed on bookworm (Python 3.11). The golden appliance template
  must be rebuilt on the same Debian release as wherever `resurgam-lab-agent`'s own build host is —
  the original template (bookworm) was rebuilt as trixie (VMID 9000 → 9002 → 9003 across this work,
  9003 is current) for exactly this reason. This is now a hard constraint on Recovery Lab's own
  build pipeline, addressed by a proposed safe deploy pipeline (see `docs/recovery-lab-deploy-pipeline.md`).
- A second real packaging finding: `dh_installsystemd --name=X` looks for
  `debian/<package>.X.service`, not `debian/X.service` — got both this and a redundant `--name` on
  each package's own default-matching unit wrong on the first attempt, which silently installed
  *no* systemd units into *either* package. `debian/rules`' `override_dh_installsystemd` documents
  the correct convention now.
- `qemu-guest-agent` is not installed by default on either Debian cloud image used this session —
  a required, operator-documented prerequisite for the golden appliance template, alongside
  `dnsmasq`/`nftables`/`nmap` (Recovery Lab already required `resurgam-lab-agent` itself to set
  `agent: 1` on every member VM it creates, ADR 0019 addendum, 2026-09-21 — this is the appliance's
  own equivalent gap, one layer up).
- The default check-port convention (`ssh_banner`/`tcp_port`→22, `http`→80, `https`/`tls_handshake`→443,
  `dns`→53) stays fixed, no per-check parameter added (user's explicit choice) — `arp` and
  `nmap_scan` need no port at all, sidestepping the question for the two checks added this round.
- `cleanup_restore_test_plan` still does not destroy member/appliance VMs (documented Phase A gap,
  unchanged) — every live run this session needed manual `qm destroy` afterward. A real, recurring
  operational cost during this work, not yet addressed; a natural next Phase C candidate.
