import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class RestoreTestPlanStatus(str, Enum):
    """Manifest §22.2's state machine, aggregated across every member of
    the run's Application Group. `PARTIAL` means some members passed and
    others failed -- `PASSED` requires every member to have passed.
    """

    PLANNED = "planned"
    # Recovery Lab Phase B: a snippet with the appliance's per-run cloud-init
    # config (backend URL/token, router settings) is queued for
    # `resurgam-lab-agent` to write locally (PVE has no REST endpoint for
    # this -- confirmed live, 2026-09-21) -- the appliance clone itself is
    # deferred until the agent confirms the file is written.
    PROVISIONING_APPLIANCE = "provisioning_appliance"
    RUNNING = "running"
    PASSED = "passed"
    PARTIAL = "partial"
    FAILED = "failed"
    CLEANING_UP = "cleaning_up"
    COMPLETED = "completed"


class RestoreTestPlanMemberStatus(str, Enum):
    """One Application Group member's own state within one run -- distinct
    from the run's aggregate `RestoreTestPlanStatus`, since one member can
    fail while others still succeed.

    ADR 0019: member restoration is `resurgam-lab-agent`-driven (a
    zero-copy `proxmox-backup-client map` + direct VM-config attach), not
    a PVE-API `qmrestore`/`live-restore` call -- `QUEUED` is where the
    backend hands a member off (`GET /lab-agent/jobs` returns it),
    `MOUNTING` is the agent's own in-progress state once claimed
    (`POST /lab-agent/jobs/{id}/claim`), and `BOOTING` means the agent
    reported the VM started successfully
    (`POST /lab-agent/jobs/{id}/status`). `RESTORE_REQUESTED`/
    `RESTORE_RUNNING` are kept, unused by the current flow, rather than
    removed -- they matched the original PVE-API-driven design this ADR
    superseded and may still be useful if a future path restores via the
    PVE API directly (e.g. a non-Lab-agent fallback).
    """

    PENDING = "pending"
    QUEUED = "queued"
    MOUNTING = "mounting"
    RESTORE_REQUESTED = "restore_requested"
    RESTORE_RUNNING = "restore_running"
    BOOTING = "booting"
    PROBING = "probing"
    PASSED = "passed"
    FAILED = "failed"


class RestoreTestPlan(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Manifest EPIC 10 (§22): one Recovery Lab run against a real, reviewed
    `ApplicationGroup` -- a real persisted row per run, not stateless, same
    reasoning ADR 0012 used for `RestorePlan`. `pve_cluster_id` must
    reference a cluster with `PveCluster.is_lab_compute=True`.

    `vnet_name` is this run's own uniquely-named isolated PVE SDN VNet
    (manifest §22.5), created fresh per run regardless of which zone it
    sits under (RESURGAM's auto-provisioned Simple zone, or an
    admin-configured cluster-spanning zone) -- see `restore_test/execution.py`
    for how the zone's real type constrains member node placement.

    `appliance_run_token_hash` authenticates the per-run probe appliance's
    callback (`POST /lab-runs/{id}/probe-results`) -- scoped to exactly
    this run, never a user session, same shape as `auth/sessions.py`'s
    token hashing.
    """

    __tablename__ = "restore_test_plans"

    application_group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("application_groups.id", ondelete="RESTRICT"), nullable=False
    )
    # Snapshotted from the group at run-creation time, same reasoning as
    # `RestorePlan.customer_id` -- a later group/customer change shouldn't
    # retroactively change who a past run is attributed to.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    pve_cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pve_clusters.id", ondelete="RESTRICT"), nullable=False
    )

    vnet_name: Mapped[str] = mapped_column(String(20), nullable=False)
    appliance_vmid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    appliance_run_token_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Recovery Lab Phase B: the appliance's cloud-init snippet content,
    # queued for `resurgam-lab-agent` to write to the cluster's local
    # snippet storage path (no PVE REST endpoint exists for this --
    # confirmed live, 2026-09-21). {"filename": ..., "content": ...}.
    # Cleared once the agent confirms the write; the appliance clone
    # itself only proceeds after that.
    appliance_snippet_pending: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    status: Mapped[str] = mapped_column(String(30), nullable=False, default=RestoreTestPlanStatus.PLANNED.value)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class RestoreTestPlanMember(Base, UUIDPrimaryKeyMixin):
    """One `ApplicationGroupMember`'s execution state within one specific
    `RestoreTestPlan` run. `backup_snapshot_id` is the specific snapshot
    resolved at run-creation time (typically that member's most recent),
    not a live reference to "whatever the group currently points at" --
    same snapshotting reasoning as `RestorePlan.backup_snapshot_id`.
    """

    __tablename__ = "restore_test_plan_members"

    restore_test_plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("restore_test_plans.id", ondelete="CASCADE"), nullable=False
    )
    application_group_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("application_group_members.id", ondelete="RESTRICT"), nullable=False
    )
    backup_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backup_snapshots.id", ondelete="RESTRICT"), nullable=False
    )

    target_vmid: Mapped[int] = mapped_column(Integer, nullable=False)
    target_node: Mapped[str] = mapped_column(String(255), nullable=False)
    target_storage: Mapped[str] = mapped_column(String(255), nullable=False)
    upid: Mapped[str | None] = mapped_column(String(255), nullable=True)

    member_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=RestoreTestPlanMemberStatus.PENDING.value
    )
    # Recovery Lab Phase B: this member's real IP, once resolved --
    # `ApplicationGroupMember.expected_ip` if set, else learned via
    # `agent_network_get_interfaces` once its guest agent is reachable.
    # Persisted (not recomputed every poll) so `GET .../probe-targets`
    # can serve the probe appliance without a live PVE round-trip per
    # request.
    resolved_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    # List of {"check_type", "status", "detail", "checked_at"} -- mirrors
    # `RestorePlan.checks_result`'s JSONB precedent.
    probe_results: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
