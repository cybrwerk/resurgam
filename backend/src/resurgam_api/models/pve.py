import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin
from resurgam_api.models.pbs import ConnectionHealthStatus


class PveCluster(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A managed PVE cluster (manifest §6.2). Same trust/secret/health shape
    as `PbsInstance` — kept as a separate model rather than a shared base
    class, since the two are only accidentally similar today; manifest §6.2
    already gives PVE clusters fields (`allowed_target_storages`,
    `restore_network_rules`) that PBS instances don't have, and later
    milestones are likely to diverge them further.
    """

    __tablename__ = "pve_clusters"

    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=8006)

    tls_trust_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    tls_fingerprint_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    api_token_secret_ref_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("secret_references.id", ondelete="RESTRICT"), nullable=False
    )

    allowed_target_storages: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    restore_network_rules: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_health_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ConnectionHealthStatus.UNKNOWN.value
    )

    disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # EPIC 9 (manifest §11.2): if set, a restore plan against this cluster
    # needs a second operator's sign-off before it can be executed -- the
    # plan's own creator is never eligible to be that second operator.
    require_restore_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Recovery Lab (manifest EPIC 10, ADR 0019): whether this cluster may be
    # used as Lab compute. Deliberately not a singleton -- several clusters
    # can each be flagged, a given lab run picks one at creation time.
    is_lab_compute: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # If set, an admin-configured PVE SDN zone (already created in PVE, e.g.
    # a VLAN/VXLAN/EVPN zone spanning this cluster) that Lab runs on this
    # cluster use instead of RESURGAM's own auto-provisioned Simple zone --
    # a Simple zone's per-node isolated bridges mean a Lab run using it must
    # place every Application Group member on the same node (see
    # `restore_test/execution.py`), while an admin-configured zone that
    # spans the cluster lets members spread across nodes freely.
    lab_sdn_zone_name: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # The VMID of the template VM cloned as the probe appliance for each
    # Lab run on this cluster. Required before any run on this cluster can
    # be executed -- there is no default. RESURGAM builds it from the Lab
    # Compute wizard (a job run by the paired lab agent, ADR 0027, or a
    # one-time SSH build), or an admin sets an existing template by hand.
    lab_appliance_template_vmid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Long-lived, cluster-scoped bearer token for `resurgam-lab-agent`
    # (ADR 0019) -- bcrypt-hashed at rest, same hash-and-compare shape as
    # `RestoreTestPlan.appliance_run_token_hash`, just cluster-scoped and
    # long-lived rather than per-run. Issued by pairing a node (ADR 0027,
    # the pair call hands it to the node's script), by the guided SSH
    # install, or generated/rotated via
    # `POST /pve-clusters/{id}/lab-agent-token` -- each replaces the
    # previous one, and the plaintext is never shown or stored anywhere
    # else. `None` means no agent has been provisioned for this cluster
    # yet -- `/lab-agent/*` cannot be authenticated until one is issued.
    lab_agent_token_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Recovery Lab (Phase B): a PVE storage (content type `snippets`) that
    # `resurgam-lab-agent` writes each run's cloud-init user-data snippet
    # to (PVE has no REST endpoint to upload a snippet), so the
    # freshly-cloned probe appliance can be handed its per-run backend
    # URL/token and router config without rebuilding the golden template
    # per run. Required before any run's appliance can report back.
    lab_snippet_storage_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # An existing, already-connected PVE bridge (e.g. "vmbr0") for the
    # probe appliance's own uplink NIC (net1) -- the appliance is
    # dual-homed: net0 sits on the isolated per-run Lab VNet (where the
    # appliance itself acts as router/DHCP/firewall/SNAT, never PVE),
    # net1 reaches the RESURGAM backend and the internet like any other
    # VM on this bridge. Required before any run's appliance can ever
    # call home.
    lab_appliance_uplink_bridge: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # When `resurgam-lab-agent` last authenticated against `/lab-agent/*`
    # with this cluster's token (any endpoint -- the agent polls every few
    # seconds). Lets the Lab Compute setup wizard / readiness check confirm
    # the agent is actually installed and talking to this backend. `None`
    # means it has never checked in. Written at most every
    # `LAB_AGENT_LAST_SEEN_WRITE_INTERVAL` (api/v1/lab_agent.py) to keep the
    # poll loop from turning into a write per request.
    lab_agent_last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The probe appliance's uplink NIC (net1) beyond its bridge (migration
    # 0055, `lab_setup/uplink.py`): an optional VLAN tag (`tag=` on the
    # netX device) and how it gets its IPv4 address -- `dhcp` (default, the
    # bridge's LAN hands one out) or `static_range`, where RESURGAM gives
    # every active lab run (and a template build) its own address from
    # range start..end (`LabUplinkIpAllocation`), with this prefix length,
    # gateway and DNS servers, applied through cloud-init (`ipconfig1`,
    # `nameserver`).
    lab_appliance_uplink_vlan_tag: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lab_appliance_uplink_ip_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="dhcp", server_default="dhcp"
    )
    lab_appliance_uplink_ip_range_start: Mapped[str | None] = mapped_column(String(45), nullable=True)
    lab_appliance_uplink_ip_range_end: Mapped[str | None] = mapped_column(String(45), nullable=True)
    lab_appliance_uplink_ip_prefix_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lab_appliance_uplink_gateway: Mapped[str | None] = mapped_column(String(45), nullable=True)
    lab_appliance_uplink_dns_servers: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Disk Mounts (ADR 0025, migration 0057): the RESURGAM-built "share
    # helper" template cloned for every disk mount on this cluster
    # (`disk_mounts/share_helper_template.py`). NULL = disk mounts are not
    # set up on this cluster yet.
    disk_mount_helper_template_vmid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The PVE node the cluster's `resurgam-lab-agent` was paired on (ADR 0027,
    # `POST /lab-setup/bootstrap/{code}/pair`) -- where agent-run template
    # builds happen. `None` for an agent installed over SSH or by hand
    # before pairing existed (the wizard then asks for the node).
    lab_agent_node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Instant Recovery (ADR 0030, migration 0070): whether restore plans on this
    # cluster may use `method=instant` -- run the VM straight from the backup
    # through the cluster's paired `resurgam-lab-agent`. Off by default: it
    # makes the agent something a production cluster runs.
    instant_recovery_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    # Hours a VM may run from the backup before an alarm says "move it to
    # storage" (ADR 0030 §6; 24 confirmed by the user 2026-11-16).
    instant_recovery_alarm_hours: Mapped[int] = mapped_column(Integer, default=24, server_default="24", nullable=False)
    # ADR 0032: last check for guests no backup job covers (`pve_guest_backup_gaps`)
    # and the error of that check, if it failed.
    backup_gaps_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    backup_gaps_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class LabUplinkIpAllocation(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One probe-appliance uplink address taken from a cluster's static
    range (`PveCluster.lab_appliance_uplink_ip_mode == "static_range"`).
    The unique constraint on (cluster, address) is what makes two
    concurrent lab runs unable to get the same address; allocation also
    locks the cluster row first (`lab_setup.uplink.allocate_uplink_ip`).
    Released when the run is cleaned up (or its appliance could not be
    created), or when a template build ends."""

    __tablename__ = "lab_uplink_ip_allocations"
    __table_args__ = (
        UniqueConstraint("pve_cluster_id", "ip_address", name="uq_lab_uplink_ip_allocation_address"),
        UniqueConstraint("restore_test_plan_id", name="uq_lab_uplink_ip_allocation_plan"),
        UniqueConstraint("disk_mount_id", name="uq_lab_uplink_ip_allocation_disk_mount"),
    )

    pve_cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pve_clusters.id", ondelete="CASCADE"), nullable=False
    )
    ip_address: Mapped[str] = mapped_column(String(45), nullable=False)
    # `lab_run` (bound to a run), `disk_mount` (bound to a mount) or `template_build` (bound to an in-memory
    # build job; an orphan from a restarted API is reclaimed after a while).
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)
    restore_test_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("restore_test_plans.id", ondelete="CASCADE"), nullable=True
    )
    # A disk mount's helper address (purpose `disk_mount`, ADR 0025) when the
    # mount uses the cluster's lab uplink range.
    disk_mount_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("disk_mounts.id", ondelete="CASCADE"), nullable=True
    )


