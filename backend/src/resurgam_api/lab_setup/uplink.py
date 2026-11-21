"""The probe appliance's uplink NIC (net1): VLAN tag and IPv4 address.

The appliance is dual-homed (ADR 0020): net0 on the run's isolated Lab VNet,
net1 on `PveCluster.lab_appliance_uplink_bridge`, from where it calls the
RESURGAM backend. This module turns the cluster's uplink settings into the
PVE config strings applied when the appliance is cloned
(`api/v1/lab_agent.confirm_appliance_snippet_written`) and when its template
is built (`resurgam_worker.lab_build.probe_template`, which the backend's
`lab_setup/template_build.py` aliases), and hands out static addresses.

PVE syntax (apidoc.js, `PUT /nodes/{node}/qemu/{vmid}/config`, checked
2026-10-07):

- `net[n]`: `[model=]<enum> [,bridge=<bridge>] ... [,tag=<integer>]`, `tag`
  "VLAN tag to apply to packets on this interface", 1-4094.
- `ipconfig[n]`: `[gw=<GatewayIPv4>] [,ip=<IPv4Format/CIDR>]`, or `ip=dhcp`
  ("in which case no explicit gateway should be provided").
- `nameserver`: "cloud-init: Sets DNS server IP address", format
  `address-list` (several addresses separated by spaces).

The appliance image is the cloud-init driven Debian template, so cloud-init
(PVE's generated network config + `cicustom` user data) is the mechanism --
the same one already used for net0's static gateway address.
"""

import ipaddress
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# The NIC settings themselves live with the builds the lab agent also runs (ADR 0027).
from resurgam_worker.lab_build.uplink import (  # noqa: F401  (re-exported)
    IP_MODE_DHCP,
    IP_MODE_STATIC_RANGE,
    IP_MODES,
    UplinkConfig,
    uplink_ipconfig,
    uplink_nameserver,
    uplink_nic,
)
from sqlalchemy.orm import Session

from resurgam_api.errors import ResurgamApiError

PURPOSE_LAB_RUN = "lab_run"
PURPOSE_TEMPLATE_BUILD = "template_build"
PURPOSE_DISK_MOUNT = "disk_mount"  # a Disk Mounts helper VM (ADR 0025)
# A template-build allocation belongs to an in-memory job; if the API was
# restarted mid-build nothing releases it, so it is reclaimed after this.
TEMPLATE_BUILD_ALLOCATION_TTL = timedelta(hours=2)
MAX_DNS_SERVERS = 3


@dataclass(frozen=True)
class UplinkProblem:
    field: str
    message: str


def uplink_config_from_cluster(cluster) -> UplinkConfig:
    return UplinkConfig(
        bridge=cluster.lab_appliance_uplink_bridge,
        vlan_tag=cluster.lab_appliance_uplink_vlan_tag,
        ip_mode=cluster.lab_appliance_uplink_ip_mode or IP_MODE_DHCP,
        range_start=cluster.lab_appliance_uplink_ip_range_start,
        range_end=cluster.lab_appliance_uplink_ip_range_end,
        prefix_length=cluster.lab_appliance_uplink_ip_prefix_length,
        gateway=cluster.lab_appliance_uplink_gateway,
        dns_servers=list(cluster.lab_appliance_uplink_dns_servers or []),
    )


def _ipv4(value: str | None, field_name: str, label: str, problems: list[UplinkProblem]):
    if not value:
        problems.append(UplinkProblem(field_name, f"{label} is required for a static range."))
        return None
    try:
        return ipaddress.IPv4Address(value)
    except ValueError:
        problems.append(UplinkProblem(field_name, f"{label} '{value}' is not an IPv4 address (e.g. 192.168.10.50)."))
        return None


