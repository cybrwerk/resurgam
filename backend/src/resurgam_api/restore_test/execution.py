"""Recovery Lab (manifest EPIC 10 / §22, ADR 0019): pure orchestration
logic for a Lab run's Application Group members -- no PVE I/O here,
matching `restore/execution.py`'s style (fully unit-testable). The API
layer (`api/v1/restore_test_plans.py`) calls the real `connectivity.pve_api`
functions and uses these to decide what to call and when.
"""

import ipaddress
import secrets
from dataclasses import dataclass, field

import bcrypt

from resurgam_api.models.restore_test import RestoreTestPlanMemberStatus, RestoreTestPlanStatus

# The 3-tier probe hierarchy (ADR 0019, user guidance 2026-09-19): Level 1
# is always `guest_agent`, resolved backend-side via PVE's REST-proxied
# guest-agent management channel (`connectivity.pve_api.agent_ping`), not
# the probe appliance -- see `member_probe_plan` below. Everything else is
# Level 2 (`icmp`) or Level 3 (a scripted service check), delegated to the
# probe appliance (`resurgam_worker.lab_prober`) since those need genuine
# network reachability the backend doesn't have to the isolated Lab VNet.
GUEST_AGENT_CHECK_TYPE = "guest_agent"
NETWORK_TIER_CHECK_TYPES = frozenset(
    {"icmp", "arp", "tcp_port", "http", "https", "dns", "tls_handshake", "ssh_banner", "nmap_scan"}
)

# PVE SDN zone/vnet identifiers are real, hard-limited constraints
# (confirmed live against the real apidoc schema, 2026-09-19): 2-8 chars,
# must start with a letter, alphanumeric only (no hyphens/underscores).
_VNET_ID_LENGTH = 8

# One persistent pool per cluster holds every Lab VM across every run
# (visibility/bulk-permissioning, not isolation -- the per-run VNet is
# what isolates). Shared between `api/v1/restore_test_plans.py` (which
# creates/reuses the pool and clones the probe appliance into it inline)
# and `api/v1/lab_agent.py` (which hands this name to `resurgam-lab-agent`
# so member VMs join the same pool via `qm create --pool`, rather than a
# separate PVE-API pool-membership write after the fact).
DEFAULT_POOL_NAME = "resurgam-lab"


def generate_vnet_name() -> str:
    """A fresh, uniquely-named VNet id per Lab run (manifest §22.5) --
    `lab` plus enough hex to fill PVE's 8-char id limit. Collisions are
    astronomically unlikely (5 hex chars = 1M possibilities) and the
    caller's `create_sdn_vnet` call would simply fail and be retried with
    a new name if one ever occurred, so no uniqueness check is done here.
    """
    return "lab" + secrets.token_hex(3)[: _VNET_ID_LENGTH - 3]


def generate_appliance_run_token() -> str:
    """The per-run bearer token the probe appliance uses to authenticate
    its `POST /lab-runs/{id}/probe-results` callback -- generated once at
    run-execution time, returned to the caller exactly once (to embed in
    the appliance's cloud-init data), and only its hash is ever persisted
    (`RestoreTestPlan.appliance_run_token_hash`), hashed/verified with
    `bcrypt` -- the same hash-and-compare pattern already used for MFA
    recovery codes (`auth/mfa.py`), not a new one.
    """
    return secrets.token_urlsafe(32)


