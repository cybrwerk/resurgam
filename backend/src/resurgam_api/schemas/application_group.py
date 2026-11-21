from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Manifest §22.6's typed checks -- no arbitrary scripts, deliberately.
# `arp` and `nmap_scan` added live (2026-09-22): a real Windows guest
# with its default firewall (blocks ICMP echo, allows RDP) failed
# `icmp` outright but answered ARP the whole time -- ARP resolution
# happens below any userspace firewall, proven more reliable for "is
# this host actually up" on a real restored guest. `nmap_scan` is a
# bounded top-100-ports scan giving real evidence of which services are
# reachable, not just one fixed port.
CheckType = Literal[
    "guest_agent", "icmp", "arp", "tcp_port", "http", "https", "dns", "tls_handshake", "ssh_banner", "nmap_scan"
]

# Recovery Lab Phase B: whether the group's probe appliance hands out
# DHCP leases on its own net0 subnet, or every member is expected to
# come up on a fixed, operator-known static IP (`ApplicationGroupMember.expected_ip`).
LabNetworkMode = Literal["dhcp", "static"]


class ApplicationGroupMemberIn(BaseModel):
    backup_group_id: UUID
    startup_order: int = 0
    check_types: list[CheckType] = Field(default_factory=list)
    expected_ip: str | None = Field(
        default=None,
        description="This member's known IP, if static -- skips guest-agent-based IP discovery. Null resolves dynamically once the member boots.",
    )


class ApplicationGroupMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="This membership row's own id.")
    backup_group_id: UUID = Field(description="The VM/CT this member tests, via its BackupGroup identity.")
    startup_order: int = Field(
        description="Where in the startup order this member boots during a Recovery Lab run -- lower boots first."
    )
    check_types: list[str] = Field(
        description="Which health checks (manifest §22.6) a Lab run probes this member with, e.g. 'icmp', 'tcp_port'."
    )
    expected_ip: str | None = Field(
        default=None,
        description="This member's known IP, if static -- skips guest-agent-based IP discovery. Null resolves dynamically once the member boots.",
    )
    backup_group_superseded: bool = Field(
        default=False,
        description=(
            "True if the referenced BackupGroup's VMID slot was reused for a different "
            "guest since this member was added (see BackupGroup.superseded_at) -- this "
            "member still points at the frozen, closed-out generation, never silently "
            "re-targeted. Review and re-point (or remove) this member if that's no longer "
            "the intended workload."
        ),
    )


class ApplicationGroupCreateRequest(BaseModel):
    customer_id: UUID
    name: str = Field(min_length=1, max_length=200)
    members: list[ApplicationGroupMemberIn] = Field(default_factory=list)
    lab_network_mode: LabNetworkMode = Field(
        default="dhcp",
        description="Whether this group's probe appliance serves DHCP on its own Lab-VNet subnet ('dhcp') or every member is expected to have a fixed static IP ('static').",
    )
    lab_subnet_cidr: str | None = Field(
        default=None,
        description="The probe appliance's own net0 subnet for this group's Lab runs, e.g. '10.99.0.0/24'. Required for any Level 2/3 check.",
    )
    lab_gateway_ip: str | None = Field(
        default=None,
        description="The probe appliance's own static IP on that subnet -- it acts as gateway/DHCP/firewall/SNAT for it, never PVE.",
    )
    lab_dns_ip: str | None = Field(
        default=None,
        description="DNS server handed to members via the appliance's DHCP. Defaults to the appliance's own gateway IP if unset.",
    )
    lab_dhcp_range_start: str | None = Field(
        default=None, description="First address the appliance's DHCP server hands out."
    )
    lab_dhcp_range_end: str | None = Field(
        default=None, description="Last address the appliance's DHCP server hands out."
    )

    lab_schedule_enabled: bool = Field(
        default=False,
        description="Whether this group runs Recovery Lab automatically on a recurring interval, in addition to any manually-created run.",
    )
    lab_schedule_interval_hours: int | None = Field(
        default=None, description="Hours between automatic runs. Required when lab_schedule_enabled is true."
    )
    lab_schedule_pve_cluster_id: UUID | None = Field(
        default=None,
        description="The Lab-enabled PVE cluster an automatic run targets. Required when lab_schedule_enabled is true.",
    )
    lab_schedule_target_node: str | None = Field(
        default=None,
        description="The PVE node every automatic run's members boot on. Required when lab_schedule_enabled is true.",
    )
    lab_schedule_target_storage: str | None = Field(
        default=None,
        description="The PVE storage every automatic run's members boot on. Required when lab_schedule_enabled is true.",
    )

    @model_validator(mode="after")
    def _check_schedule(self) -> "ApplicationGroupCreateRequest":
        _validate_lab_schedule(
            self.lab_schedule_enabled,
            self.lab_schedule_interval_hours,
            self.lab_schedule_pve_cluster_id,
            self.lab_schedule_target_node,
            self.lab_schedule_target_storage,
        )
        return self