def validate_uplink_config(cfg: UplinkConfig) -> list[UplinkProblem]:
    """Every problem with the settings, each with the field and how to fix
    it. Empty = valid. DHCP needs nothing beyond an optional VLAN tag."""
    problems: list[UplinkProblem] = []
    if cfg.vlan_tag is not None and not 1 <= cfg.vlan_tag <= 4094:
        problems.append(UplinkProblem("lab_appliance_uplink_vlan_tag", "The VLAN tag must be between 1 and 4094."))
    if cfg.ip_mode not in IP_MODES:
        problems.append(
            UplinkProblem("lab_appliance_uplink_ip_mode", f"Unknown IP mode '{cfg.ip_mode}'; use dhcp or static_range.")
        )
        return problems
    if not cfg.is_static:
        return problems

    start = _ipv4(cfg.range_start, "lab_appliance_uplink_ip_range_start", "The range start", problems)
    end = _ipv4(cfg.range_end, "lab_appliance_uplink_ip_range_end", "The range end", problems)
    gateway = _ipv4(cfg.gateway, "lab_appliance_uplink_gateway", "The gateway", problems)
    prefix = cfg.prefix_length
    if prefix is None:
        problems.append(UplinkProblem("lab_appliance_uplink_ip_prefix_length", "The prefix length is required."))
    elif not 8 <= prefix <= 30:
        problems.append(
            UplinkProblem(
                "lab_appliance_uplink_ip_prefix_length",
                "The prefix length must be between 8 and 30 (e.g. 24 for 255.255.255.0).",
            )
        )
        prefix = None
    if not cfg.dns_servers:
        problems.append(
            UplinkProblem(
                "lab_appliance_uplink_dns_servers",
                "Give at least one DNS server: with a static address the probe appliance has no other way to "
                "resolve the RESURGAM backend's host name.",
            )
        )
    if len(cfg.dns_servers) > MAX_DNS_SERVERS:
        problems.append(
            UplinkProblem("lab_appliance_uplink_dns_servers", f"At most {MAX_DNS_SERVERS} DNS servers are supported.")
        )
    for dns in cfg.dns_servers:
        try:
            ipaddress.IPv4Address(dns)
        except ValueError:
            problems.append(
                UplinkProblem("lab_appliance_uplink_dns_servers", f"DNS server '{dns}' is not an IPv4 address.")
            )

    if start is None or end is None or prefix is None:
        return problems
    network = ipaddress.IPv4Network(f"{start}/{prefix}", strict=False)
    if end not in network:
        problems.append(
            UplinkProblem(
                "lab_appliance_uplink_ip_range_end",
                f"Range start {start} and end {end} are not in the same /{prefix} subnet ({network}).",
            )
        )
        return problems
    if int(start) > int(end):
        problems.append(
            UplinkProblem("lab_appliance_uplink_ip_range_end", f"The range end {end} is before its start {start}.")
        )
        return problems
    for label, address, field_name in (
        ("range start", start, "lab_appliance_uplink_ip_range_start"),
        ("range end", end, "lab_appliance_uplink_ip_range_end"),
    ):
        if address in (network.network_address, network.broadcast_address):
            problems.append(
                UplinkProblem(field_name, f"The {label} {address} is the network or broadcast address of {network}.")
            )
    if gateway is not None:
        if gateway not in network:
            problems.append(
                UplinkProblem("lab_appliance_uplink_gateway", f"The gateway {gateway} is not inside {network}.")
            )
        elif gateway in (network.network_address, network.broadcast_address):
            problems.append(
                UplinkProblem(
                    "lab_appliance_uplink_gateway", f"The gateway {gateway} is the network or broadcast address."
                )
            )
        elif int(start) <= int(gateway) <= int(end):
            problems.append(
                UplinkProblem(
                    "lab_appliance_uplink_gateway",
                    f"The gateway {gateway} lies inside the range {start}-{end}; shrink the range so it excludes "
                    "the gateway.",
                )
            )
    return problems


def raise_if_invalid(cfg: UplinkConfig) -> None:
    problems = validate_uplink_config(cfg)
    if problems:
        raise ResurgamApiError(
            code="invalid_uplink_network",
            message="; ".join(p.message for p in problems),
            status_code=422,
            details={"errors": [{"field": p.field, "message": p.message} for p in problems]},
        )