def hash_appliance_run_token(token: str) -> str:
    return bcrypt.hashpw(token.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_appliance_run_token(token: str, token_hash: str) -> bool:
    return bcrypt.checkpw(token.encode("utf-8"), token_hash.encode("utf-8"))


def zone_requires_single_node(zone_type: str | None) -> bool:
    """Whether every Application Group member in a run must land on the
    same PVE node. PVE's Simple zone type creates isolated bridges *per
    node*, with no tunnel/overlay between nodes -- confirmed from PVE's
    own zone-type semantics, but not yet live-verified against a real
    multi-node cluster in this environment (single-node at time of
    writing) -- treated as the safe assumption until disproven live.
    VLAN/VXLAN/EVPN/QinQ/Faucet zones can span the whole cluster.
    """
    return zone_type in (None, "simple")


def next_startable_member(members: list[dict]) -> dict | None:
    """Which member (by dict with `startup_order`/`member_status` keys)
    should begin restoring next, or `None` if none are eligible yet.

    Manifest §22.4's ordering: a member only starts once every member
    with a strictly lower `startup_order` has at least reached `BOOTING`
    (its restore completed and the guest was told to start) -- a
    deliberate simplification of "gated on the previous member's basic
    reachability" for this first pass: true reachability is only known
    once the appliance's async probe result arrives, which would make
    ordering depend on two independent async signals (PVE task status
    *and* the appliance's callback) instead of one. Gating on `BOOTING`
    keeps the whole state machine driven by `poll`'s PVE-status checks
    alone, while still guaranteeing strict sequential boot order, not
    simultaneous starts -- refining this to wait for confirmed
    reachability is a reasonable fast-follow once this base case is
    proven live.
    """
    pending = [m for m in members if m["member_status"] == RestoreTestPlanMemberStatus.PENDING.value]
    if not pending:
        return None
    candidate = min(pending, key=lambda m: m["startup_order"])
    blockers = [
        m
        for m in members
        if m["startup_order"] < candidate["startup_order"]
        and m["member_status"]
        not in (
            RestoreTestPlanMemberStatus.BOOTING.value,
            RestoreTestPlanMemberStatus.PROBING.value,
            RestoreTestPlanMemberStatus.PASSED.value,
            RestoreTestPlanMemberStatus.FAILED.value,
        )
    ]
    if blockers:
        return None
    return candidate


# `icmp` and `arp` are alternative proofs of the same underlying fact --
# the guest is up and answering on the isolated Lab VNet's network
# segment -- not two independent requirements. Raised directly by the
# user from a real run: ICMP echo blocked by the guest's own firewall
# (common, e.g. Windows Defender Firewall's default inbound rule) is a
# false-negative for "is this restore usable", not a real failure, once
# ARP already proves the guest is alive and answering on the wire.
# Every other check type (`guest_agent`, a scripted service check, etc.)
# still must pass individually -- only these two overlap in what they
# actually prove.
_NETWORK_LIVENESS_CHECK_TYPES = frozenset({"icmp", "arp"})


def probe_outcome_to_member_status(check_results: list[dict]) -> str:
    """A member's final status once probe results have arrived. `PASSED`
    requires every check to pass, with one exception: among `icmp`/`arp`
    results (see `_NETWORK_LIVENESS_CHECK_TYPES`), only one needs to
    pass, not both. No `PARTIAL` at the member level (manifest §22.2's
    `PARTIAL` is a *run*-level concept, for when some members pass and
    others fail -- see `aggregate_run_status`).
    """
    if not check_results:
        return RestoreTestPlanMemberStatus.FAILED.value

    liveness_results = [r for r in check_results if r.get("check_type") in _NETWORK_LIVENESS_CHECK_TYPES]
    other_results = [r for r in check_results if r.get("check_type") not in _NETWORK_LIVENESS_CHECK_TYPES]
    liveness_ok = not liveness_results or any(r.get("status") == "pass" for r in liveness_results)
    others_ok = all(r.get("status") == "pass" for r in other_results)

    if liveness_ok and others_ok:
        return RestoreTestPlanMemberStatus.PASSED.value
    return RestoreTestPlanMemberStatus.FAILED.value


_TERMINAL_MEMBER_STATUSES = (RestoreTestPlanMemberStatus.PASSED.value, RestoreTestPlanMemberStatus.FAILED.value)


def aggregate_run_status(member_statuses: list[str]) -> str:
    """The run's overall `status` derived from every member's current
    `member_status` -- manifest §22.2's `PLANNED`/running/terminal
    trichotomy (`PASSED`/`PARTIAL`/`FAILED`), not including `CLEANING_UP`/
    `COMPLETED`, which only ever come from an explicit `cleanup` call.
    """
    if all(s == RestoreTestPlanMemberStatus.PENDING.value for s in member_statuses):
        return RestoreTestPlanStatus.PLANNED.value
    if not all(s in _TERMINAL_MEMBER_STATUSES for s in member_statuses):
        return RestoreTestPlanStatus.RUNNING.value
    if all(s == RestoreTestPlanMemberStatus.PASSED.value for s in member_statuses):
        return RestoreTestPlanStatus.PASSED.value
    if all(s == RestoreTestPlanMemberStatus.FAILED.value for s in member_statuses):
        return RestoreTestPlanStatus.FAILED.value
    return RestoreTestPlanStatus.PARTIAL.value


@dataclass
class MemberProbePlan:
    """What's left to do for one `PROBING` member, computed fresh from its
    `check_types` + whatever `probe_results` have landed so far -- nothing
    stored beyond `probe_results` itself, matching this codebase's general
    "derive, don't cache" style for run-state decisions (`next_startable_member`,
    `aggregate_run_status`).
    """

    needs_guest_agent_check: bool = False
    guest_agent_failed: bool = False
    network_checks_pending: list[str] = field(default_factory=list)
    ready_for_appliance: bool = False
    is_complete: bool = False


def member_probe_plan(check_types: list[str], probe_results: list[dict], resolved_ip: str | None) -> MemberProbePlan:
    """Decides what's left for one member: whether Level 1 still needs
    resolving, whether Level 1 failed (which gates everything else off --
    "each tier gates the next", ADR 0019), which Level 2/3 check types
    still need a result, and whether it's ready to appear on the probe
    appliance's `GET /lab-runs/{id}/probe-targets` (resolved IP + at
    least one still-pending network-tier check + Level 1 not failed).

    IP resolution itself is not decided here -- it depends on
    `ApplicationGroupMember.expected_ip`/`agent_network_get_interfaces`,
    which needs a live PVE call the caller (`poll_restore_test_plan`)
    already has to make regardless of whether `guest_agent` is one of
    this member's *requested* check types (no other IP source exists
    when `expected_ip` is unset) -- `resolved_ip` is passed in already
    resolved or `None`.
    """
    done_types = {r["check_type"] for r in probe_results}

    needs_guest_agent_check = GUEST_AGENT_CHECK_TYPE in check_types and GUEST_AGENT_CHECK_TYPE not in done_types
    guest_agent_failed = any(
        r["check_type"] == GUEST_AGENT_CHECK_TYPE and r.get("status") != "pass" for r in probe_results
    )

    network_checks_pending = [t for t in check_types if t in NETWORK_TIER_CHECK_TYPES and t not in done_types]

    ready_for_appliance = (
        not guest_agent_failed
        and resolved_ip is not None
        and not needs_guest_agent_check
        and bool(network_checks_pending)
    )

    is_complete = set(check_types) <= done_types or guest_agent_failed

    return MemberProbePlan(
        needs_guest_agent_check=needs_guest_agent_check,
        guest_agent_failed=guest_agent_failed,
        network_checks_pending=network_checks_pending,
        ready_for_appliance=ready_for_appliance,
        is_complete=is_complete,
    )


def merge_probe_results(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Merges a member's `probe_results` by `check_type` -- both the
    backend (the `guest_agent` tier, via `poll_restore_test_plan`) and
    the probe appliance (`POST /lab-runs/{id}/probe-results`, Level 2/3)
    write into the same list now, so a later write must overlay by key
    rather than replace the whole list (the original Phase A design,
    before this member had two independent writers).
    """
    by_type = {r["check_type"]: r for r in existing}
    for entry in incoming:
        by_type[entry["check_type"]] = entry
    return list(by_type.values())


def render_appliance_cloud_init_snippet(
    *,
    backend_url: str,
    plan_id,
    run_token: str,
    gateway_ip: str,
    subnet_cidr: str,
    dns_ip: str | None,
    dhcp_range_start: str | None,
    dhcp_range_end: str | None,
) -> str:
    """The probe appliance's per-run cloud-init user-data -- written by
    `resurgam-lab-agent` to the cluster's local snippet storage path (no
    PVE REST endpoint exists to upload one, confirmed live 2026-09-21).
    Writes two plain env files the appliance's own systemd units read:
    the prober's backend/run credentials, and the router bootstrap's own
    gateway/DHCP/DNS config (its own net0 subnet -- it acts as
    gateway/DHCP/firewall/SNAT for that segment itself, PVE never
    provisions a subnet on the Lab VNet -- see the module docstring
    above and ADR 0019).
    """
    dns = dns_ip or gateway_ip
    lines = [
        "#cloud-config",
        "write_files:",
        "  - path: /etc/default/resurgam-lab-prober",
        "    content: |",
        f"      RESURGAM_BACKEND_URL={backend_url}",
        f"      RESURGAM_LAB_RUN_ID={plan_id}",
        f"      RESURGAM_LAB_RUN_TOKEN={run_token}",
        "  - path: /etc/default/resurgam-lab-router",
        "    content: |",
        f"      GATEWAY_IP={gateway_ip}",
        f"      SUBNET_CIDR={subnet_cidr}",
        f"      DNS_IP={dns}",
        f"      DHCP_RANGE_START={dhcp_range_start or ''}",
        f"      DHCP_RANGE_END={dhcp_range_end or ''}",
        "runcmd:",
        "  - systemctl enable --now resurgam-lab-router.service",
        "  - systemctl enable --now resurgam-lab-prober.service",
    ]
    return "\n".join(lines) + "\n"


def subnet_prefix_length(subnet_cidr: str) -> int:
    """`10.99.0.0/24` -> `24` -- for building PVE's `ipconfig0=ip=<ip>/<prefix>`
    syntax from the Application Group's own subnet CIDR.
    """
    return ipaddress.ip_network(subnet_cidr, strict=False).prefixlen