def _validate_lab_schedule(
    enabled: bool,
    interval_hours: int | None,
    pve_cluster_id: UUID | None,
    target_node: str | None,
    target_storage: str | None,
) -> None:
    if enabled and (interval_hours is None or pve_cluster_id is None or not target_node or not target_storage):
        raise ValueError(
            "lab_schedule_interval_hours, lab_schedule_pve_cluster_id, lab_schedule_target_node and "
            "lab_schedule_target_storage are all required when lab_schedule_enabled is true"
        )


class ApplicationGroupUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    members: list[ApplicationGroupMemberIn] | None = None
    lab_network_mode: LabNetworkMode | None = None
    lab_subnet_cidr: str | None = None
    lab_gateway_ip: str | None = None
    lab_dns_ip: str | None = None
    lab_dhcp_range_start: str | None = None
    lab_dhcp_range_end: str | None = None

    lab_schedule_enabled: bool | None = None
    lab_schedule_interval_hours: int | None = None
    lab_schedule_pve_cluster_id: UUID | None = None
    lab_schedule_target_node: str | None = None
    lab_schedule_target_storage: str | None = None


class ApplicationGroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    customer_id: UUID
    name: str
    created_at: datetime
    members: list[ApplicationGroupMemberOut]
    lab_network_mode: str = Field(
        description="Whether this group's probe appliance serves DHCP on its own Lab-VNet subnet ('dhcp') or every member is expected to have a fixed static IP ('static')."
    )
    lab_subnet_cidr: str | None = Field(
        default=None,
        description="The probe appliance's own net0 subnet for this group's Lab runs, e.g. '10.99.0.0/24'. Required for any Level 2/3 check.",
    )
    lab_gateway_ip: str | None = Field(
        default=None,
        description="The probe appliance's own static IP on that subnet -- it acts as gateway/DHCP/firewall/SNAT for it, never PVE.",
    )
    lab_dns_ip: str | None = Field(
        default=None,
        description="DNS server handed to members via the appliance's DHCP. Defaults to the appliance's own gateway IP if unset.",
    )
    lab_dhcp_range_start: str | None = Field(
        default=None, description="First address the appliance's DHCP server hands out."
    )
    lab_dhcp_range_end: str | None = Field(
        default=None, description="Last address the appliance's DHCP server hands out."
    )

    lab_schedule_enabled: bool = Field(
        description="Whether this group runs Recovery Lab automatically on a recurring interval."
    )
    lab_schedule_interval_hours: int | None = Field(default=None, description="Hours between automatic runs.")
    lab_next_scheduled_run_at: datetime | None = Field(
        default=None, description="When the next automatic run is due. Null if lab_schedule_enabled is false."
    )
    lab_schedule_pve_cluster_id: UUID | None = Field(
        default=None, description="The Lab-enabled PVE cluster an automatic run targets."
    )
    lab_schedule_target_node: str | None = Field(
        default=None, description="The PVE node every automatic run's members boot on."
    )
    lab_schedule_target_storage: str | None = Field(
        default=None, description="The PVE storage every automatic run's members boot on."
    )