def overlapping_lab_subnets(cfg: UplinkConfig, subnets: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """(name, cidr) Application Group lab subnets that overlap the static
    uplink subnet -- the appliance routes between the two, so an overlap
    breaks either the lab side or the way back to the backend."""
    if not cfg.is_static or validate_uplink_config(cfg):
        return []
    uplink = cfg.network()
    overlapping = []
    for name, cidr in subnets:
        try:
            if ipaddress.ip_network(cidr, strict=False).overlaps(uplink):
                overlapping.append((name, cidr))
        except ValueError:
            continue
    return overlapping


def _allocations_model():
    from resurgam_api.models.pve import LabUplinkIpAllocation

    return LabUplinkIpAllocation


def allocate_uplink_ip(
    db: Session,
    cluster,
    *,
    purpose: str,
    restore_test_plan_id: uuid.UUID | None = None,
    disk_mount_id: uuid.UUID | None = None,
) -> str | None:
    """Takes the lowest free address of the cluster's static range for one
    run (or one template build) and returns it; `None` for DHCP. Locks the
    cluster row first (`SELECT ... FOR UPDATE`) so concurrent allocations
    queue up; the unique (cluster, address) constraint backs that up.
    Idempotent per run. Raises 409 `lab_uplink_range_exhausted` when every
    address is taken. Does not commit."""
    from resurgam_api.models.pve import PveCluster

    allocation_model = _allocations_model()
    locked = db.query(PveCluster).filter_by(id=cluster.id).with_for_update().one()
    cfg = uplink_config_from_cluster(locked)
    if not cfg.is_static:
        return None
    raise_if_invalid(cfg)

    if restore_test_plan_id is not None:
        existing = db.query(allocation_model).filter_by(restore_test_plan_id=restore_test_plan_id).one_or_none()
        if existing is not None:
            return existing.ip_address
    if disk_mount_id is not None:
        existing = db.query(allocation_model).filter_by(disk_mount_id=disk_mount_id).one_or_none()
        if existing is not None:
            return existing.ip_address

    stale_before = datetime.now(UTC) - TEMPLATE_BUILD_ALLOCATION_TTL
    db.query(allocation_model).filter(
        allocation_model.pve_cluster_id == cluster.id,
        allocation_model.purpose == PURPOSE_TEMPLATE_BUILD,
        allocation_model.created_at < stale_before,
    ).delete(synchronize_session=False)

    taken = {row.ip_address for row in db.query(allocation_model.ip_address).filter_by(pve_cluster_id=cluster.id).all()}
    start = int(ipaddress.IPv4Address(cfg.range_start))
    end = int(ipaddress.IPv4Address(cfg.range_end))
    for value in range(start, end + 1):
        address = str(ipaddress.IPv4Address(value))
        if address in taken:
            continue
        db.add(
            allocation_model(
                pve_cluster_id=cluster.id,
                ip_address=address,
                purpose=purpose,
                restore_test_plan_id=restore_test_plan_id,
                disk_mount_id=disk_mount_id,
            )
        )
        db.flush()
        return address
    raise ResurgamApiError(
        code="lab_uplink_range_exhausted",
        message=f"All {end - start + 1} uplink addresses ({cfg.range_start}-{cfg.range_end}) are in use by active "
        "lab runs, disk mounts or a template build. Clean up finished lab runs (each run keeps its address until "
        "cleanup), stop disk mounts that are no longer needed, or widen the range in the Lab Compute setup (network "
        "step).",
        status_code=409,
        details={"range_start": cfg.range_start, "range_end": cfg.range_end, "in_use": len(taken)},
    )


def allocated_uplink_ip(db: Session, restore_test_plan_id: uuid.UUID) -> str | None:
    allocation_model = _allocations_model()
    row = db.query(allocation_model).filter_by(restore_test_plan_id=restore_test_plan_id).one_or_none()
    return row.ip_address if row is not None else None


def release_uplink_ip(
    db: Session,
    *,
    restore_test_plan_id: uuid.UUID | None = None,
    disk_mount_id: uuid.UUID | None = None,
    cluster_id=None,
    address: str | None = None,
) -> None:
    """Frees a run's (or a disk mount's) address, or one specific address of a
    cluster. Does not commit."""
    allocation_model = _allocations_model()
    query = db.query(allocation_model)
    if restore_test_plan_id is not None:
        query = query.filter_by(restore_test_plan_id=restore_test_plan_id)
    elif disk_mount_id is not None:
        query = query.filter_by(disk_mount_id=disk_mount_id)
    elif cluster_id is not None and address is not None:
        query = query.filter_by(pve_cluster_id=cluster_id, ip_address=address)
    else:
        return
    query.delete(synchronize_session=False)


def uplink_usage(db: Session, cluster_id) -> int:
    allocation_model = _allocations_model()
    return db.query(allocation_model).filter_by(pve_cluster_id=cluster_id).count()
