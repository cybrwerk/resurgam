"""Whether a probe target's IP falls outside the appliance's own `net0`
subnet, and if so, a secondary address alias to add on that interface so
it can reach it -- a member with a baked-in static production IP outside
the group's configured DHCP range is a real, expected case (ADR 0019 /
Recovery Lab Phase B). Purely a self-contained change to the appliance's
own NIC -- no PVE/host involvement, matching the "PVE stays out of the
lab's network data path" design (see `backend`'s
`restore_test/execution.py` module docstring).
"""

from __future__ import annotations

import ipaddress
import subprocess


def needs_alias(own_subnet_cidr: str, target_ip: str) -> bool:
    network = ipaddress.ip_network(own_subnet_cidr, strict=False)
    return ipaddress.ip_address(target_ip) not in network


def alias_for_target(target_ip: str) -> str:
    """A /32 alias on `net0` covering just this one target -- narrower
    than guessing the target's real subnet mask (unknowable from a bare
    IP), sufficient for point-to-point reachability on a shared L2
    segment.
    """
    return f"{target_ip}/32"


def add_alias(interface: str, alias_cidr: str) -> None:
    """`ip addr add <cidr> dev <interface>` -- idempotent in effect (a
    duplicate add is a harmless no-op error from `ip`, not raised here
    since a target probed across multiple poll cycles will hit this
    repeatedly).
    """
    subprocess.run(  # noqa: S603 - fixed binary + argument list, no shell
        ["ip", "addr", "add", alias_cidr, "dev", interface], capture_output=True, text=True, check=False
    )
