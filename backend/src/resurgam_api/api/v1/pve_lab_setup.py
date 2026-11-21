"""Recovery Lab compute setup wizard endpoints under `/pve-clusters/{id}`:
the readiness checklist, the pickers it offers instead of free-text
fields, two safe auto-fixes, and the lab agent's install instructions.

Kept out of `pve_clusters.py` (already ~1000 lines); shares its cluster
lookup, credential and audit helpers.
"""

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from resurgam_api.api.v1._connection_helpers import upstream_error
from resurgam_api.api.v1.pve_clusters import _cluster_trust_and_token, _get_cluster_or_404, _record_audit
from resurgam_api.auth.dependency import require_role
from resurgam_api.config import Settings, get_settings
from resurgam_api.connectivity.pve_api import list_nodes
from resurgam_api.connectivity.pve_lab_api import (
    SNIPPET_CAPABLE_STORAGE_TYPES,
    PveApiError,
    add_storage_content,
    create_dir_storage,
    get_storage_config,
    list_guest_resources,
    list_node_bridges,
    list_sdn_zones,
    list_storage_config,
)
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.lab_setup.agent_install import (
    LAB_AGENT_ENV_FILE_PATH,
    LAB_AGENT_TOKEN_PLACEHOLDER,
    render_lab_agent_env_file,
)
from resurgam_api.lab_setup.artifacts import LAB_AGENT_ARTIFACT, try_load_manifest
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.pve import PveCluster
from resurgam_api.restore_test.lab_readiness import (
    DEFAULT_OVERLAY_DIR,
    DEFAULT_OVERLAY_STORAGE_ID,
    LAB_USABLE_ZONE_TYPES,
    ReadinessItem,
    ReadinessResult,
    run_lab_readiness,
)
from resurgam_api.schemas.lab_setup import (
    AddStorageContentRequest,
    CreateOverlayStorageRequest,
    LabAgentInstallOut,
    LabReadinessItemOut,
    LabReadinessOut,
    LabSetupFixOut,
    PveBridgeOut,
    PveSdnZoneOut,
    PveStorageConfigOut,
    PveTemplateOut,
)
from resurgam_api.version import running_version

router = APIRouter(prefix="/pve-clusters", tags=["pve-clusters"])

# Served by `api/v1/pve_lab_provisioning.py` (path relative to /api/v1).
DOWNLOADS_PATH = "/pve-clusters/lab-setup/downloads"

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)

LAB_AGENT_PACKAGE_NAME = LAB_AGENT_ARTIFACT


def fallback_lab_agent_package_version() -> str:
    """The package version to name when no built-package manifest is present
    (dev setups). The image ships the built packages and their manifest
    (`lab_setup/artifacts.py`), whose version wins whenever it exists. The
    packages' upstream version always equals the release VERSION (enforced by
    the image build, build-bundle.sh and a test), so it's derived from it
    rather than maintained by hand; Debian revision 1 is the norm."""
    return f"{running_version()}-1"


_NODES_QUERY = Query(
    default=None,
    description="Lab node(s) to check per-node items on (repeat the parameter for several nodes). Omitted: the "
    "overlay storage's own node restriction, else every online node.",
)
_OVERLAY_QUERY = Query(
    default=DEFAULT_OVERLAY_STORAGE_ID,
    pattern=r"^[A-Za-z][A-Za-z0-9\-_.]*[A-Za-z0-9]$",
    description="The lab agent's overlay storage id (its RESURGAM_LAB_AGENT_OVERLAY_STORAGE_ID).",
)


def _item_out(item: ReadinessItem) -> LabReadinessItemOut:
    return LabReadinessItemOut(
        key=item.key,
        status=item.status,
        code=item.code,
        message=item.message,
        fix_hint=item.fix_hint,
        auto_fix=item.auto_fix,
        details=item.details,
    )


def _readiness_out(cluster: PveCluster, result: ReadinessResult) -> LabReadinessOut:
    return LabReadinessOut(
        cluster_id=cluster.id,
        ready=result.ready,
        lab_nodes=result.lab_nodes,
        lab_nodes_source=result.lab_nodes_source,
        overlay_storage_id=result.overlay_storage_id,
        checked_at=result.checked_at,
        items=[_item_out(i) for i in result.items],
    )