class PveClusterPbsInstance(Base, UUIDPrimaryKeyMixin):
    """Manifest §6.2: "Verbindung zu den verwendeten PBS-Instanzen" — which
    PBS instances a PVE cluster's restores may draw from.
    """

    __tablename__ = "pve_cluster_pbs_instances"
    __table_args__ = (UniqueConstraint("pve_cluster_id", "pbs_instance_id", name="uq_pve_cluster_pbs_instance"),)

    pve_cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pve_clusters.id", ondelete="CASCADE"), nullable=False
    )
    pbs_instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pbs_instances.id", ondelete="CASCADE"), nullable=False
    )


class PveClusterDatastore(Base, UUIDPrimaryKeyMixin):
    """Which PVE cluster(s) a given datastore's backups actually come from
    -- used *only* for workload-name resolution provenance (falling back to
    a real guest name via `list_guests()` when PBS's own per-group notes
    are empty), not for restore capability (`PveClusterPbsInstance` above
    already covers that, and is the wrong table to reuse here).

    Deliberately its own mapping rather than a reuse of
    `PveClusterPbsInstance`: that table links a cluster to a *PBS
    instance*, but a single PBS instance can host datastores for many
    different tenants/clusters (multi-tenant hosting), and a VMID is only
    unique *within* one PVE cluster (ADR 0009) -- resolving a name via "any
    cluster paired with this instance" risks matching the wrong guest
    across tenants. Scoping the link to the specific datastore instead
    keeps name resolution correctly tenant-scoped.
    """

    __tablename__ = "pve_cluster_datastores"
    __table_args__ = (UniqueConstraint("pve_cluster_id", "datastore_id", name="uq_pve_cluster_datastore"),)

    pve_cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pve_clusters.id", ondelete="CASCADE"), nullable=False
    )
    datastore_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datastores.id", ondelete="CASCADE"), nullable=False
    )


