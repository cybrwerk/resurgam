"""Lab node pairing and agent-run template builds (docs/adr/0027).

Three audiences:

- the admin (`platform_admin`): create a one-time pairing code for a node and
  watch it get used; queue a template build for the paired agent;
- the node, unauthenticated: download the pairing script by the pairing's
  id (`/lab-setup/bootstrap/p/{pairing_id}/script` -- no secret in it, the
  one download made before the node trusts RESURGAM's certificate), then,
  over verified TLS and authenticated by the code in the URL
  (`/lab-setup/bootstrap/{code}/...`), the agent package and the exchange of
  the code -- once -- for the agent's env file;
- the agent (its cluster token): pick up template builds, report their step
  log, fetch the prober package (`/lab-agent/setup-*`).
"""

import ipaddress
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, Path, Request
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy.orm import Session

from resurgam_api.api.v1.lab_agent import _require_lab_agent
from resurgam_api.api.v1.pve_clusters import _cluster_trust_and_token, _get_cluster_or_404
from resurgam_api.api.v1.pve_lab_provisioning import (
    _audit,
    _check_target,
    _ensure_no_running_job,
    _template_prerequisites,
)
from resurgam_api.auth.dependency import require_role
from resurgam_api.config import Settings, get_settings
from resurgam_api.connectivity.pve_api import list_nodes
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.lab_setup import pairing
from resurgam_api.lab_setup.artifacts import (
    LAB_AGENT_ARTIFACT,
    LAB_PROBER_ARTIFACT,
    require_artifact,
    require_manifest,
)
from resurgam_api.lab_setup.template_build import IMAGE_SOURCE_NODE
from resurgam_api.lab_setup.uplink import (
    PURPOSE_TEMPLATE_BUILD,
    allocate_uplink_ip,
    release_uplink_ip,
    uplink_config_from_cluster,
)
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.lab_setup import LabAgentSetupJob, LabNodePairing
from resurgam_api.models.pve import PveCluster
from resurgam_api.restore_test.execution import generate_appliance_run_token, hash_appliance_run_token
from resurgam_api.schemas.lab_pairing import (
    AgentSetupArtifactOut,
    AgentSetupProgress,
    AgentSetupStatus,
    LabAgentSetupJobCreate,
    LabAgentSetupJobOut,
    LabNodePairingCreate,
    LabNodePairingOut,
    PairRequest,
)
from resurgam_api.schemas.lab_provisioning import LabSetupJobOut, LabSetupJobStepOut
from resurgam_api.version import running_version

