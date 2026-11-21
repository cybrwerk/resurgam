"""Recovery Lab compute readiness: an ordered checklist of everything a PVE
cluster needs before a Lab run on it can succeed (ADR 0019/0020), as used
by `GET /pve-clusters/{id}/lab-readiness` and the Lab Compute setup
wizard.

Each check is isolated: a PVE call that fails turns only the checks that
depend on it into `unknown` (with PVE's error in `details.error`), every
other check still runs. Nothing here writes to PVE or to the database.

What the checks encode (and why), from the code that actually runs a lab:

- SDN zone: `restore_test_plans._ensure_zone` uses `lab_sdn_zone_name` if
  set (never creates or modifies it), else creates RESURGAM's own default
  Simple zone `resurgam` on first use. `create_sdn_vnet` then creates a
  VNet *without* a `tag` -- PVE rejects that for VLAN/QinQ/VXLAN/EVPN zones
  (pve-network `Zones/VlanPlugin.pm` `vnet_update_hook`: "missing vlan
  tag"; the VNet `tag` parameter is "VLAN Tag (for VLAN or QinQ zones) or
  VXLAN VNI (for VXLAN or EVPN zones)", apidoc.js `POST /cluster/sdn/vnets`).
  So only a `simple` zone works with today's lab code.
- Overlay storage: `resurgam-lab-agent` writes each member's COW overlay
  to `<RESURGAM_LAB_AGENT_OVERLAY_DIR>/images/<vmid>/` and references it as
  `<RESURGAM_LAB_AGENT_OVERLAY_STORAGE_ID>:<vmid>/...` -- a PVE "dir"
  storage with content `images` whose path is that directory must exist on
  every lab node (ADR 0019).
- Probe appliance template: `lab_agent.confirm_appliance_snippet_written`
  sends the clone to the *first member's target node*
  (`POST /nodes/{that node}/qemu/{template}/clone`); PVE can only clone a
  guest on the node that holds its config, so the template must sit on the
  lab node runs target.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from resurgam_api.config import Settings
from resurgam_api.connectivity.models import TrustConfig
from resurgam_api.connectivity.pve_api import get_own_permissions, get_sdn_zone, get_vm_config, list_nodes
from resurgam_api.connectivity.pve_lab_api import (
    SNIPPET_CAPABLE_STORAGE_TYPES,
    GuestResource,
    StorageConfig,
    list_guest_resources,
    list_node_bridges,
    list_storage_config,
    vlan_ids_include,
)
from resurgam_api.connectivity.required_privileges import RequiredPrivilege, missing_privileges
from resurgam_api.lab_setup.artifacts import LAB_PROBER_ARTIFACT, try_load_manifest
from resurgam_api.lab_setup.template_build import parse_prober_version
from resurgam_api.lab_setup.uplink import (
    overlapping_lab_subnets,
    uplink_config_from_cluster,
    uplink_usage,
    validate_uplink_config,
)
from resurgam_api.models.pve import PveCluster

# RESURGAM's own default Lab zone, created on first use when the cluster has
# no `lab_sdn_zone_name` -- must stay in sync with
# `api.v1.restore_test_plans.DEFAULT_ZONE_NAME` (imported there from here).
DEFAULT_LAB_ZONE_NAME = "resurgam"

# `resurgam-lab-agent`'s defaults (restore-worker
# `lab_agent/config.py`: `RESURGAM_LAB_AGENT_OVERLAY_STORAGE_ID`,
# `RESURGAM_LAB_AGENT_OVERLAY_DIR`; the .deb creates the directory via
# `debian/resurgam-lab-agent.dirs`).
DEFAULT_OVERLAY_STORAGE_ID = "resurgam-lab-overlays"
DEFAULT_OVERLAY_DIR = "/var/lib/resurgam-lab-agent/overlays"

# The agent polls every 5 s by default; a check-in older than this means it
# is stopped, can't reach the backend, or uses an outdated token.
LAB_AGENT_RECENT_WINDOW = timedelta(minutes=5)

# Zone types today's lab code can create an (untagged) VNet in -- see the
# module docstring.
LAB_USABLE_ZONE_TYPES = frozenset({"simple"})

# The subset of `PVE_REQUIRED_PRIVILEGES` a Lab run (and this wizard's
# auto-fixes) needs. Paths/reasons per PVE apidoc.js permission checks:
# clone (`VM.Clone` on the template, `VM.Allocate` + `Datastore.AllocateSpace`
# on the target, `Pool.Allocate` for inline pool membership), dual-NIC +
# cloud-init config (`VM.Config.Network` covers `net*`, `ipconfig*` and
# `cicustom` -- qemu-server `check_vm_modify_config_perm`), start/stop
# (`VM.PowerMgmt`), level-1 checks (`VM.GuestAgent.Audit`), zone/VNet
# (`SDN.Allocate`), attaching to VNet/uplink (`SDN.Use`), storage.cfg
# writes for the auto-fixes (`Datastore.Allocate` on `/storage`).
LAB_REQUIRED_PRIVILEGES: tuple[RequiredPrivilege, ...] = (
    RequiredPrivilege("Sys.Audit", "/", "List nodes and read task status of clones."),
    RequiredPrivilege("VM.Audit", "/vms", "List guests and find the probe appliance template."),
    RequiredPrivilege("VM.Clone", "/vms", "Clone the probe appliance template for each run."),
    RequiredPrivilege("VM.Allocate", "/vms", "Create the appliance clone and remove lab VMs afterwards."),
    RequiredPrivilege("VM.Config.Network", "/vms", "Wire the appliance's two NICs and its cloud-init snippet."),
    RequiredPrivilege("VM.PowerMgmt", "/vms", "Start and stop the appliance and lab VMs."),
    RequiredPrivilege("VM.GuestAgent.Audit", "/vms", "Level-1 checks: guest-agent ping and guest IP addresses."),
    RequiredPrivilege("SDN.Allocate", "/sdn", "Create the isolated per-run VNet (and the default zone)."),
    RequiredPrivilege("SDN.Use", "/sdn", "Attach the appliance and lab VMs to the VNet and the uplink bridge."),
    RequiredPrivilege("Pool.Allocate", "/pool", "Create the 'resurgam-lab' pool that groups lab VMs."),
    RequiredPrivilege("Pool.Audit", "/pool", "Confirm pool membership before removing a lab VM."),
    RequiredPrivilege("Datastore.AllocateSpace", "/storage", "Allocate the appliance clone's disk."),
    RequiredPrivilege(
        "Datastore.Allocate", "/storage", "Wizard auto-fixes: create the overlay storage, add 'snippets' content."
    ),
)

CHECK_KEYS = (
    "privileges",
    "sdn_zone",
    "uplink_bridge",
    "uplink_network",
    "snippet_storage",
    "overlay_storage",
    "appliance_template",
    "lab_agent",
    "backend_url",
)


@dataclass
class ReadinessItem:
    key: str
    status: str  # ok | missing | warning | unknown
    code: str
    message: str
    fix_hint: str | None = None
    auto_fix: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReadinessResult:
    lab_nodes: list[str]
    lab_nodes_source: str
    overlay_storage_id: str
    checked_at: datetime
    items: list[ReadinessItem]

    @property
    def ready(self) -> bool:
        return all(item.status in ("ok", "warning") for item in self.items)


class _Lazy:
    """Memoizes one PVE call -- including its failure, so a shared call
    (e.g. `GET /storage`, used by two checks) is made once and a failure
    marks every dependent check `unknown` without retrying."""

    def __init__(self, fn: Callable[[], Any]):
        self._fn = fn
        self._done = False
        self._value: Any = None
        self._error: Exception | None = None

    def get(self) -> Any:
        if not self._done:
            try:
                self._value = self._fn()
            except Exception as exc:  # noqa: BLE001 -- any failure means "unknown", reported verbatim
                self._error = exc
            self._done = True
        if self._error is not None:
            raise self._error
        return self._value


def _unknown(key: str, exc: Exception) -> ReadinessItem:
    return ReadinessItem(
        key=key,
        status="unknown",
        code="pve_error",
        message=f"Could not check this: {exc}",
        fix_hint="Make sure the cluster is reachable (Test connection) and try again.",
        details={"error": str(exc)},
    )


def _not_on_nodes(nodes: list[str] | None, lab_nodes: list[str]) -> list[str]:
    """Lab nodes a storage/zone `nodes` restriction leaves out (`None` = all nodes)."""
    if nodes is None:
        return []
    return [n for n in lab_nodes if n not in nodes]


def _check_privileges(perms: _Lazy) -> ReadinessItem:
    result = perms.get()
    missing = missing_privileges(result.privileges, LAB_REQUIRED_PRIVILEGES)
    if missing:
        return ReadinessItem(
            key="privileges",
            status="missing",
            code="privileges_missing",
            message=f"RESURGAM's PVE token lacks {len(missing)} privilege(s) the lab needs: {', '.join(missing)}.",
            fix_hint="Add the missing privileges to the token's role (e.g. `pveum role modify RESURGAM --privs "
            "'...' --append 1`), or reconnect with credentials so RESURGAM updates its role itself.",
            details={"missing": missing, "required": [p.name for p in LAB_REQUIRED_PRIVILEGES]},
        )
    return ReadinessItem(
        key="privileges",
        status="ok",
        code="all_granted",
        message="The token holds every privilege the lab needs.",
        details={"required": [p.name for p in LAB_REQUIRED_PRIVILEGES]},
    )


def _check_sdn_zone(cluster: PveCluster, get_zone: Callable[[str], dict | None], lab_nodes: list[str]):
    configured = cluster.lab_sdn_zone_name
    zone_name = configured or DEFAULT_LAB_ZONE_NAME
    zone = get_zone(zone_name)
    if zone is None:
        if configured:
            return ReadinessItem(
                key="sdn_zone",
                status="missing",
                code="zone_not_found",
                message=f"The configured SDN zone '{configured}' does not exist on this cluster.",
                fix_hint="Pick an existing Simple zone, or clear the field so RESURGAM creates its own "
                f"'{DEFAULT_LAB_ZONE_NAME}' Simple zone on the first run.",
                details={"zone": configured},
            )
        return ReadinessItem(
            key="sdn_zone",
            status="ok",
            code="default_zone_auto_created",
            message=f"No zone configured: RESURGAM creates its own Simple zone '{DEFAULT_LAB_ZONE_NAME}' on the "
            "first lab run (needs SDN.Allocate, see privileges).",
            details={"zone": DEFAULT_LAB_ZONE_NAME, "auto": True},
        )

    zone_type = zone.get("type")
    details = {"zone": zone_name, "type": zone_type, "auto": not configured}
    if zone_type not in LAB_USABLE_ZONE_TYPES:
        return ReadinessItem(
            key="sdn_zone",
            status="missing",
            code="zone_type_unsupported",
            message=f"Zone '{zone_name}' is of type '{zone_type}'. Lab runs create an untagged VNet per run, which "
            "PVE only accepts in a Simple zone (VLAN/QinQ/VXLAN/EVPN VNets need a tag).",
            fix_hint="Pick a Simple zone, or clear the field to use RESURGAM's own Simple zone.",
            details=details,
        )
    zone_nodes = zone.get("nodes")
    zone_node_list = [n.strip() for n in zone_nodes.split(",") if n.strip()] if isinstance(zone_nodes, str) else None
    outside = _not_on_nodes(zone_node_list, lab_nodes)
    if outside:
        return ReadinessItem(
            key="sdn_zone",
            status="missing",
            code="zone_not_on_nodes",
            message=f"Zone '{zone_name}' is restricted to nodes {zone_node_list}; lab node(s) {outside} are not "
            "included, so the per-run VNet would not exist there.",
            fix_hint="Add the lab nodes to the zone's node list in PVE (Datacenter > SDN > Zones), then apply.",
            details={**details, "missing_nodes": outside},
        )
    if zone.get("state"):
        return ReadinessItem(
            key="sdn_zone",
            status="warning",
            code="zone_pending_apply",
            message=f"Zone '{zone_name}' has SDN changes that are not applied yet.",
            fix_hint="Apply the pending SDN configuration in PVE (Datacenter > SDN > Apply).",
            details=details,
        )
    return ReadinessItem(
        key="sdn_zone",
        status="ok",
        code="zone_ok",
        message=f"Zone '{zone_name}' ({zone_type}) is usable for isolated lab VNets.",
        details=details,
    )


def _check_uplink_bridge(cluster: PveCluster, bridges_for: Callable[[str], list], lab_nodes: list[str]):
    bridge = cluster.lab_appliance_uplink_bridge
    if not bridge:
        return ReadinessItem(
            key="uplink_bridge",
            status="missing",
            code="not_configured",
            message="No uplink bridge chosen for the probe appliance's second NIC.",
            fix_hint="Pick an existing bridge (e.g. vmbr0) from which the appliance can reach the RESURGAM backend.",
        )
    missing_on: list[str] = []
    inactive_on: list[str] = []
    for node in lab_nodes:
        found = next((b for b in bridges_for(node) if b.iface == bridge), None)
        if found is None:
            missing_on.append(node)
        elif found.active is False:
            inactive_on.append(node)
    details = {"bridge": bridge, "nodes": lab_nodes}
    if missing_on:
        return ReadinessItem(
            key="uplink_bridge",
            status="missing",
            code="bridge_not_on_nodes",
            message=f"Bridge '{bridge}' does not exist on lab node(s) {missing_on}.",
            fix_hint="Pick a bridge that exists on every lab node, or create it there (System > Network).",
            details={**details, "missing_nodes": missing_on},
        )
    if inactive_on:
        return ReadinessItem(
            key="uplink_bridge",
            status="warning",
            code="bridge_inactive",
            message=f"Bridge '{bridge}' exists but is not active on node(s) {inactive_on}.",
            fix_hint="Activate the bridge in PVE (System > Network > Apply Configuration).",
            details={**details, "inactive_nodes": inactive_on},
        )
    return ReadinessItem(
        key="uplink_bridge",
        status="ok",
        code="bridge_ok",
        message=f"Bridge '{bridge}' exists on every lab node.",
        details=details,
    )


def _check_uplink_network(
    cluster: PveCluster,
    bridges_for: Callable[[str], list],
    lab_nodes: list[str],
    usage: Callable[[], int] | None,
    lab_subnets: Callable[[], list[tuple[str, str]]] | None,
) -> ReadinessItem:
    """VLAN tag + IPv4 mode of the appliance's uplink NIC. With a tag, the
    uplink bridge must carry that VLAN: PVE documents three ways (pve-docs
    "VLAN for Guest Networks"): a VLAN-aware Linux bridge (tag must be in
    its `bridge-vids`), an OVS bridge, or -- on a Linux bridge that is not
    VLAN-aware -- the "traditional" method, where PVE creates
    `<port>.<tag>` + `vmbr0v<tag>` on first use; that only works when the
    bridge has a physical port carrying the VLAN, so it is a warning."""
    cfg = uplink_config_from_cluster(cluster)
    problems = validate_uplink_config(cfg)
    details: dict[str, Any] = {
        "bridge": cfg.bridge,
        "vlan_tag": cfg.vlan_tag,
        "ip_mode": cfg.ip_mode,
    }
    if problems:
        return ReadinessItem(
            key="uplink_network",
            status="missing",
            code="invalid_settings",
            message="The uplink network settings are inconsistent: " + "; ".join(p.message for p in problems),
            fix_hint="Correct them in the network step (VLAN tag / static range).",
            details={**details, "errors": [{"field": p.field, "message": p.message} for p in problems]},
        )
    if cfg.is_static:
        details.update(
            range_start=cfg.range_start,
            range_end=cfg.range_end,
            prefix_length=cfg.prefix_length,
            gateway=cfg.gateway,
            dns_servers=", ".join(cfg.dns_servers),
            range_size=cfg.range_size(),
        )

    if cfg.vlan_tag is not None and cfg.bridge:
        not_carried: list[str] = []
        traditional: list[str] = []
        for node in lab_nodes:
            found = next((b for b in bridges_for(node) if b.iface == cfg.bridge), None)
            if found is None:
                continue  # reported by the uplink_bridge item
            if found.type == "OVSBridge":
                continue
            if found.vlan_aware:
                if vlan_ids_include(found.vlan_ids, cfg.vlan_tag) is False:
                    not_carried.append(node)
            else:
                traditional.append(node)
        if not_carried:
            return ReadinessItem(
                key="uplink_network",
                status="missing",
                code="vlan_not_allowed",
                message=f"Bridge '{cfg.bridge}' is VLAN-aware, but VLAN {cfg.vlan_tag} is not in its allowed VLANs "
                f"(bridge-vids) on node(s) {not_carried}.",
                fix_hint=f"Add {cfg.vlan_tag} to the bridge's VLAN IDs in PVE (System > Network > {cfg.bridge}), or "
                "use another tag.",
                details={**details, "nodes": not_carried},
            )
        if traditional:
            return ReadinessItem(
                key="uplink_network",
                status="warning",
                code="bridge_not_vlan_aware",
                message=f"Bridge '{cfg.bridge}' is not VLAN-aware on node(s) {traditional}. PVE then uses its "
                f"'traditional' VLAN method (creates <port>.{cfg.vlan_tag} and {cfg.bridge}v{cfg.vlan_tag}), which "
                "only works if the bridge has a physical port that carries the VLAN.",
                fix_hint=f"Prefer ticking 'VLAN aware' on {cfg.bridge} in PVE (System > Network), or confirm the port "
                f"carries VLAN {cfg.vlan_tag}.",
                details={**details, "nodes": traditional},
            )

    if cfg.is_static:
        if lab_subnets is not None:
            overlaps = overlapping_lab_subnets(cfg, lab_subnets())
            if overlaps:
                names = ", ".join(f"{name} ({cidr})" for name, cidr in overlaps)
                return ReadinessItem(
                    key="uplink_network",
                    status="warning",
                    code="overlaps_lab_subnet",
                    message=f"The uplink subnet {cfg.network()} overlaps the lab network of Application Group(s) "
                    f"{names}; runs of those groups are refused.",
                    fix_hint="Use a different uplink range or change those groups' lab subnet.",
                    details={**details, "overlaps": names},
                )
        if usage is not None:
            used = usage()
            details["in_use"] = used
            if used >= cfg.range_size():
                return ReadinessItem(
                    key="uplink_network",
                    status="warning",
                    code="range_exhausted",
                    message=f"All {cfg.range_size()} uplink addresses are in use by active lab runs; a new run would "
                    "be refused.",
                    fix_hint="Clean up finished lab runs, or widen the range.",
                    details=details,
                )
        return ReadinessItem(
            key="uplink_network",
            status="ok",
            code="static_ok",
            message=f"Static uplink {cfg.range_start}-{cfg.range_end}/{cfg.prefix_length} via {cfg.gateway}"
            + (f", VLAN {cfg.vlan_tag}" if cfg.vlan_tag else ", untagged")
            + ".",
            details=details,
        )
    return ReadinessItem(
        key="uplink_network",
        status="ok",
        code="dhcp_ok",
        message="The uplink gets its address by DHCP" + (f" on VLAN {cfg.vlan_tag}." if cfg.vlan_tag else "."),
        details=details,
    )


def _find_storage(storages: list[StorageConfig], storage_id: str) -> StorageConfig | None:
    return next((s for s in storages if s.storage == storage_id), None)


def _check_snippet_storage(cluster: PveCluster, storages: _Lazy, lab_nodes: list[str]) -> ReadinessItem:
    storage_id = cluster.lab_snippet_storage_id
    if not storage_id:
        return ReadinessItem(
            key="snippet_storage",
            status="missing",
            code="not_configured",
            message="No snippet storage chosen for the probe appliance's per-run cloud-init config.",
            fix_hint="Pick a file-based storage (e.g. 'local') available on the lab nodes; RESURGAM can add the "
            "'snippets' content type to it.",
        )
    storage = _find_storage(storages.get(), storage_id)
    if storage is None:
        return ReadinessItem(
            key="snippet_storage",
            status="missing",
            code="storage_not_found",
            message=f"Storage '{storage_id}' does not exist (or the token can't see it).",
            fix_hint="Pick an existing storage.",
            details={"storage": storage_id},
        )
    details = {"storage": storage_id, "type": storage.type, "content": storage.content, "nodes": storage.nodes}
    if storage.type not in SNIPPET_CAPABLE_STORAGE_TYPES:
        return ReadinessItem(
            key="snippet_storage",
            status="missing",
            code="storage_type_unsupported",
            message=f"Storage '{storage_id}' is of type '{storage.type}', which cannot hold snippets.",
            fix_hint="Pick a file-based storage (dir, nfs, cifs, cephfs or btrfs).",
            details=details,
        )
    if storage.disabled:
        return ReadinessItem(
            key="snippet_storage",
            status="missing",
            code="storage_disabled",
            message=f"Storage '{storage_id}' is disabled.",
            fix_hint="Enable it in PVE (Datacenter > Storage) or pick another one.",
            details=details,
        )
    outside = _not_on_nodes(storage.nodes, lab_nodes)
    if outside:
        return ReadinessItem(
            key="snippet_storage",
            status="missing",
            code="storage_not_on_nodes",
            message=f"Storage '{storage_id}' is not available on lab node(s) {outside}.",
            fix_hint="Add the lab nodes to the storage's node list in PVE, or pick another storage.",
            details={**details, "missing_nodes": outside},
        )
    if "snippets" not in storage.content:
        return ReadinessItem(
            key="snippet_storage",
            status="missing",
            code="snippets_content_missing",
            message=f"Storage '{storage_id}' does not allow the 'snippets' content type yet.",
            fix_hint="Let RESURGAM add 'snippets' (existing content types are kept), or add it in PVE.",
            auto_fix="add_snippets_content",
            details=details,
        )
    return ReadinessItem(
        key="snippet_storage",
        status="ok",
        code="storage_ok",
        message=f"Storage '{storage_id}' accepts snippets on every lab node.",
        details=details,
    )


def _check_overlay_storage(storages: _Lazy, overlay_storage_id: str, lab_nodes: list[str]) -> ReadinessItem:
    storage = _find_storage(storages.get(), overlay_storage_id)
    if storage is None:
        return ReadinessItem(
            key="overlay_storage",
            status="missing",
            code="storage_not_found",
            message=f"The lab agent's overlay storage '{overlay_storage_id}' does not exist.",
            fix_hint=f"Let RESURGAM create it (type dir, path {DEFAULT_OVERLAY_DIR}, content images, restricted to "
            f"the lab nodes), or run on a node: pvesm add dir {overlay_storage_id} --path {DEFAULT_OVERLAY_DIR} "
            "--content images",
            auto_fix="create_overlay_storage",
            details={"storage": overlay_storage_id, "expected_path": DEFAULT_OVERLAY_DIR},
        )
    details = {
        "storage": overlay_storage_id,
        "type": storage.type,
        "path": storage.path,
        "content": storage.content,
        "nodes": storage.nodes,
    }
    if storage.type != "dir":
        return ReadinessItem(
            key="overlay_storage",
            status="missing",
            code="storage_type_not_dir",
            message=f"Storage '{overlay_storage_id}' is of type '{storage.type}'; the agent needs a 'dir' storage "
            "it can write qcow2 overlay files into.",
            fix_hint="Use a different overlay storage id for a new dir storage (and set "
            "RESURGAM_LAB_AGENT_OVERLAY_STORAGE_ID accordingly).",
            details=details,
        )
    if storage.disabled:
        return ReadinessItem(
            key="overlay_storage",
            status="missing",
            code="storage_disabled",
            message=f"Storage '{overlay_storage_id}' is disabled.",
            fix_hint="Enable it in PVE (Datacenter > Storage).",
            details=details,
        )
    outside = _not_on_nodes(storage.nodes, lab_nodes)
    if outside:
        return ReadinessItem(
            key="overlay_storage",
            status="missing",
            code="storage_not_on_nodes",
            message=f"Storage '{overlay_storage_id}' is not available on lab node(s) {outside}.",
            fix_hint="Add the lab nodes to the storage's node list in PVE (Datacenter > Storage > Edit > Nodes).",
            details={**details, "missing_nodes": outside},
        )
    if "images" not in storage.content:
        return ReadinessItem(
            key="overlay_storage",
            status="missing",
            code="images_content_missing",
            message=f"Storage '{overlay_storage_id}' does not allow the 'images' content type.",
            fix_hint="Let RESURGAM add 'images' (existing content types are kept), or add it in PVE.",
            auto_fix="add_images_content",
            details=details,
        )
    if storage.shared:
        return ReadinessItem(
            key="overlay_storage",
            status="warning",
            code="storage_marked_shared",
            message=f"Storage '{overlay_storage_id}' is marked 'shared', but each overlay file only exists on the "
            "node whose agent wrote it.",
            fix_hint="Unless the path really is shared storage, untick 'Shared' in PVE.",
            details=details,
        )
    return ReadinessItem(
        key="overlay_storage",
        status="ok",
        code="storage_ok",
        message=f"Overlay storage '{overlay_storage_id}' (dir, {storage.path}) is available on every lab node.",
        details=details,
    )


def _check_template(
    cluster: PveCluster,
    guests: _Lazy,
    lab_nodes: list[str],
    expected_prober_version: str | None = None,
    template_config: Callable[[str, int], dict] | None = None,
) -> ReadinessItem:
    vmid = cluster.lab_appliance_template_vmid
    if vmid is None:
        return ReadinessItem(
            key="appliance_template",
            status="missing",
            code="not_configured",
            message="No probe appliance template chosen.",
            fix_hint="Let RESURGAM build the template (appliance step: 'Build template'), or pick a VM template "
            "that has resurgam-lab-prober, qemu-guest-agent, dnsmasq, nftables and nmap installed.",
        )
    guest: GuestResource | None = next((g for g in guests.get() if g.vmid == vmid), None)
    if guest is None:
        return ReadinessItem(
            key="appliance_template",
            status="missing",
            code="template_not_found",
            message=f"VM {vmid} does not exist on this cluster.",
            fix_hint="Pick an existing template VM.",
            details={"vmid": vmid},
        )
    details = {"vmid": vmid, "name": guest.name, "node": guest.node, "guest_type": guest.guest_type}
    if guest.guest_type != "qemu":
        return ReadinessItem(
            key="appliance_template",
            status="missing",
            code="template_not_qemu",
            message=f"Guest {vmid} is a container ({guest.guest_type}); the probe appliance must be a QEMU VM.",
            fix_hint="Pick a VM template.",
            details=details,
        )
    if not guest.template:
        return ReadinessItem(
            key="appliance_template",
            status="missing",
            code="not_a_template",
            message=f"VM {vmid} ({guest.name or 'unnamed'}) is not a template.",
            fix_hint="Convert the prepared appliance VM to a template in PVE (More > Convert to template).",
            details=details,
        )
    if lab_nodes and guest.node not in lab_nodes:
        return ReadinessItem(
            key="appliance_template",
            status="missing",
            code="template_not_on_lab_node",
            message=f"Template {vmid} is on node '{guest.node}', which is not a lab node; the clone is requested on "
            "the lab run's target node and fails there.",
            fix_hint="Migrate the template to the lab node (or make that node a lab node).",
            details=details,
        )
    if expected_prober_version is not None and template_config is not None:
        config_read = True
        try:
            description = template_config(guest.node, vmid).get("description")
        except Exception as exc:  # noqa: BLE001 -- a failed read only drops the version comparison
            description, config_read = None, False
            details["version_check_error"] = str(exc)
        found_version = parse_prober_version(description)
        details["prober_version"] = found_version
        details["shipped_prober_version"] = expected_prober_version
        if not config_read:
            pass  # the template itself is fine; only its version could not be read (see details)
        elif found_version is None:
            return ReadinessItem(
                key="appliance_template",
                status="warning",
                code="template_version_unknown",
                message=f"Template {vmid} carries no RESURGAM build marker, so its resurgam-lab-prober version is "
                f"unknown (this RESURGAM ships {expected_prober_version}).",
                fix_hint="Let RESURGAM build a fresh template (appliance step), and rebuild it after every RESURGAM "
                "update.",
                details=details,
            )
        elif found_version != expected_prober_version:
            return ReadinessItem(
                key="appliance_template",
                status="warning",
                code="template_outdated",
                message=f"Template {vmid} has resurgam-lab-prober {found_version}; this RESURGAM ships "
                f"{expected_prober_version}.",
                fix_hint="Rebuild the template (appliance step) so the probe appliance matches this version.",
                details=details,
            )
    if len(lab_nodes) > 1:
        return ReadinessItem(
            key="appliance_template",
            status="warning",
            code="template_single_node",
            message=f"Template {vmid} is on node '{guest.node}'; lab runs whose first member targets another lab "
            "node cannot clone it.",
            fix_hint=f"Target '{guest.node}' for lab runs, or keep a copy of the template on every lab node.",
            details=details,
        )
    return ReadinessItem(
        key="appliance_template",
        status="ok",
        code="template_ok",
        message=f"Template {vmid} ({guest.name or 'unnamed'}) is on lab node '{guest.node}'"
        + (f", resurgam-lab-prober {details['prober_version']}." if details.get("prober_version") else "."),
        details=details,
    )


def check_lab_agent(cluster: PveCluster, now: datetime) -> ReadinessItem:
    if cluster.lab_agent_token_hash is None:
        return ReadinessItem(
            key="lab_agent",
            status="missing",
            code="token_missing",
            message="No lab agent token has been generated for this cluster yet.",
            fix_hint="Generate the token in the agent step and put it into the agent's env file.",
        )
    last = cluster.lab_agent_last_seen_at
    if last is None:
        return ReadinessItem(
            key="lab_agent",
            status="missing",
            code="never_seen",
            message="resurgam-lab-agent has never checked in with this cluster's token.",
            fix_hint="Install resurgam-lab-agent on the lab node, fill /etc/default/resurgam-lab-agent and start "
            "it: systemctl enable --now resurgam-lab-agent (logs: journalctl -u resurgam-lab-agent).",
        )
    age = now - last
    details = {"last_seen_at": last.isoformat(), "age_seconds": int(age.total_seconds())}
    if age > LAB_AGENT_RECENT_WINDOW:
        return ReadinessItem(
            key="lab_agent",
            status="warning",
            code="stale",
            message=f"resurgam-lab-agent last checked in {int(age.total_seconds() // 60)} minute(s) ago.",
            fix_hint="Check the service on the lab node: systemctl status resurgam-lab-agent; journalctl -u "
            "resurgam-lab-agent. A rotated token must also be updated in its env file.",
            details=details,
        )
    return ReadinessItem(
        key="lab_agent",
        status="ok",
        code="recently_seen",
        message="resurgam-lab-agent is checking in.",
        details=details,
    )


def check_backend_url(settings: Settings) -> ReadinessItem:
    url = settings.recovery_lab_public_backend_url
    if not url:
        return ReadinessItem(
            key="backend_url",
            status="missing",
            code="not_configured",
            message="RECOVERY_LAB_PUBLIC_BACKEND_URL is not set: the agent and the probe appliance don't know how to "
            "reach this backend.",
            fix_hint="Set RECOVERY_LAB_PUBLIC_BACKEND_URL (e.g. https://resurgam.example.com/api/v1) for the API and "
            "the recovery-lab scheduler, then restart them.",
        )
    if not url.startswith("https://"):
        return ReadinessItem(
            key="backend_url",
            status="warning",
            code="not_https",
            message=f"The public backend URL '{url}' is not HTTPS; agent and appliance tokens would travel in clear.",
            fix_hint="Use an https:// URL.",
            details={"url": url},
        )
    return ReadinessItem(
        key="backend_url",
        status="ok",
        code="configured",
        message=f"Agent and appliance call back to {url}.",
        details={"url": url},
    )


class _SnippetOverride:
    """Reads like the cluster, but with a different `lab_snippet_storage_id`
    (a not-yet-saved wizard choice, or the storage an auto-fix just changed)."""

    def __init__(self, cluster: PveCluster, snippet_storage_id: str):
        self._cluster = cluster
        self.lab_snippet_storage_id = snippet_storage_id

    def __getattr__(self, name: str):
        return getattr(self._cluster, name)


def run_lab_readiness(
    cluster: PveCluster,
    trust: TrustConfig,
    token: str,
    settings: Settings,
    *,
    nodes: list[str] | None = None,
    overlay_storage_id: str = DEFAULT_OVERLAY_STORAGE_ID,
    snippet_storage_id: str | None = None,
    keys: frozenset[str] | None = None,
    now: datetime | None = None,
    db: Session | None = None,
) -> ReadinessResult:
    """Runs the checks against the live cluster. `nodes` are the lab
    nodes to check per-node items on; if omitted, the overlay storage's own
    `nodes` restriction is used (the wizard creates it restricted to the lab
    nodes), else every online node. `snippet_storage_id` checks that storage
    instead of the cluster's saved `lab_snippet_storage_id`; `keys` limits
    the run to those items (an auto-fix re-checks only its own item)."""
    now = now or datetime.now(UTC)
    host, port, timeout = cluster.host, cluster.port, settings.connectivity_timeout_seconds

    def _perms():
        result = get_own_permissions(host, port, trust, token, timeout=timeout)
        if not result.reachable:
            raise RuntimeError(result.error)
        return result

    def _online_nodes():
        result = list_nodes(host, port, trust, token, timeout=timeout)
        if not result.reachable:
            raise RuntimeError(result.error)
        return [n.node for n in result.nodes if n.status in (None, "online")]

    manifest = try_load_manifest(settings.lab_artifacts_dir)
    prober = manifest.artifacts.get(LAB_PROBER_ARTIFACT) if manifest is not None else None
    expected_prober_version = prober.version if prober is not None else None

    def template_config(node: str, vmid: int) -> dict:
        return get_vm_config(host, port, trust, token, timeout, node=node, vmid=vmid)

    usage = None
    lab_subnets = None
    if db is not None:

        def usage() -> int:
            return uplink_usage(db, cluster.id)

        def lab_subnets() -> list[tuple[str, str]]:
            from resurgam_api.models.application_group import ApplicationGroup

            rows = db.query(ApplicationGroup.name, ApplicationGroup.lab_subnet_cidr).all()
            return [(name, cidr) for name, cidr in rows if cidr]

    perms = _Lazy(_perms)
    storages = _Lazy(lambda: list_storage_config(host, port, trust, token, timeout))
    guests = _Lazy(lambda: list_guest_resources(host, port, trust, token, timeout))
    online = _Lazy(_online_nodes)
    bridges_cache: dict[str, _Lazy] = {}
    zones_cache: dict[str, _Lazy] = {}

    def bridges_for(node: str) -> list:
        if node not in bridges_cache:
            bridges_cache[node] = _Lazy(lambda: list_node_bridges(host, port, trust, token, timeout, node=node))
        return bridges_cache[node].get()

    def get_zone(name: str) -> dict | None:
        if name not in zones_cache:
            zones_cache[name] = _Lazy(lambda: get_sdn_zone(host, port, trust, token, timeout, name=name))
        return zones_cache[name].get()

    lab_nodes: list[str] = []
    node_error: Exception | None = None
    if nodes:
        lab_nodes, source = list(nodes), "query"
    else:
        try:
            overlay = _find_storage(storages.get(), overlay_storage_id)
        except Exception:  # noqa: BLE001 -- falls through to the online-node list
            overlay = None
        if overlay is not None and overlay.nodes:
            lab_nodes, source = list(overlay.nodes), "overlay_storage"
        else:
            source = "all_online_nodes"
            try:
                lab_nodes = online.get()
            except Exception as exc:  # noqa: BLE001
                node_error = exc
    if not lab_nodes and node_error is None:
        node_error = RuntimeError("no online node found to check (and no lab nodes given)")

    def guarded(key: str, fn: Callable[[], ReadinessItem], needs_nodes: bool = False) -> ReadinessItem:
        if needs_nodes and node_error is not None:
            return _unknown(key, node_error)
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 -- isolate each check
            return _unknown(key, exc)

    snippet_cluster = cluster
    if snippet_storage_id is not None:
        snippet_cluster = _SnippetOverride(cluster, snippet_storage_id)

    checks: list[tuple[str, Callable[[], ReadinessItem], bool]] = [
        ("privileges", lambda: _check_privileges(perms), False),
        ("sdn_zone", lambda: _check_sdn_zone(cluster, get_zone, lab_nodes), True),
        ("uplink_bridge", lambda: _check_uplink_bridge(cluster, bridges_for, lab_nodes), True),
        (
            "uplink_network",
            lambda: _check_uplink_network(cluster, bridges_for, lab_nodes, usage, lab_subnets),
            True,
        ),
        ("snippet_storage", lambda: _check_snippet_storage(snippet_cluster, storages, lab_nodes), True),
        ("overlay_storage", lambda: _check_overlay_storage(storages, overlay_storage_id, lab_nodes), True),
        (
            "appliance_template",
            lambda: _check_template(cluster, guests, lab_nodes, expected_prober_version, template_config),
            True,
        ),
        ("lab_agent", lambda: check_lab_agent(cluster, now), False),
        ("backend_url", lambda: check_backend_url(settings), False),
    ]
    items = [guarded(key, fn, needs_nodes) for key, fn, needs_nodes in checks if keys is None or key in keys]
    return ReadinessResult(
        lab_nodes=lab_nodes,
        lab_nodes_source=source,
        overlay_storage_id=overlay_storage_id,
        checked_at=now,
        items=items,
    )
