"""Lab node pairing and agent-run template builds (docs/adr/0027)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from resurgam_api.restore_test.lab_readiness import DEFAULT_OVERLAY_DIR, DEFAULT_OVERLAY_STORAGE_ID
from resurgam_api.schemas.lab_provisioning import _STORAGE_ID_PATTERN, LabSetupJobStepOut
from resurgam_api.schemas.lab_setup import PveNodeName


class LabNodePairingCreate(BaseModel):
    node: PveNodeName = Field(
        description="The PVE node to pair as this cluster's lab node -- the command refuses to run on any other host."
    )
    overlay_storage_id: str = Field(
        default=DEFAULT_OVERLAY_STORAGE_ID,
        pattern=_STORAGE_ID_PATTERN,
        max_length=64,
        description="The agent's overlay storage id (RESURGAM_LAB_AGENT_OVERLAY_STORAGE_ID).",
    )
    overlay_dir: str = Field(
        default=DEFAULT_OVERLAY_DIR,
        pattern=r"^/[A-Za-z0-9_.\-/]+$",
        max_length=255,
        description="The overlay storage's path on the node (RESURGAM_LAB_AGENT_OVERLAY_DIR).",
    )


class LabNodePairingOut(BaseModel):
    id: UUID = Field(description="Pairing id; poll GET /pve-clusters/{id}/lab-setup/pairings/{pairing_id}.")
    node: str = Field(description="The node this code pairs.")
    code: str | None = Field(
        default=None,
        description="The one-time code (only in the create response; RESURGAM stores just its SHA-256).",
    )
    command: str | None = Field(
        default=None,
        description="The command to paste into the node's root shell (only in the create response). It downloads "
        "the pairing script by the pairing's id (no secret, fetched before the node trusts RESURGAM's "
        "certificate), checks its SHA-256 and runs it with the code as argument; the script sends the code only "
        "over verified TLS.",
    )
    script_sha256: str = Field(description="SHA-256 of the pairing script, pinned in `command`.")
    shell_url: str = Field(
        description="Proxmox VE's own web shell for this node (what its 'Shell' button opens) -- paste the command "
        "there."
    )
    expires_at: datetime = Field(description="The code is refused after this (UTC).")
    state: Literal["waiting", "paired", "expired", "revoked"] = Field(
        description="`waiting` until the node uses the code, `paired` once it did, `expired` after `expires_at`, "
        "`revoked` when a newer code for this cluster replaced it."
    )
    consumed_at: datetime | None = Field(default=None, description="When the node used the code (UTC).")
    pbs_token_provided: bool | None = Field(
        default=None,
        description="Whether the node's PBS token was entered during pairing. Without it the agent only runs "
        "template builds until it is added on the node.",
    )
    agent_last_seen_at: datetime | None = Field(
        default=None, description="When the cluster's agent last called RESURGAM (UTC) -- confirms it is running."
    )


class PairRequest(BaseModel):
    node: PveNodeName = Field(description="The node's short host name (`hostname -s`), i.e. its PVE node name.")
    pbs_token_provided: bool = Field(
        default=False, description="Whether a PBS token was entered on the node (the token itself is never sent)."
    )


class LabAgentSetupJobCreate(BaseModel):
    kind: Literal["template_build", "share_helper_template_build"] = Field(
        description="`template_build`: the Recovery Lab probe appliance template. `share_helper_template_build`: "
        "the Disk Mounts share helper template (ADR 0025)."
    )
    storage: str = Field(
        pattern=_STORAGE_ID_PATTERN,
        max_length=64,
        description="Target storage for the template's disk and cloud-init drive (content `images`, on the agent's "
        "node).",
    )
    vmid: int | None = Field(
        default=None,
        ge=100,
        le=999999999,
        description="VMID for the template; omitted = PVE's next free VMID.",
    )
    image_source: Literal["download", "node_image"] = Field(
        default="download",
        description="`download`: the node downloads the Debian 13 genericcloud image from cloud.debian.org. "
        "`node_image`: use an image already on the node (`node_image`).",
    )
    node_image: str | None = Field(
        default=None,
        max_length=255,
        description="With image_source `node_image`: absolute path, or a PVE volume with content `import`.",
    )


class LabAgentSetupJobOut(BaseModel):
    """What the agent sees of a queued job: structured settings only."""

    id: UUID = Field(description="Job id.")
    kind: Literal["template_build", "share_helper_template_build"] = Field(description="Which template to build.")
    node: str = Field(description="The node it must run on (the agent refuses a job for another node).")
    spec: dict = Field(
        description="Validated by the agent: `vmid`, `storage`, `snippet_storage`, `image_source`, `node_image`, "
        "`uplink` {bridge, vlan_tag, ip_mode, range_start, range_end, prefix_length, gateway, dns_servers}, "
        "`uplink_address`, `release_version`."
    )


class AgentStepIn(BaseModel):
    key: str = Field(max_length=40, description="Step id, as in LabSetupJobStepOut.")
    status: Literal["running", "ok", "warning", "failed", "skipped"] = Field(description="Step state.")
    message: str = Field(max_length=2000, description="What happened.")
    output: str | None = Field(default=None, max_length=8000, description="Tail of the command output.")
    started_at: datetime = Field(description="Step start (UTC).")
    finished_at: datetime | None = Field(default=None, description="Step end (UTC).")


class AgentSetupProgress(BaseModel):
    steps: list[AgentStepIn] = Field(max_length=100, description="The full step log so far.")
    result: dict = Field(default_factory=dict, description="Facts so far (`template_vmid`, `prober_version`, ...).")


class AgentSetupStatus(AgentSetupProgress):
    status: Literal["succeeded", "failed"] = Field(description="Outcome.")
    error_code: str | None = Field(default=None, max_length=64, description="Why it failed.")
    error_message: str | None = Field(default=None, max_length=2000, description="The failure in English.")


class AgentSetupArtifactOut(BaseModel):
    name: str = Field(description="Package name.")
    filename: str = Field(description="The .deb's file name.")
    version: str = Field(description="Package version.")
    sha256: str = Field(description="SHA-256 the agent checks after downloading `/file`.")


__all__ = [
    "AgentSetupArtifactOut",
    "AgentSetupProgress",
    "AgentSetupStatus",
    "AgentStepIn",
    "LabAgentSetupJobCreate",
    "LabAgentSetupJobOut",
    "LabNodePairingCreate",
    "LabNodePairingOut",
    "LabSetupJobStepOut",
    "PairRequest",
]