admin_router = APIRouter(prefix="/pve-clusters", tags=["lab-setup"])
bootstrap_router = APIRouter(prefix="/lab-setup/bootstrap", tags=["lab-setup"])
agent_router = APIRouter(prefix="/lab-agent", tags=["lab-agent"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)

# A queued job nobody claims, or a running job whose agent stopped
# reporting (it sends at least every 30 s), is failed after this.
AGENT_JOB_UNCLAIMED_TIMEOUT = timedelta(minutes=15)
AGENT_JOB_SILENT_TIMEOUT = timedelta(minutes=10)
ACTIVE_STATES = ("queued", "running")


def _now() -> datetime:
    return datetime.now(UTC)


def _api_base(settings: Settings) -> str:
    if not settings.recovery_lab_public_backend_url:
        raise ResurgamApiError(
            code="recovery_lab_backend_url_not_configured",
            message="RECOVERY_LAB_PUBLIC_BACKEND_URL is not set, so the node would not know where to reach RESURGAM. "
            "Set it for the API, restart it, then retry.",
            status_code=400,
        )
    return settings.recovery_lab_public_backend_url.rstrip("/")


def _is_loopback_host(host: str) -> bool:
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _pairing_api_base(settings: Settings) -> str:
    """The backend URL the node calls -- refused when it points at the
    loopback interface, which on the node would be the node itself."""
    api_base = _api_base(settings)
    if _is_loopback_host(urlsplit(api_base).hostname or ""):
        raise ResurgamApiError(
            code="recovery_lab_backend_url_loopback",
            message=f"RECOVERY_LAB_PUBLIC_BACKEND_URL is {api_base}, a loopback address: on the node that would be "
            "the node itself. Set it to an address of RESURGAM the node can reach, restart the API, then retry.",
            status_code=400,
        )
    return api_base


def _check_node_in_cluster(db: Session, settings: Settings, cluster: PveCluster, node: str) -> None:
    """Refuses a node the cluster does not have. Skipped when PVE is not
    reachable right now -- the script still refuses to run on another host."""
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    nodes = list_nodes(cluster.host, cluster.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    if not nodes.reachable:
        return
    known = sorted(n.node for n in nodes.nodes)
    if node not in known:
        raise ResurgamApiError(
            code="unknown_nodes",
            message=f"Node '{node}' is not part of this cluster. Pick from: {known}.",
            status_code=400,
        )


def _pairing_state(row: LabNodePairing) -> str:
    if row.consumed_at is not None:
        return "paired"
    if row.revoked_at is not None:
        return "revoked"
    if row.expires_at <= _now():
        return "expired"
    return "waiting"


def _pairing_out(row: LabNodePairing, cluster: PveCluster, *, code: str | None = None, command: str | None = None):
    return LabNodePairingOut(
        id=row.id,
        node=row.node,
        code=code,
        command=command,
        script_sha256=row.script_sha256,
        shell_url=pairing.node_shell_url(cluster.host, cluster.port, row.node),
        expires_at=row.expires_at,
        state=_pairing_state(row),
        consumed_at=row.consumed_at,
        pbs_token_provided=row.pbs_token_provided,
        agent_last_seen_at=cluster.lab_agent_last_seen_at,
    )


# --- admin: pairing ---------------------------------------------------------------------


@admin_router.post("/{cluster_id}/lab-setup/pairings", response_model=LabNodePairingOut, status_code=201)
def create_pairing(
    cluster_id: uuid.UUID,
    body: LabNodePairingCreate,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> LabNodePairingOut:
    """`platform_admin` only. Creates a one-time code that pairs `node` as this
    cluster's lab node and returns the command to paste into the node's root
    shell (`shell_url` opens Proxmox's own web shell for it). The command
    downloads a script by the pairing's id (no secret; its SHA-256 is pinned)
    and runs it with the code as argument: over verified TLS it installs the
    shipped resurgam-lab-agent, asks for the node's PBS token locally,
    exchanges the code for the agent's env file and starts the agent. 400
    when `node` is not part of the cluster (checked when PVE is reachable) or
    RECOVERY_LAB_PUBLIC_BACKEND_URL is unset or a loopback address. Valid for
    `LAB_PAIRING_CODE_TTL_MINUTES` (30), single use; a new code revokes older
    unused ones. When the node uses it, the cluster's agent token is replaced
    (an agent elsewhere stops working). The code is shown only here; only its
    SHA-256 is stored. Audited."""
    cluster = _get_cluster_or_404(db, cluster_id)
    api_base = _pairing_api_base(settings)
    manifest = require_manifest(settings.lab_artifacts_dir)
    agent = require_artifact(settings.lab_artifacts_dir, LAB_AGENT_ARTIFACT)
    _check_node_in_cluster(db, settings, cluster, body.node)
    now = _now()
    for old in (
        db.query(LabNodePairing)
        .filter(
            LabNodePairing.pve_cluster_id == cluster.id,
            LabNodePairing.consumed_at.is_(None),
            LabNodePairing.revoked_at.is_(None),
        )
        .all()
    ):
        old.revoked_at = now
    code = pairing.new_code()
    script = pairing.render_bootstrap_script(
        api_base=api_base,
        cluster_name=cluster.display_name,
        node=body.node,
        debian_release=manifest.debian_release,
        agent=agent,
        ca_certificate=pairing.read_ca_certificate(settings.appliance_tls_ca_file),
    )
    row = LabNodePairing(
        pve_cluster_id=cluster.id,
        code_hash=pairing.hash_code(code),
        node=body.node,
        overlay_storage_id=body.overlay_storage_id,
        overlay_dir=body.overlay_dir,
        script=script.text,
        script_sha256=script.sha256,
        expires_at=now + timedelta(minutes=settings.lab_pairing_code_ttl_minutes),
        created_by_user_id=user.user_id,
    )
    db.add(row)
    db.flush()
    _audit(
        db,
        actor_user_id=user.user_id,
        correlation_id=getattr(request.state, "correlation_id", None),
        action="pve_cluster_lab_pairing_created",
        cluster_id=cluster.id,
        detail={"pairing_id": str(row.id), "node": body.node, "expires_at": row.expires_at.isoformat()},
    )
    db.commit()
    db.refresh(row)
    command = pairing.render_pair_command(api_base, row.id, code, script.sha256)
    return _pairing_out(row, cluster, code=code, command=command)


@admin_router.get("/{cluster_id}/lab-setup/pairings/{pairing_id}", response_model=LabNodePairingOut)
def get_pairing(
    cluster_id: uuid.UUID,
    pairing_id: uuid.UUID,
    db: Session = Depends(get_db),
    _user=Depends(_require_platform_admin),
) -> LabNodePairingOut:
    """`platform_admin` only. Whether the node has used the code yet (poll
    while `state` is `waiting`), and when the agent last called in. The code
    and command are not returned again."""
    cluster = _get_cluster_or_404(db, cluster_id)
    row = db.get(LabNodePairing, pairing_id)
    if row is None or row.pve_cluster_id != cluster.id:
        raise ResurgamApiError(code="not_found", message="Pairing not found.", status_code=404)
    return _pairing_out(row, cluster)


# --- the node: bootstrap ------------------------------------------------------------------


_CODE_PATH = Path(pattern=r"^[A-Za-z0-9-]{8,40}$", description="The one-time pairing code from the Lab Compute wizard.")


def _refuse_unusable(row: LabNodePairing | None) -> LabNodePairing:
    if row is None or _pairing_state(row) != "waiting":
        raise ResurgamApiError(
            code="pairing_code_invalid",
            message="This pairing code is unknown, already used or expired. Create a new one in RESURGAM "
            "(Lab Compute setup -> Pair node).",
            status_code=404,
        )
    return row


def _usable_pairing(db: Session, code: str, *, for_update: bool = False) -> LabNodePairing:
    query = db.query(LabNodePairing).filter_by(code_hash=pairing.hash_code(code))
    if for_update:
        query = query.with_for_update()
    return _refuse_unusable(query.one_or_none())


@bootstrap_router.get("/p/{pairing_id}/script", response_class=PlainTextResponse)
def bootstrap_script(
    pairing_id: Annotated[
        uuid.UUID, Path(description="The pairing's id from the pair command (not a secret, unlike the code).")
    ],
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    """The pairing script of a pairing that is still waiting (unused,
    unexpired, not replaced). Unauthenticated: it is fetched before the node
    trusts RESURGAM's certificate, so it holds no secret -- the code is its
    argument -- and the pair command checks its SHA-256 before running it.
    404 for an unknown, used, expired or replaced pairing."""
    row = _refuse_unusable(db.get(LabNodePairing, pairing_id))
    return PlainTextResponse(row.script, media_type="text/x-shellscript")


@bootstrap_router.get(
    "/{code}/agent.deb",
    response_class=FileResponse,
    responses={200: {"content": {"application/vnd.debian.binary-package": {}}}},
)
def bootstrap_agent_package(
    code: Annotated[str, _CODE_PATH],
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """The shipped resurgam-lab-agent .deb, for a valid, unused code (the
    script calls this over verified TLS only). The script checks it against
    the SHA-256 it carries."""
    _usable_pairing(db, code)
    item = require_artifact(settings.lab_artifacts_dir, LAB_AGENT_ARTIFACT)
    return FileResponse(item.path, media_type="application/vnd.debian.binary-package", filename=item.filename)


@bootstrap_router.post("/{code}/pair", response_class=PlainTextResponse)
def bootstrap_pair(
    code: Annotated[str, _CODE_PATH],
    body: PairRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PlainTextResponse:
    """Uses the code (once): generates a new agent token for the cluster, records
    the node as its lab node and returns the RESURGAM part of
    /etc/default/resurgam-lab-agent (backend URL, the token, node name, overlay
    storage). The script appends the PBS token and CA path locally. 404 for an
    unknown, used or expired code; 409 when the node is not the one the code
    was made for. Audited (never the token)."""
    row = _usable_pairing(db, code, for_update=True)
    if body.node != row.node:
        raise ResurgamApiError(
            code="pairing_node_mismatch",
            message=f"This code is for node '{row.node}', not '{body.node}'. Create a code for this node.",
            status_code=409,
        )
    cluster = db.get(PveCluster, row.pve_cluster_id)
    api_base = _api_base(settings)
    token = generate_appliance_run_token()
    previous_node = cluster.lab_agent_node
    cluster.lab_agent_token_hash = hash_appliance_run_token(token)
    cluster.lab_agent_node = row.node
    row.consumed_at = _now()
    row.pbs_token_provided = body.pbs_token_provided
    _audit(
        db,
        actor_user_id=row.created_by_user_id,
        correlation_id=None,
        action="pve_cluster_lab_agent_paired",
        cluster_id=cluster.id,
        detail={
            "pairing_id": str(row.id),
            "node": row.node,
            "previous_node": previous_node,
            "pbs_token_provided": body.pbs_token_provided,
        },
    )
    db.commit()
    env = pairing.render_paired_env(
        cluster_name=cluster.display_name,
        backend_url=api_base,
        agent_token=token,
        overlay_storage_id=row.overlay_storage_id,
        overlay_dir=row.overlay_dir,
        node=row.node,
    )
    return PlainTextResponse(env)


# --- admin: agent-run template builds ---------------------------------------------------


def _stale_reason(job: LabAgentSetupJob, now: datetime) -> tuple[str, str] | None:
    stale = None
    if job.status == "queued" and job.created_at <= now - AGENT_JOB_UNCLAIMED_TIMEOUT:
        stale = (
            "agent_not_responding",
            (
                "The lab agent did not pick up the job within 15 minutes. Is it running and paired? "
                "(systemctl status resurgam-lab-agent on the node)"
            ),
        )
    elif job.status == "running" and (job.last_progress_at or job.claimed_at or now) <= now - AGENT_JOB_SILENT_TIMEOUT:
        stale = (
            "agent_stopped_reporting",
            (
                "The lab agent stopped reporting during the build (restarted or lost its connection). Check the node "
                f"-- VM {job.spec.get('vmid')} may be left over -- and start the build again."
            ),
        )
    return stale


def _expire_if_stale(db: Session, job: LabAgentSetupJob) -> None:
    if _stale_reason(job, _now()) is None:
        return
    # Re-read under a row lock: the agent may have just claimed the job or
    # reported progress, and that must not be overwritten with "failed".
    job = db.query(LabAgentSetupJob).filter_by(id=job.id).with_for_update().populate_existing().one()
    now = _now()
    stale = _stale_reason(job, now)
    if stale is None:
        db.commit()  # releases the lock
        return
    job.status = "failed"
    job.error_code, job.error_message = stale
    job.finished_at = now
    if job.uplink_address:
        release_uplink_ip(db, cluster_id=job.pve_cluster_id, address=job.uplink_address)
    db.commit()


def active_agent_job(db: Session, cluster_id: uuid.UUID) -> LabAgentSetupJob | None:
    """The cluster's queued or running agent job, after failing stale ones."""
    for active in (
        db.query(LabAgentSetupJob)
        .filter(LabAgentSetupJob.pve_cluster_id == cluster_id, LabAgentSetupJob.status.in_(ACTIVE_STATES))
        .all()
    ):
        _expire_if_stale(db, active)
        if active.status in ACTIVE_STATES:
            return active
    return None


def agent_job_out(job: LabAgentSetupJob) -> LabSetupJobOut:
    steps = [LabSetupJobStepOut(**step) for step in job.steps or []]
    if job.status == "queued":
        steps = [
            LabSetupJobStepOut(
                key="agent",
                status="running",
                message=f"Waiting for the lab agent on {job.node} to pick up the job.",
                started_at=job.created_at,
            )
        ]
    return LabSetupJobOut(
        id=job.id,
        cluster_id=job.pve_cluster_id,
        kind=job.kind,
        node=job.node,
        status="running" if job.status in ACTIVE_STATES else job.status,
        started_at=job.created_at,
        finished_at=job.finished_at,
        steps=steps,
        result={**(job.result or {}), "uplink_address": job.uplink_address},
        error_code=job.error_code,
        error_message=job.error_message,
        runner="agent",
    )


def find_agent_job(db: Session, cluster_id: uuid.UUID, job_id: uuid.UUID) -> LabSetupJobOut | None:
    job = db.get(LabAgentSetupJob, job_id)
    if job is None or job.pve_cluster_id != cluster_id:
        return None
    _expire_if_stale(db, job)
    return agent_job_out(job)


@admin_router.post("/{cluster_id}/lab-setup/agent-jobs", response_model=LabSetupJobOut, status_code=202)
def start_agent_setup_job(
    cluster_id: uuid.UUID,
    body: LabAgentSetupJobCreate,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user=Depends(_require_platform_admin),
) -> LabSetupJobOut:
    """`platform_admin` only. Queues a template build for the cluster's paired
    lab agent -- no SSH login: the agent runs the same build as the guided SSH
    path on its own node (probe appliance template, or the Disk Mounts share
    helper template) and reports the step log; poll
    GET /pve-clusters/{id}/lab-setup/jobs/{job_id}. Needs a paired agent
    (`lab_agent_node`), the uplink bridge and snippet storage. On success the
    template becomes the cluster's lab template / share helper template. 409
    while another setup job runs. Audited: start and outcome."""
    cluster = _get_cluster_or_404(db, cluster_id)
    if not cluster.lab_agent_node or not cluster.lab_agent_token_hash:
        raise ResurgamApiError(
            code="lab_agent_not_paired",
            message="No lab agent is paired with this cluster yet. Pair a node first (Lab Compute setup -> agent).",
            status_code=409,
        )
    if body.kind == "share_helper_template_build" and not cluster.is_lab_compute:
        raise ResurgamApiError(
            code="not_lab_compute",
            message="Disk mounts run on a lab cluster: enable 'Lab compute' for this cluster first.",
            status_code=400,
        )
    _template_prerequisites(cluster)
    _ensure_no_running_job(db, cluster.id)
    release = running_version()
    if body.kind == "template_build":
        release = require_manifest(settings.lab_artifacts_dir).release_version
        require_artifact(settings.lab_artifacts_dir, LAB_PROBER_ARTIFACT)
    node = cluster.lab_agent_node
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    vmid = _check_target(cluster, settings, trust, token, node=node, storage=body.storage, vmid=body.vmid)
    uplink = uplink_config_from_cluster(cluster)
    address = allocate_uplink_ip(db, cluster, purpose=PURPOSE_TEMPLATE_BUILD)
    spec = {
        "vmid": vmid,
        "storage": body.storage,
        "snippet_storage": cluster.lab_snippet_storage_id,
        "image_source": body.image_source,
        "node_image": body.node_image if body.image_source == IMAGE_SOURCE_NODE else None,
        "uplink": {
            "bridge": uplink.bridge,
            "vlan_tag": uplink.vlan_tag,
            "ip_mode": uplink.ip_mode,
            "range_start": uplink.range_start,
            "range_end": uplink.range_end,
            "prefix_length": uplink.prefix_length,
            "gateway": uplink.gateway,
            "dns_servers": list(uplink.dns_servers),
        },
        "uplink_address": address,
        "release_version": release,
    }
    job = LabAgentSetupJob(
        pve_cluster_id=cluster.id,
        kind=body.kind,
        node=node,
        spec=spec,
        status="queued",
        steps=[],
        result={},
        uplink_address=address,
        created_by_user_id=user.user_id,
    )
    db.add(job)
    db.flush()
    action = (
        "pve_cluster_lab_template_build_started"
        if body.kind == "template_build"
        else "pve_cluster_share_helper_template_build_started"
    )
    _audit(
        db,
        actor_user_id=user.user_id,
        correlation_id=getattr(request.state, "correlation_id", None),
        action=action,
        cluster_id=cluster.id,
        detail={
            "runner": "agent",
            "job_id": str(job.id),
            "node": node,
            "vmid": vmid,
            "storage": body.storage,
            "image_source": body.image_source,
            "uplink_address": address,
        },
    )
    db.commit()
    db.refresh(job)
    return agent_job_out(job)


# --- the agent ---------------------------------------------------------------------------


def _agent_job_or_404(db: Session, cluster: PveCluster, job_id: uuid.UUID) -> LabAgentSetupJob:
    job = db.query(LabAgentSetupJob).filter_by(id=job_id).with_for_update().one_or_none()
    if job is None or job.pve_cluster_id != cluster.id:
        raise ResurgamApiError(code="not_found", message="Setup job not found.", status_code=404)
    return job


@agent_router.get("/setup-jobs", response_model=list[LabAgentSetupJobOut])
def agent_list_setup_jobs(
    authorization: str | None = Header(default=None), db: Session = Depends(get_db)
) -> list[LabAgentSetupJobOut]:
    """`resurgam-lab-agent` (cluster token): template builds queued for this
    cluster, oldest first."""
    cluster = _require_lab_agent(db, authorization)
    jobs = (
        db.query(LabAgentSetupJob)
        .filter_by(pve_cluster_id=cluster.id, status="queued")
        .order_by(LabAgentSetupJob.created_at)
        .all()
    )
    return [LabAgentSetupJobOut(id=j.id, kind=j.kind, node=j.node, spec=j.spec) for j in jobs]


@agent_router.post("/setup-jobs/{job_id}/claim", status_code=204)
def agent_claim_setup_job(
    job_id: uuid.UUID, authorization: str | None = Header(default=None), db: Session = Depends(get_db)
) -> None:
    """Marks a queued job as running on this agent. 409 if it is not queued."""
    cluster = _require_lab_agent(db, authorization)
    job = _agent_job_or_404(db, cluster, job_id)
    if job.status != "queued":
        raise ResurgamApiError(code="setup_job_not_queued", message="The job is not queued.", status_code=409)
    job.status = "running"
    job.claimed_at = job.last_progress_at = _now()
    db.commit()


@agent_router.post("/setup-jobs/{job_id}/progress", status_code=204)
def agent_setup_progress(
    job_id: uuid.UUID,
    body: AgentSetupProgress,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> None:
    """The step log so far (replaces the stored one). 409 once the job ended."""
    cluster = _require_lab_agent(db, authorization)
    job = _agent_job_or_404(db, cluster, job_id)
    if job.status != "running":
        raise ResurgamApiError(code="setup_job_not_running", message="The job is not running.", status_code=409)
    job.steps = [step.model_dump(mode="json") for step in body.steps]
    job.result = body.result
    job.last_progress_at = _now()
    db.commit()


@agent_router.post("/setup-jobs/{job_id}/status", status_code=204)
def agent_setup_status(
    job_id: uuid.UUID,
    body: AgentSetupStatus,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> None:
    """The outcome. On success the built template (`result.template_vmid`, which
    must be the job's VMID) becomes the cluster's lab template or share helper
    template. Frees the job's uplink address. Audited."""
    cluster = _require_lab_agent(db, authorization)
    job = _agent_job_or_404(db, cluster, job_id)
    if job.status not in ACTIVE_STATES:
        raise ResurgamApiError(code="setup_job_finished", message="The job already ended.", status_code=409)
    job.steps = [step.model_dump(mode="json") for step in body.steps]
    job.result = body.result
    job.finished_at = _now()
    status = body.status
    template_vmid = body.result.get("template_vmid")
    if status == "succeeded" and template_vmid != job.spec.get("vmid"):
        status = "failed"
        body.error_code = "template_vmid_mismatch"
        body.error_message = f"The agent reported template VMID {template_vmid}, expected {job.spec.get('vmid')}."
    job.status = status
    job.error_code = body.error_code if status == "failed" else None
    job.error_message = body.error_message if status == "failed" else None
    detail = {"runner": "agent", "job_id": str(job.id), "node": job.node, "vmid": job.spec.get("vmid")}
    if status == "succeeded":
        if job.kind == "template_build":
            detail.update(
                previous_vmid=cluster.lab_appliance_template_vmid, prober_version=body.result.get("prober_version")
            )
            cluster.lab_appliance_template_vmid = template_vmid
            action = "pve_cluster_lab_template_built"
        else:
            detail.update(
                previous_vmid=cluster.disk_mount_helper_template_vmid,
                template_version=body.result.get("template_version"),
            )
            cluster.disk_mount_helper_template_vmid = template_vmid
            action = "pve_cluster_share_helper_template_built"
    else:
        detail["error_code"] = job.error_code
        action = (
            "pve_cluster_lab_template_build_failed"
            if job.kind == "template_build"
            else "pve_cluster_share_helper_template_build_failed"
        )
    if job.uplink_address:
        release_uplink_ip(db, cluster_id=cluster.id, address=job.uplink_address)
    _audit(
        db,
        actor_user_id=job.created_by_user_id,
        correlation_id=None,
        action=action,
        cluster_id=cluster.id,
        detail=detail,
    )
    db.commit()


_SETUP_ARTIFACT_PATH = Path(pattern=r"^resurgam-lab-prober$", description="The package: `resurgam-lab-prober`.")


@agent_router.get("/setup-artifacts/{name}", response_model=AgentSetupArtifactOut)
def agent_setup_artifact(
    name: Annotated[str, _SETUP_ARTIFACT_PATH],
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AgentSetupArtifactOut:
    """Name, version and SHA-256 of a package a setup job installs into the
    template (the prober)."""
    _require_lab_agent(db, authorization)
    item = require_artifact(settings.lab_artifacts_dir, name)
    return AgentSetupArtifactOut(name=item.name, filename=item.filename, version=item.version, sha256=item.sha256)


@agent_router.get(
    "/setup-artifacts/{name}/file",
    response_class=FileResponse,
    responses={200: {"content": {"application/vnd.debian.binary-package": {}}}},
)
def agent_setup_artifact_file(
    name: Annotated[str, _SETUP_ARTIFACT_PATH],
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """The package file itself; the agent checks its SHA-256."""
    _require_lab_agent(db, authorization)
    item = require_artifact(settings.lab_artifacts_dir, name)
    return FileResponse(item.path, media_type="application/vnd.debian.binary-package", filename=item.filename)