@router.get("/{cluster_id}/lab-readiness", response_model=LabReadinessOut)
def get_lab_readiness(
    cluster_id: uuid.UUID,
    nodes: list[str] | None = _NODES_QUERY,
    overlay_storage_id: str = _OVERLAY_QUERY,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> LabReadinessOut:
    """Ordered Recovery Lab compute checklist for this cluster, checked live
    with RESURGAM's stored PVE token: (a) token privileges the lab needs,
    (b) SDN zone usable for isolated VNets (only Simple zones -- or none, then
    RESURGAM creates its own Simple zone `resurgam`), (c) uplink bridge on the
    lab node(s), (d) uplink network: VLAN tag carried by that bridge, static
    range valid / not exhausted / not overlapping an Application Group's lab
    subnet, (e) snippet storage with `snippets` content, (f) the agent's
    overlay `dir` storage with `images` content on the lab node(s), (g) probe
    appliance template (and whether its resurgam-lab-prober version matches
    the one this RESURGAM ships), (h) lab agent checked in within 5 minutes,
    (i) RECOVERY_LAB_PUBLIC_BACKEND_URL set. Read-only; always HTTP 200 -- a
    failing PVE call turns only the dependent items into `unknown`.
    """
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    result = run_lab_readiness(
        cluster, trust, token, settings, nodes=nodes, overlay_storage_id=overlay_storage_id, db=db
    )
    return _readiness_out(cluster, result)


@router.get("/{cluster_id}/sdn-zones", response_model=list[PveSdnZoneOut])
def list_pve_cluster_sdn_zones(
    cluster_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> list[PveSdnZoneOut]:
    """Live list of the cluster's SDN zones (`GET /cluster/sdn/zones`) for the
    wizard's zone picker, each flagged `usable_for_lab`."""
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    try:
        zones = list_sdn_zones(cluster.host, cluster.port, trust, token, settings.connectivity_timeout_seconds)
    except PveApiError as exc:
        raise upstream_error("pve", str(exc)) from exc
    return [
        PveSdnZoneOut(
            zone=z.zone, type=z.type, nodes=z.nodes, pending=z.pending, usable_for_lab=z.type in LAB_USABLE_ZONE_TYPES
        )
        for z in sorted(zones, key=lambda z: z.zone)
    ]


@router.get("/{cluster_id}/nodes/{node}/bridges", response_model=list[PveBridgeOut])
def list_pve_cluster_node_bridges(
    cluster_id: uuid.UUID,
    node: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> list[PveBridgeOut]:
    """Live list of one node's Linux/OVS bridges (`GET /nodes/{node}/network
    ?type=any_local_bridge` -- never SDN VNets) for the uplink-bridge picker."""
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    try:
        bridges = list_node_bridges(
            cluster.host, cluster.port, trust, token, settings.connectivity_timeout_seconds, node=node
        )
    except PveApiError as exc:
        raise upstream_error("pve", str(exc)) from exc
    return [PveBridgeOut(**vars(b)) for b in bridges]


@router.get("/{cluster_id}/storage-config", response_model=list[PveStorageConfigOut])
def list_pve_cluster_storage_config(
    cluster_id: uuid.UUID,
    content: str | None = Query(
        default=None,
        description="Only storages whose content types include this one (e.g. `snippets`, `images`).",
    ),
    snippets_capable: bool = Query(
        default=False, description="Only storages whose type can hold snippets (dir, nfs, cifs, cephfs, btrfs)."
    ),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> list[PveStorageConfigOut]:
    """Live, cluster-wide storage.cfg (`GET /storage`) with content types and
    node restrictions -- the snippet-storage picker (and the overlay-storage
    check). Distinct from `/nodes/{node}/storages`, which is one node's live
    usage view."""
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    try:
        storages = list_storage_config(cluster.host, cluster.port, trust, token, settings.connectivity_timeout_seconds)
    except PveApiError as exc:
        raise upstream_error("pve", str(exc)) from exc
    out = []
    for s in sorted(storages, key=lambda s: s.storage):
        capable = s.type in SNIPPET_CAPABLE_STORAGE_TYPES
        if content is not None and content not in s.content:
            continue
        if snippets_capable and not capable:
            continue
        out.append(
            PveStorageConfigOut(
                storage=s.storage,
                type=s.type,
                content=s.content,
                nodes=s.nodes,
                path=s.path,
                shared=s.shared,
                disabled=s.disabled,
                snippets_capable=capable,
            )
        )
    return out


@router.get("/{cluster_id}/templates", response_model=list[PveTemplateOut])
def list_pve_cluster_templates(
    cluster_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> list[PveTemplateOut]:
    """Live list of every guest template on the cluster
    (`GET /cluster/resources?type=vm`, `template=1`) for the probe-appliance
    template picker."""
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    try:
        guests = list_guest_resources(cluster.host, cluster.port, trust, token, settings.connectivity_timeout_seconds)
    except PveApiError as exc:
        raise upstream_error("pve", str(exc)) from exc
    return [
        PveTemplateOut(vmid=g.vmid, name=g.name, node=g.node, guest_type=g.guest_type)
        for g in sorted(guests, key=lambda g: g.vmid)
        if g.template
    ]


def _recheck(
    cluster: PveCluster,
    settings: Settings,
    trust,
    token,
    *,
    key: str,
    nodes: list[str] | None,
    overlay_storage_id: str = DEFAULT_OVERLAY_STORAGE_ID,
    snippet_storage_id: str | None = None,
) -> LabReadinessItemOut:
    result = run_lab_readiness(
        cluster,
        trust,
        token,
        settings,
        nodes=nodes,
        overlay_storage_id=overlay_storage_id,
        snippet_storage_id=snippet_storage_id,
        keys=frozenset({key}),
    )
    return _item_out(result.items[0])


@router.post("/{cluster_id}/lab-setup/overlay-storage", response_model=LabSetupFixOut)
def create_lab_overlay_storage(
    cluster_id: uuid.UUID,
    body: CreateOverlayStorageRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> LabSetupFixOut:
    """Auto-fix: creates the lab agent's overlay storage on PVE
    (`POST /storage`, type `dir`, the given path, content `images`, restricted
    to the given lab nodes). Needs `Datastore.Allocate` on `/storage`. Never
    touches an existing storage: 409 if the id is taken. Audited; the
    response carries the re-checked `overlay_storage` readiness item. Undo:
    remove the storage in PVE (Datacenter > Storage > Remove) -- the
    directory and its files are left on the nodes."""
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    timeout = settings.connectivity_timeout_seconds

    try:
        existing = list_storage_config(cluster.host, cluster.port, trust, token, timeout)
    except PveApiError as exc:
        raise upstream_error("pve", str(exc)) from exc
    if any(s.storage == body.storage_id for s in existing):
        raise ResurgamApiError(
            code="storage_exists",
            message=f"A storage named '{body.storage_id}' already exists on this cluster; RESURGAM never changes an "
            "existing storage here. Fix it in PVE, or choose another overlay storage id (and set the agent's "
            "RESURGAM_LAB_AGENT_OVERLAY_STORAGE_ID to match).",
            status_code=409,
        )

    node_result = list_nodes(cluster.host, cluster.port, trust, token, timeout=timeout)
    if not node_result.reachable:
        raise upstream_error("pve", node_result.error)
    known = {n.node for n in node_result.nodes}
    unknown = [n for n in body.nodes if n not in known]
    if unknown:
        raise ResurgamApiError(
            code="unknown_nodes",
            message=f"Node(s) {unknown} are not part of this cluster. Pick from: {sorted(known)}.",
            status_code=400,
            details={"unknown_nodes": unknown, "known_nodes": sorted(known)},
        )

    try:
        create_dir_storage(
            cluster.host,
            cluster.port,
            trust,
            token,
            timeout,
            storage_id=body.storage_id,
            path=body.path,
            nodes=body.nodes,
            content="images",
        )
    except PveApiError as exc:
        raise ResurgamApiError(
            code="pve_write_failed",
            message=f"PVE refused to create the storage: {exc}. The token needs Datastore.Allocate on /storage.",
            status_code=502,
        ) from exc

    _record_audit(
        db,
        request,
        actor_user_id=user.user_id,
        action="pve_cluster_lab_overlay_storage_created",
        target_id=cluster.id,
        detail={"storage_id": body.storage_id, "path": body.path, "nodes": body.nodes, "content": ["images"]},
    )
    db.commit()

    item = _recheck(
        cluster, settings, trust, token, key="overlay_storage", nodes=body.nodes, overlay_storage_id=body.storage_id
    )
    return LabSetupFixOut(action="create_overlay_storage", storage_id=body.storage_id, content=["images"], item=item)


@router.post("/{cluster_id}/lab-setup/storage-content", response_model=LabSetupFixOut)
def add_lab_storage_content(
    cluster_id: uuid.UUID,
    body: AddStorageContentRequest,
    request: Request,
    nodes: list[str] | None = _NODES_QUERY,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> LabSetupFixOut:
    """Auto-fix: adds `snippets` (snippet storage) or `images` (overlay
    storage) to an existing storage's content types (`PUT /storage/{id}`),
    sending every existing content type back unchanged and PVE's `digest`
    so a concurrent edit is rejected rather than overwritten. Needs
    `Datastore.Allocate` on `/storage`. A no-op if the type is already
    there. Audited (before/after content); the response carries the
    re-checked readiness item. Undo: remove the content type again in PVE
    (Datacenter > Storage > Edit > Content)."""
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    timeout = settings.connectivity_timeout_seconds

    try:
        storage = get_storage_config(cluster.host, cluster.port, trust, token, timeout, storage_id=body.storage_id)
    except PveApiError as exc:
        raise upstream_error("pve", str(exc)) from exc
    if storage is None:
        raise ResurgamApiError(
            code="storage_not_found",
            message=f"Storage '{body.storage_id}' does not exist on this cluster. Pick an existing storage.",
            status_code=404,
        )
    if body.content_type == "snippets" and storage.type not in SNIPPET_CAPABLE_STORAGE_TYPES:
        raise ResurgamApiError(
            code="storage_type_unsupported",
            message=f"Storage '{body.storage_id}' is of type '{storage.type}', which cannot hold snippets. Pick a "
            "dir, nfs, cifs, cephfs or btrfs storage.",
            status_code=400,
        )
    if not storage.content:
        # Never guess: sending a content list without the current types
        # would silently drop them.
        raise ResurgamApiError(
            code="storage_content_unknown",
            message=f"PVE reported no content types for '{body.storage_id}', so RESURGAM cannot add one without "
            "risking dropping the existing ones. Add it in PVE (Datacenter > Storage > Edit > Content).",
            status_code=409,
        )

    before = list(storage.content)
    after = before
    if body.content_type not in before:
        try:
            after = add_storage_content(
                cluster.host, cluster.port, trust, token, timeout, storage=storage, content_type=body.content_type
            )
        except PveApiError as exc:
            raise ResurgamApiError(
                code="pve_write_failed",
                message=f"PVE refused the change: {exc}. The token needs Datastore.Allocate on /storage; if the "
                "storage was edited at the same moment, just retry.",
                status_code=502,
            ) from exc
        _record_audit(
            db,
            request,
            actor_user_id=user.user_id,
            action="pve_cluster_lab_storage_content_added",
            target_id=cluster.id,
            detail={
                "storage_id": body.storage_id,
                "content_type": body.content_type,
                "content_before": before,
                "content_after": after,
            },
        )
        db.commit()

    if body.content_type == "snippets":
        item = _recheck(
            cluster, settings, trust, token, key="snippet_storage", nodes=nodes, snippet_storage_id=body.storage_id
        )
    else:
        item = _recheck(
            cluster, settings, trust, token, key="overlay_storage", nodes=nodes, overlay_storage_id=body.storage_id
        )
    return LabSetupFixOut(action="add_storage_content", storage_id=body.storage_id, content=after, item=item)


@router.get("/{cluster_id}/lab-setup/agent-install", response_model=LabAgentInstallOut)
def get_lab_agent_install(
    cluster_id: uuid.UUID,
    overlay_storage_id: str = _OVERLAY_QUERY,
    overlay_dir: str = Query(
        default=DEFAULT_OVERLAY_DIR,
        pattern=r"^/[A-Za-z0-9_.\-/]+$",
        description="The overlay storage's path on the lab nodes (the env file's RESURGAM_LAB_AGENT_OVERLAY_DIR).",
    ),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> LabAgentInstallOut:
    """How to build and install `resurgam-lab-agent` on a lab node, and the
    env file with backend URL and overlay storage filled in. Static (no PVE
    call). The token is not included: generate it with
    `POST /pve-clusters/{id}/lab-agent-token` and substitute
    `token_placeholder`."""
    cluster = _get_cluster_or_404(db, cluster_id)
    backend_url = settings.recovery_lab_public_backend_url or None
    manifest = try_load_manifest(settings.lab_artifacts_dir)
    shipped = manifest.artifacts.get(LAB_AGENT_ARTIFACT) if manifest is not None else None
    version = shipped.version if shipped is not None else fallback_lab_agent_package_version()
    deb = shipped.filename if shipped is not None else f"{LAB_AGENT_PACKAGE_NAME}_{version}_amd64.deb"
    if shipped is not None:
        package_source = (
            f"Shipped with this RESURGAM: download it from {DOWNLOADS_PATH}/{LAB_AGENT_ARTIFACT} (SHA-256 "
            f"{shipped.sha256}), built for Debian '{manifest.debian_release}' -- the PVE node must run the same "
            "Debian release (PVE 9 = Debian 13 trixie), since the bundled virtualenv uses the node's system Python "
            "(ADR 0020)."
        )
    else:
        package_source = (
            "This installation ships no prebuilt package (only the appliance image does): build the .deb from "
            "restore-worker/ on a Debian host of the same release as the PVE node (PVE 9 = Debian 13 trixie, "
            "PVE 8 = Debian 12 bookworm) -- the bundled virtualenv uses the target's system Python (ADR 0020)."
        )
    return LabAgentInstallOut(
        package_name=LAB_AGENT_PACKAGE_NAME,
        package_version=version,
        package_source=package_source,
        package_download_path=f"{DOWNLOADS_PATH}/{LAB_AGENT_ARTIFACT}" if shipped is not None else None,
        package_sha256=shipped.sha256 if shipped is not None else None,
        package_filename=deb,
        build_steps=[]
        if shipped is not None
        else [
            "apt-get install -y debhelper dh-virtualenv virtualenv python3-dev python3-venv",
            "cd resurgam/restore-worker",
            "dpkg-buildpackage -us -uc -b",
            f"ls ../{deb}",
        ],
        install_steps=[
            *([f"echo '{shipped.sha256}  {deb}' | sha256sum -c -"] if shipped is not None else []),
            f"scp {deb} root@<lab-node>:/root/",
            f"apt install /root/{deb}",
            f"nano {LAB_AGENT_ENV_FILE_PATH}   # paste the env file below, fill in the PBS token",
            f"chmod 600 {LAB_AGENT_ENV_FILE_PATH}",
            "systemctl enable resurgam-lab-agent && systemctl restart resurgam-lab-agent",
            "journalctl -u resurgam-lab-agent -f",
        ],
        env_file_path=LAB_AGENT_ENV_FILE_PATH,
        env_file=render_lab_agent_env_file(
            cluster_name=cluster.display_name,
            backend_url=backend_url,
            overlay_storage_id=overlay_storage_id,
            overlay_dir=overlay_dir,
        ),
        token_placeholder=LAB_AGENT_TOKEN_PLACEHOLDER,
        backend_url=backend_url,
        overlay_storage_id=overlay_storage_id,
        overlay_dir=overlay_dir,
    )
