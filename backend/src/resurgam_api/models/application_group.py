import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class ApplicationGroup(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Manifest §22.4 "Application Groups": a customer-scoped, reusable
    definition of several VMs/CTs that together make up one application
    (e.g. a database, an app server, a web front-end), defined once and
    reused by many Recovery Lab runs (`RestoreTestPlan`, `restore_test.py`)
    -- a single-VM smoke test is just a one-member group, not a separate
    code path.
    """

    __tablename__ = "application_groups"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Recovery Lab (Phase B): the *probe appliance's own* router/DHCP
    # config for this group's Lab runs -- PVE itself never provisions a
    # subnet/gateway/DHCP/SNAT on the Lab VNet (a direct user correction:
    # the PVE host must never become part of the lab's network data
    # path). The cloned appliance VM is dual-homed (net0 on the isolated
    # Lab VNet, net1 on an existing uplink bridge) and runs its own
    # dnsmasq + nftables masquerade using these values -- see
    # `restore_test/execution.py` and `resurgam_worker.lab_prober`. All
    # nullable: a group with none of this set can still run the
    # `guest_agent` check (no network path needed), just not Level 2/3.
    lab_network_mode: Mapped[str] = mapped_column(String(10), nullable=False, default="dhcp")
    lab_subnet_cidr: Mapped[str | None] = mapped_column(String(43), nullable=True)
    lab_gateway_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    lab_dns_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    lab_dhcp_range_start: Mapped[str | None] = mapped_column(String(45), nullable=True)
    lab_dhcp_range_end: Mapped[str | None] = mapped_column(String(45), nullable=True)

    # Recurring Lab runs, raised directly by the user ("the lab Scheduler
    # (Cron Like is not there)"), approved as one schedule per Application
    # Group rather than a separate many-schedules table -- mirrors a PVE
    # backup job or PBS prune job carrying its own schedule. Shape mirrors
    # `CustomerFileIndexSettings.schedule_enabled`/`schedule_interval_hours`/
    # `next_scheduled_run_at`, this codebase's only other recurring-schedule
    # feature: "interval-since-last-run, not cron syntax" (ADR 0006 -- no
    # general scheduler exists in this codebase). The cluster/node/storage
    # triple mirrors `CustomerFileIndexSettings.guest_exec_pve_cluster_id`/
    # `guest_exec_target_node`/`guest_exec_target_storage` -- the same
    # "operator sets it once, the scheduler uses it every time" precedent,
    # needed here because a scheduled run has no human present to choose a
    # target the way `CreateRunForm` normally asks for one.
    lab_schedule_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    lab_schedule_interval_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lab_next_scheduled_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lab_schedule_pve_cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pve_clusters.id", ondelete="SET NULL"), nullable=True
    )
    lab_schedule_target_node: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lab_schedule_target_storage: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ApplicationGroupMember(Base, UUIDPrimaryKeyMixin):
    """One VM/CT within an `ApplicationGroup`, plus its Lab-specific
    metadata: where in the startup order it boots (manifest §22.4's
    ordering requirement -- e.g. a database before the app server that
    depends on it) and which checks (manifest §22.6) a Lab run probes it
    with. `backup_group_id` is RESTRICT, matching every other reference to
    a `BackupGroup` in this codebase (e.g. `RestorePlan.backup_group_id`)
    -- the Collector owns that row's lifecycle, not this feature.
    """

    __tablename__ = "application_group_members"

    application_group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("application_groups.id", ondelete="CASCADE"), nullable=False
    )
    backup_group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backup_groups.id", ondelete="RESTRICT"), nullable=False
    )
    startup_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # List of manifest §22.6's typed checks, e.g. ["guest_agent", "icmp",
    # "tcp:22", "http:80"] -- no arbitrary scripts (explicitly forbidden by
    # the manifest). Exact string vocabulary owned by `restore_test/`.
    check_types: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Recovery Lab (Phase B): the real IP this member is expected to come
    # up on, if known ahead of time (e.g. a static-IP production VM) --
    # skips guest-agent-based IP discovery entirely. None means "resolve
    # dynamically" via the member's own guest agent once it boots (see
    # `restore_test/execution.py::member_probe_plan`).
    expected_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
