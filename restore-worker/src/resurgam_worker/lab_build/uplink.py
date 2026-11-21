"""The lab uplink NIC settings a build VM boots with (bridge, VLAN, DHCP or a
static address) -- the pure part of the backend's `lab_setup/uplink.py`."""

import ipaddress
from dataclasses import dataclass, field

IP_MODE_DHCP = "dhcp"
IP_MODE_STATIC_RANGE = "static_range"
IP_MODES = (IP_MODE_DHCP, IP_MODE_STATIC_RANGE)


@dataclass(frozen=True)
class UplinkConfig:
    bridge: str | None
    vlan_tag: int | None = None
    ip_mode: str = IP_MODE_DHCP
    range_start: str | None = None
    range_end: str | None = None
    prefix_length: int | None = None
    gateway: str | None = None
    dns_servers: list[str] = field(default_factory=list)

    @property
    def is_static(self) -> bool:
        return self.ip_mode == IP_MODE_STATIC_RANGE

    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(f"{self.range_start}/{self.prefix_length}", strict=False)

    def range_size(self) -> int:
        return int(ipaddress.IPv4Address(self.range_end)) - int(ipaddress.IPv4Address(self.range_start)) + 1


def uplink_nic(bridge: str, vlan_tag: int | None) -> str:
    """The appliance's uplink `netX` value, e.g. `model=virtio,bridge=vmbr0,tag=20`."""
    value = f"model=virtio,bridge={bridge}"
    if vlan_tag is not None:
        value += f",tag={vlan_tag}"
    return value


def uplink_ipconfig(cfg: UplinkConfig, address: str | None) -> str:
    """The uplink `ipconfigX` value: `ip=dhcp`, or `ip=<addr>/<prefix>,gw=<gw>`."""
    if not cfg.is_static:
        return "ip=dhcp"
    if not address:
        raise ValueError("a static uplink needs an allocated address")
    return f"ip={address}/{cfg.prefix_length},gw={cfg.gateway}"


def uplink_nameserver(cfg: UplinkConfig) -> str | None:
    """The `nameserver` value (space-separated), or None for DHCP."""
    if not cfg.is_static or not cfg.dns_servers:
        return None
    return " ".join(cfg.dns_servers)