class PveBackupJobCustomerAssignment(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """An explicit customer-scope override for one PVE vzdump backup job
    (docs/adr/0023-backup-and-maintenance-job-administration). A vzdump job
    has no RESURGAM-owned row of its own -- it lives entirely in PVE's own
    config, fetched live -- so this table, not a column on some job row, is
    where that scope lives. Mirrors `DatastoreCustomerAssignment`'s shape
    exactly (same unique-one-customer, same cascade/restrict split), one
    level more specific: a job is identified by *which cluster* plus its
    own PVE-side string id, not a RESURGAM UUID.

    The default, in-design scope for a job is inherited from its target
    datastore/namespace (a `Datastore` is customer-scoped, per this
    project's own design) -- this table exists only for the override case,
    when a job's real targeting doesn't line up with that (e.g. `all_guests`
    spans multiple customers' storage, or a job intentionally targets a
    different customer's VMs than its usual storage owner).
    """

    __tablename__ = "pve_backup_job_customer_assignments"
    __table_args__ = (UniqueConstraint("pve_cluster_id", "job_id", name="uq_pve_backup_job_customer_assignment"),)

    pve_cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pve_clusters.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[str] = mapped_column(String(100), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )


class PveBackupJobReenable(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A pending automatic re-enable of one disabled PVE vzdump backup job
    ("disable for N hours", ADR 0018 addendum 2026-11-11). Like
    `PveBackupJobCustomerAssignment`, a job has no RESURGAM-owned row of its
    own, so it is keyed by cluster plus PVE's own job id.

    A row exists only while a re-enable is pending: the collector scheduler
    (`pve_job_reenable.run_due_reenables`) enables the job in PVE once
    `reenable_at` has passed and then deletes the row. A manual enable, a
    plain (indefinite) disable, deleting the job, or cancelling the timer
    deletes it too. A failed attempt keeps the row and is retried on the
    next tick, with `last_error` shown in the UI until it succeeds.
    """

    __tablename__ = "pve_backup_job_reenables"
    __table_args__ = (UniqueConstraint("pve_cluster_id", "job_id", name="uq_pve_backup_job_reenable"),)

    pve_cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pve_clusters.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[str] = mapped_column(String(100), nullable=False)
    reenable_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
