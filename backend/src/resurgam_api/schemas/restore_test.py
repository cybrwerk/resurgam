from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RestoreTestPlanCreateRequest(BaseModel):
    application_group_id: UUID
    pve_cluster_id: UUID
    # Shared target node/storage for every member in this first pass --
    # required for a Simple-zone cluster (every member must land on the
    # same node anyway); an admin-configured cluster-spanning zone could
    # in principle place members independently, but that's not exposed
    # here yet (see `restore_test/execution.py`'s docstrings).
    target_node: str
    target_storage: str


class ProbeResultEntry(BaseModel):
    check_type: str
    status: Literal["pass", "fail"]
    detail: str | None = None
    checked_at: datetime


class RestoreTestPlanMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    application_group_member_id: UUID
    backup_snapshot_id: UUID
    target_vmid: int
    target_node: str
    target_storage: str
    upid: str | None
    member_status: str
    probe_results: list[ProbeResultEntry]
    resolved_ip: str | None = Field(
        default=None,
        description="This member's real IP once resolved (static ApplicationGroupMember.expected_ip, "
        "or learned via the guest agent) -- null until Level 1 (guest_agent) succeeds and IP discovery runs.",
    )
    started_at: datetime | None
    completed_at: datetime | None
    error: str | None


class RestoreTestPlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    application_group_id: UUID
    customer_id: UUID | None
    pve_cluster_id: UUID
    vnet_name: str
    appliance_vmid: int | None
    status: str
    created_by_user_id: UUID | None
    requested_at: datetime | None
    completed_at: datetime | None
    error: str | None
    members: list[RestoreTestPlanMemberOut]


class RestoreTestPlanExecuteOut(BaseModel):
    """`execute`'s response carries the plaintext appliance run token
    exactly once -- never persisted, never returned again by `GET`.
    """

    plan: RestoreTestPlanOut
    appliance_run_token: str


class ProbeResultSubmission(BaseModel):
    """Body of the probe appliance's `POST /lab-runs/{id}/probe-results`
    callback -- authenticated by the per-run bearer token, not a user
    session (see `restore_test/execution.py`'s token hashing).
    """

    member_id: UUID
    results: list[ProbeResultEntry] = Field(min_length=1)


class ProbeTargetOut(BaseModel):
    """One entry of `GET /lab-runs/{id}/probe-targets` -- the probe
    appliance's own job list. Only Level 2/3 (network-tier) check types
    are ever listed here; Level 1 (`guest_agent`) is always resolved
    backend-side via PVE's REST-proxied guest-agent channel, never by
    the appliance (see `restore_test/execution.py::NETWORK_TIER_CHECK_TYPES`).
    """

    member_id: UUID = Field(description="This Lab run member's own id -- echoed back on the results callback.")
    ip: str = Field(description="This member's resolved IP to probe.")
    check_types: list[str] = Field(
        description="Which network-tier checks (e.g. 'icmp', 'tcp_port') are still pending for this member."
    )


class ApplianceSnippetJobOut(BaseModel):
    """One entry of `GET /lab-agent/appliance-jobs` -- a plan whose probe
    appliance cloud-init snippet still needs writing to local disk (no
    PVE REST endpoint exists to upload a snippet file, confirmed live
    2026-09-21 -- `resurgam-lab-agent` writes it directly instead).
    """

    plan_id: UUID = Field(
        description="The Recovery Lab run this snippet belongs to -- echoed back on the write confirmation."
    )
    snippet_storage_id: str = Field(
        description="The PVE storage id (content type 'snippets') to resolve a local path under, e.g. via `pvesm path`."
    )
    filename: str = Field(description="The snippet's filename, unique per run.")
    content: str = Field(description="The cloud-init user-data YAML content to write verbatim.")
