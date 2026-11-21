"""Schemas for the Recovery Lab compute setup wizard
(`/pve-clusters/{id}/lab-readiness`, the pickers and the auto-fixes)."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints

from resurgam_api.restore_test.lab_readiness import DEFAULT_OVERLAY_DIR, DEFAULT_OVERLAY_STORAGE_ID

ReadinessStatus = Literal["ok", "missing", "warning", "unknown"]
ReadinessKey = Literal[
    "privileges",
    "sdn_zone",
    "uplink_bridge",
    "snippet_storage",
    "overlay_storage",
    "appliance_template",
    "lab_agent",
    "backend_url",
    "uplink_network",
]

# PVE storage ids: letter first, then letters, digits, '-', '_', '.', ending in
# a letter or digit, case-insensitive (`pve-storage-id` format,
# PVE::JSONSchema `parse_storage_id`: m/^[a-z][a-z0-9\-\_\.]*[a-z0-9]$/i).
_STORAGE_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9\-_.]*[A-Za-z0-9]$"
# PVE node names (`pve-node` format: a DNS host-name label).
PveNodeName = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9]([A-Za-z0-9\-]*[A-Za-z0-9])?$", max_length=63),
]


class LabReadinessItemOut(BaseModel):
    key: ReadinessKey = Field(
        description="Which prerequisite this is, in checklist order: `privileges` (token privileges the lab "
        "needs), `sdn_zone`, `uplink_bridge`, `uplink_network` (VLAN tag + IP mode of the appliance's uplink), "
        "`snippet_storage`, `overlay_storage`, `appliance_template` (incl. whether its prober version matches this "
        "RESURGAM's), `lab_agent` (agent checked in recently), `backend_url` (RECOVERY_LAB_PUBLIC_BACKEND_URL set)."
    )
    status: ReadinessStatus = Field(
        description="`ok`: fine. `missing`: blocks lab runs, must be fixed. `warning`: runs may work, but read the "
        "message. `unknown`: the check itself failed (PVE call error in `details.error`)."
    )
    code: str = Field(
        description="Machine-readable reason within the item, e.g. `not_configured`, `storage_not_found`, "
        "`snippets_content_missing`, `zone_type_unsupported`, `never_seen`, `pve_error` -- the UI translates by "
        "`key` + `code`."
    )
    message: str = Field(description="What was found, in English (the UI shows its own translation by `code`).")
    fix_hint: str | None = Field(default=None, description="How to fix it, in English; `null` when `status` is ok.")
    auto_fix: Literal["create_overlay_storage", "add_snippets_content", "add_images_content"] | None = Field(
        default=None,
        description="A safe fix RESURGAM can apply itself for this item: `create_overlay_storage` -> `POST "
        "/pve-clusters/{id}/lab-setup/overlay-storage`; `add_snippets_content`/`add_images_content` -> `POST "
        "/pve-clusters/{id}/lab-setup/storage-content`. `null` if the operator must fix it by hand.",
    )
    details: dict = Field(
        default_factory=dict,
        description="Structured facts behind the message (e.g. `missing` privileges, `missing_nodes`, `storage`, "
        "`content`, `zone`/`type`, `last_seen_at`, `error`).",
    )


class LabReadinessOut(BaseModel):
    """Ordered Lab Compute checklist for one PVE cluster. Each item is
    checked independently -- one failing PVE call only turns the items that
    depend on it into `unknown`."""

    cluster_id: UUID = Field(description="The PVE cluster checked.")
    ready: bool = Field(description="True when no item is `missing` or `unknown` (warnings allowed).")
    lab_nodes: list[str] = Field(description="The node(s) per-node items (bridge, storages, template) were checked on.")
    lab_nodes_source: Literal["query", "overlay_storage", "all_online_nodes"] = Field(
        description="Where `lab_nodes` came from: the `nodes` query parameter, the overlay storage's own node "
        "restriction (the wizard creates it restricted to the lab nodes), or every online node."
    )
    overlay_storage_id: str = Field(description="The overlay storage id checked (the agent's configured id).")
    checked_at: datetime = Field(description="When the checks ran (UTC).")
    items: list[LabReadinessItemOut] = Field(description="The checklist, in fixed order.")


class PveSdnZoneOut(BaseModel):
    zone: str = Field(description="SDN zone id.")
    type: str | None = Field(description="Zone type: simple, vlan, qinq, vxlan, evpn or faucet.")
    nodes: list[str] | None = Field(description="Nodes the zone is restricted to; `null` = all nodes.")
    pending: bool = Field(description="True if the zone has SDN changes not applied yet.")
    usable_for_lab: bool = Field(
        description="True if lab runs can create their untagged per-run VNet in this zone (only `simple` zones: "
        "PVE requires a VNet tag in VLAN/QinQ/VXLAN/EVPN zones)."
    )


class PveBridgeOut(BaseModel):
    iface: str = Field(description="Bridge name, e.g. `vmbr0`.")
    type: str = Field(description="`bridge` (Linux bridge) or `OVSBridge`.")
    active: bool | None = Field(description="Whether the bridge is up on the node.")
    vlan_aware: bool | None = Field(description="Whether the Linux bridge is VLAN-aware.")
    comments: str | None = Field(description="The bridge's comment in PVE, if any.")
    vlan_ids: str | None = Field(
        default=None,
        description="Allowed VLANs of a VLAN-aware Linux bridge (`bridge_vids`, e.g. `2-4094`); `null` if PVE "
        "reports none.",
    )


class PveStorageConfigOut(BaseModel):
    storage: str = Field(description="Storage id.")
    type: str = Field(description="Storage type (dir, nfs, lvmthin, zfspool, pbs, ...).")
    content: list[str] = Field(description="Allowed content types (images, rootdir, snippets, iso, ...).")
    nodes: list[str] | None = Field(description="Nodes the storage is restricted to; `null` = all nodes.")
    path: str | None = Field(description="Filesystem path for file-based storages.")
    shared: bool = Field(description="Marked as shared storage.")
    disabled: bool = Field(description="Disabled in storage.cfg.")
    snippets_capable: bool = Field(
        description="Whether the storage type can hold snippets at all (dir, nfs, cifs, cephfs, btrfs) -- the "
        "`add_snippets_content` auto-fix is only offered for these."
    )


class PveTemplateOut(BaseModel):
    vmid: int = Field(description="Template VMID.")
    name: str | None = Field(description="Template name.")
    node: str = Field(description="Node the template lives on (clones are requested on this node).")
    guest_type: str = Field(description="`qemu` or `lxc`; the probe appliance must be `qemu`.")


class CreateOverlayStorageRequest(BaseModel):
    """Creates the lab agent's overlay storage: `POST /storage` with type
    `dir`, content `images`, restricted to the lab nodes. Refused (409) if
    the storage id already exists -- nothing existing is ever changed."""

    storage_id: str = Field(
        default=DEFAULT_OVERLAY_STORAGE_ID,
        pattern=_STORAGE_ID_PATTERN,
        max_length=64,
        description="New storage id; must match the agent's RESURGAM_LAB_AGENT_OVERLAY_STORAGE_ID (default "
        f"`{DEFAULT_OVERLAY_STORAGE_ID}`). Letter first, then letters, digits, `-`, `_`, `.`.",
    )
    path: str = Field(
        default=DEFAULT_OVERLAY_DIR,
        pattern=r"^/[A-Za-z0-9_.\-/]+$",
        max_length=255,
        description="Absolute directory on each lab node; must match the agent's RESURGAM_LAB_AGENT_OVERLAY_DIR "
        f"(default `{DEFAULT_OVERLAY_DIR}`, created by the resurgam-lab-agent package; PVE creates it too if "
        "missing).",
    )
    nodes: list[PveNodeName] = Field(
        min_length=1,
        description="Lab node(s) to restrict the storage to (an overlay is local to the node whose agent wrote it).",
    )


class AddStorageContentRequest(BaseModel):
    """Adds one content type to an existing storage (`PUT /storage/{id}`),
    keeping every existing content type."""

    storage_id: str = Field(
        pattern=_STORAGE_ID_PATTERN, max_length=64, description="Existing storage id, e.g. `local`."
    )
    content_type: Literal["snippets", "images"] = Field(
        description="`snippets` for the snippet storage (only on dir/nfs/cifs/cephfs/btrfs), `images` for the "
        "overlay storage."
    )


class LabSetupFixOut(BaseModel):
    """Result of an auto-fix: what was sent to PVE, then the re-run
    readiness item that the fix was meant to resolve."""

    action: str = Field(description="`create_overlay_storage` or `add_storage_content`.")
    storage_id: str = Field(description="The storage created or changed.")
    content: list[str] = Field(description="The storage's content types as sent to PVE.")
    item: LabReadinessItemOut = Field(description="The re-checked readiness item (overlay_storage/snippet_storage).")


class LabAgentInstallOut(BaseModel):
    """Everything needed to install `resurgam-lab-agent` on a lab node.
    The agent token itself is never part of this response: generate it with
    `POST /pve-clusters/{id}/lab-agent-token` (shown once) and put it in
    place of `token_placeholder` in `env_file`."""

    package_name: str = Field(description="Debian package name (`resurgam-lab-agent`).")
    package_version: str = Field(description="Package version built from this source tree (debian/changelog).")
    package_source: str = Field(
        description="Where the .deb comes from: shipped with this RESURGAM (downloadable from "
        "`package_download_path`), or -- outside the appliance image -- built from `restore-worker/` on a Debian "
        "host matching the PVE node's release."
    )
    package_download_path: str | None = Field(
        default=None,
        description="API path (below /api/v1) serving the shipped .deb (platform admin); `null` when this "
        "installation ships none -- then follow `build_steps`.",
    )
    package_sha256: str | None = Field(default=None, description="SHA-256 of the shipped .deb; `null` if none.")
    package_filename: str = Field(description="File name of the .deb, e.g. `resurgam-lab-agent_0.2.1-1_amd64.deb`.")
    build_steps: list[str] = Field(
        description="Shell commands to build the .deb on a Debian build host -- empty when the package is shipped."
    )
    install_steps: list[str] = Field(description="Shell commands to run as root on each lab node.")
    env_file_path: str = Field(description="Path of the agent's environment file on the node.")
    env_file: str = Field(description="Full env file content; replace `token_placeholder` with the agent token.")
    token_placeholder: str = Field(description="The placeholder string in `env_file` that stands for the token.")
    backend_url: str | None = Field(
        description="RECOVERY_LAB_PUBLIC_BACKEND_URL used in the env file (`null` if unset)."
    )
    overlay_storage_id: str = Field(description="Overlay storage id written to the env file.")
    overlay_dir: str = Field(description="Overlay directory written to the env file.")
