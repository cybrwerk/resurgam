"""RESURGAM-driven Recovery Lab provisioning (Lab Compute setup wizard):

- `GET /pve-clusters/lab-setup/downloads[/{artifact}]` -- the lab packages
  shipped with this backend (manual install).
- A guided, one-time SSH session to a lab node: `.../ssh-target` (default
  address), `.../ssh/host-key` (step 1: fingerprint, no credentials sent),
  then a job that installs `resurgam-lab-agent` or builds the probe
  appliance template (step 2, credentials re-sent with the confirmed
  fingerprint), polled via `.../lab-setup/jobs/{job_id}`.
- The manual template build script and PVE's next free VMID.

Credentials: request bodies only; held in memory by the job's thread and
dropped when it ends (`lab_setup/jobs.py` explains the job design). They
never reach the database, the audit trail, a log line or a response.
"""

import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy.orm import Session

from resurgam_api.api.v1._connection_helpers import upstream_error
from resurgam_api.api.v1.pve_clusters import _cluster_trust_and_token, _get_cluster_or_404
from resurgam_api.api.v1.pve_lab_setup import DOWNLOADS_PATH
from resurgam_api.auth.dependency import require_role
from resurgam_api.config import Settings, get_settings
from resurgam_api.connectivity.pve_api import check_vmid_free, list_nodes, next_free_vmid
from resurgam_api.connectivity.pve_lab_api import PveApiError, get_node_addresses, list_storage_config
from resurgam_api.db import get_db, get_session_factory
from resurgam_api.errors import ResurgamApiError
from resurgam_api.lab_setup import ssh as ssh_module
from resurgam_api.lab_setup.agent_install import AgentInstallSpec, run_agent_install
from resurgam_api.lab_setup.artifacts import (
    ARTIFACT_NAMES,
    LAB_AGENT_ARTIFACT,
    LAB_PROBER_ARTIFACT,
    require_artifact,
    require_manifest,
)
from resurgam_api.lab_setup.jobs import JobLog, LabSetupJob, StepFailed, registry
from resurgam_api.lab_setup.ssh import SshCredentials, SshError, SshTarget, open_remote_shell
from resurgam_api.lab_setup.template_build import (
    IMAGE_SOURCE_NODE,
    TemplateBuildSpec,
    render_manual_script,
    run_template_build,
)
from resurgam_api.lab_setup.uplink import (
    PURPOSE_TEMPLATE_BUILD,
    allocate_uplink_ip,
    raise_if_invalid,
    release_uplink_ip,
    uplink_config_from_cluster,
)
from resurgam_api.models.audit import AuditEvent
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.pve import LabUplinkIpAllocation, PveCluster
from resurgam_api.restore_test.execution import generate_appliance_run_token, hash_appliance_run_token
from resurgam_api.schemas.lab_provisioning import (
    LabAgentInstallJobRequest,
    LabArtifactOut,
    LabArtifactsOut,
    LabSetupJobOut,
    LabSetupJobStepOut,
    LabTemplateBuildJobRequest,
    LabTemplateBuildScriptOut,
    NextVmidOut,
    SshHostKeyOut,
    SshHostKeyRequest,
    SshLoginRequest,
    SshTargetOut,
    validate_node_image,
)
from resurgam_api.schemas.lab_setup import PveNodeName

router = APIRouter(prefix="/pve-clusters", tags=["pve-clusters"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)

SSH_CONNECT_TIMEOUT_SECONDS = 15.0

SessionRunner = Callable[[Callable[[Session], None]], None]


def get_job_session_runner() -> SessionRunner:
    """How a job's background thread writes to the database: a fresh,
    short-lived session per write (the request's session is gone by then).
    Overridden in tests."""
    factory = get_session_factory()

    def run(fn: Callable[[Session], None]) -> None:
        db = factory()
        try:
            fn(db)
            db.commit()
        finally:
            db.close()

    return run


def _audit(db: Session, *, actor_user_id, correlation_id, action: str, cluster_id, detail: dict | None) -> None:
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            target_type="pve_cluster",
            target_id=str(cluster_id),
            correlation_id=correlation_id,
            detail=detail,
        )
    )


def _job_out(job: LabSetupJob) -> LabSetupJobOut:
    snap = registry.snapshot(job)
    return LabSetupJobOut(
        id=snap.id,
        cluster_id=snap.cluster_id,
        kind=snap.kind,
        node=snap.node,
        status=snap.status,
        started_at=snap.started_at,
        finished_at=snap.finished_at,
        steps=[LabSetupJobStepOut(**vars(step)) for step in snap.steps],
        result=snap.result,
        error_code=snap.error_code,
        error_message=snap.error_message,
    )


# --- downloads ---------------------------------------------------------------


@router.get("/lab-setup/downloads", response_model=LabArtifactsOut)
def list_lab_downloads(
    settings: Settings = Depends(get_settings), _user=Depends(_require_platform_admin)
) -> LabArtifactsOut:
    """The Recovery Lab packages shipped with this RESURGAM (built into the
    backend image from the same source tree, `Settings.lab_artifacts_dir`):
    `resurgam-lab-agent` for the lab node, `resurgam-lab-prober` for the
    probe appliance template. 404 `lab_artifacts_missing` outside the image."""
    manifest = require_manifest(settings.lab_artifacts_dir)
    return LabArtifactsOut(
        release_version=manifest.release_version,
        debian_release=manifest.debian_release,
        artifacts=[
            LabArtifactOut(
                name=a.name,
                version=a.version,
                architecture=a.architecture,
                filename=a.filename,
                sha256=a.sha256,
                size=a.size,
                download_path=f"{DOWNLOADS_PATH}/{a.name}",
                sha256_path=f"{DOWNLOADS_PATH}/{a.name}.sha256",
            )
            for a in sorted(manifest.artifacts.values(), key=lambda a: a.name)
        ],
    )


_ARTIFACT_PATH = Path(
    pattern=r"^resurgam-lab-(agent|prober)(\.sha256)?$",
    description="`resurgam-lab-agent` or `resurgam-lab-prober` for the .deb; append `.sha256` for its checksum "
    "line (`sha256sum -c` format).",
)


@router.get(
    "/lab-setup/downloads/{artifact}",
    response_class=FileResponse,
    responses={200: {"content": {"application/vnd.debian.binary-package": {}, "text/plain": {}}}},
)
def download_lab_artifact(
    artifact: Annotated[str, _ARTIFACT_PATH],
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
):
    """Serves a shipped lab package (.deb), or with `.sha256` appended its
    checksum line. Platform admin only. Copy it to the node and install it
    with `apt install ./<file>` (manual mode)."""
    name = artifact.removesuffix(".sha256")
    if name not in ARTIFACT_NAMES:
        raise ResurgamApiError(code="not_found", message=f"Unknown package '{name}'.", status_code=404)
    item = require_artifact(settings.lab_artifacts_dir, name)
    if artifact.endswith(".sha256"):
        return PlainTextResponse(f"{item.sha256}  {item.filename}\n")
    return FileResponse(item.path, media_type="application/vnd.debian.binary-package", filename=item.filename)


# --- SSH step 1 ----------------------------------------------------------------


@router.get("/{cluster_id}/lab-setup/nodes/{node}/ssh-target", response_model=SshTargetOut)
def get_lab_ssh_target(
    cluster_id: uuid.UUID,
    node: Annotated[str, Path(pattern=r"^[A-Za-z0-9]([A-Za-z0-9\-]*[A-Za-z0-9])?$", description="PVE node name.")],
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> SshTargetOut:
    """Suggested SSH address for a node: its IP as PVE resolves it
    (`GET /cluster/status`), else the cluster's API host. Nothing is
    contacted over SSH here."""
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    try:
        addresses = get_node_addresses(cluster.host, cluster.port, trust, token, settings.connectivity_timeout_seconds)
    except PveApiError:
        addresses = {}
    if node in addresses:
        return SshTargetOut(node=node, host=addresses[node], host_source="cluster_status", port=22, username="root")
    return SshTargetOut(node=node, host=cluster.host, host_source="cluster_host", port=22, username="root")


@router.post("/{cluster_id}/lab-setup/ssh/host-key", response_model=SshHostKeyOut)
def get_lab_ssh_host_key(
    cluster_id: uuid.UUID,
    body: SshHostKeyRequest,
    db: Session = Depends(get_db),
    _user=Depends(_require_platform_admin),
) -> SshHostKeyOut:
    """Step 1 of a guided SSH session: connects to the node's SSH server
    and returns its host key fingerprint (`SHA256:...`) for the admin to
    compare and confirm. Does not log in -- no credential is ever sent to
    a host whose key has not been confirmed. Nothing is stored."""
    _get_cluster_or_404(db, cluster_id)
    try:
        info = ssh_module.ssh_backend.fetch_host_key(
            SshTarget(body.host, body.port), timeout=SSH_CONNECT_TIMEOUT_SECONDS
        )
    except SshError as exc:
        raise ResurgamApiError(code=exc.code, message=exc.message, status_code=502) from exc
    return SshHostKeyOut(
        host=body.host,
        port=body.port,
        key_type=info.key_type,
        fingerprint_sha256=info.fingerprint_sha256,
        verify_command='for f in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf "$f"; done',
    )


# --- jobs ------------------------------------------------------------------------


def _credentials(ssh: SshLoginRequest) -> SshCredentials:
    return SshCredentials(
        username=ssh.username,
        password=ssh.password.get_secret_value() if ssh.auth_method == "password" and ssh.password else None,
        private_key=ssh.private_key.get_secret_value()
        if ssh.auth_method == "private_key" and ssh.private_key
        else None,
        passphrase=ssh.passphrase.get_secret_value() if ssh.passphrase else None,
    )


def _connect(log: JobLog, ssh: SshLoginRequest, credentials: SshCredentials):
    step = log.start("connect", f"Connecting to {ssh.host}:{ssh.port} and checking the confirmed host key.")
    try:
        session = ssh_module.ssh_backend.connect(
            SshTarget(ssh.host, ssh.port),
            credentials,
            expected_fingerprint=ssh.confirmed_fingerprint,
            timeout=SSH_CONNECT_TIMEOUT_SECONDS,
        )
    except SshError as exc:
        raise StepFailed(exc.code, exc.message) from exc
    log.finish(step, "ok", f"Connected; host key {ssh.confirmed_fingerprint} matches.")
    try:
        return session, open_remote_shell(session, credentials.username)
    except SshError as exc:
        session.close()
        raise StepFailed(exc.code, exc.message) from exc


def _ensure_no_running_job(db: Session, cluster_id: uuid.UUID) -> None:
    """409 while a setup job runs for the cluster: a guided SSH job (in this
    process's memory) or an agent-run build (ADR 0027, in the database; stale
    ones are failed first). They share the uplink, VMIDs and the agent."""
    running = registry.running_for_cluster(cluster_id)
    if running is not None:
        raise ResurgamApiError(
            code="lab_setup_job_running",
            message="Another guided setup job is still running for this cluster; wait for it to finish.",
            status_code=409,
            details={"job_id": str(running.id), "kind": running.kind},
        )
    from resurgam_api.api.v1.lab_pairing import active_agent_job  # lab_pairing imports this module

    active = active_agent_job(db, cluster_id)
    if active is not None:
        raise ResurgamApiError(
            code="lab_setup_job_running",
            message=f"The lab agent on {active.node} is still running a setup job for this cluster; wait for it to "
            "finish.",
            status_code=409,
            details={"job_id": str(active.id), "kind": active.kind},
        )


@router.post("/{cluster_id}/lab-setup/agent-install-jobs", response_model=LabSetupJobOut, status_code=202)
def start_lab_agent_install_job(
    cluster_id: uuid.UUID,
    body: LabAgentInstallJobRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    run_in_session: SessionRunner = Depends(get_job_session_runner),
    user=Depends(_require_platform_admin),
) -> LabSetupJobOut:
    """Step 2, agent: logs in once (host key must match
    `confirmed_fingerprint`, else nothing is sent), uploads the shipped
    resurgam-lab-agent .deb, installs it (root or `sudo -n`), writes
    /etc/default/resurgam-lab-agent (backend URL, a freshly generated agent
    token, overlay storage id/dir, the node's PBS token) and restarts the
    service. Returns at once (202) with a job to poll; the wizard then waits
    for the agent's check-in. The new token replaces the cluster's previous
    one only after the env file is written. Credentials and token are never
    stored, logged, audited or returned. Audited: start and outcome."""
    cluster = _get_cluster_or_404(db, cluster_id)
    if not settings.recovery_lab_public_backend_url:
        raise ResurgamApiError(
            code="recovery_lab_backend_url_not_configured",
            message="RECOVERY_LAB_PUBLIC_BACKEND_URL is not set, so the agent would not know where to call home. "
            "Set it for the API, restart it, then retry.",
            status_code=400,
        )
    manifest = require_manifest(settings.lab_artifacts_dir)
    artifact = require_artifact(settings.lab_artifacts_dir, LAB_AGENT_ARTIFACT)
    _ensure_no_running_job(db, cluster.id)

    ssh = body.ssh
    credentials = _credentials(ssh)
    pbs_token = body.pbs_token.get_secret_value() if body.pbs_token else None
    spec = AgentInstallSpec(
        cluster_name=cluster.display_name,
        backend_url=settings.recovery_lab_public_backend_url,
        overlay_storage_id=body.overlay_storage_id,
        overlay_dir=body.overlay_dir,
        artifact=artifact,
        debian_release=manifest.debian_release,
        pbs_token=pbs_token or None,
    )
    actor = user.user_id
    correlation_id = getattr(request.state, "correlation_id", None)
    cluster_pk = cluster.id
    _audit(
        db,
        actor_user_id=actor,
        correlation_id=correlation_id,
        action="pve_cluster_lab_agent_install_started",
        cluster_id=cluster_pk,
        detail={"node": ssh.node, "host": ssh.host, "port": ssh.port, "auth_method": ssh.auth_method},
    )
    db.commit()

    def commit_token(token: str) -> None:
        def write(session: Session) -> None:
            target = session.get(PveCluster, cluster_pk)
            target.lab_agent_token_hash = hash_appliance_run_token(token)
            _audit(
                session,
                actor_user_id=actor,
                correlation_id=correlation_id,
                action="pve_cluster_lab_agent_token_rotated",
                cluster_id=cluster_pk,
                detail={"via": "guided_agent_install", "node": ssh.node},
            )

        run_in_session(write)

    def work(log: JobLog) -> None:
        session, shell = _connect(log, ssh, credentials)
        try:
            run_agent_install(shell, log, spec, issue_token=generate_appliance_run_token, commit_token=commit_token)
        except SshError as exc:
            raise StepFailed(exc.code, exc.message) from exc
        finally:
            session.close()

    def on_finish(job: LabSetupJob) -> None:
        def write(session: Session) -> None:
            _audit(
                session,
                actor_user_id=actor,
                correlation_id=correlation_id,
                action="pve_cluster_lab_agent_installed"
                if job.status == "succeeded"
                else "pve_cluster_lab_agent_install_failed",
                cluster_id=cluster_pk,
                detail={
                    "job_id": str(job.id),
                    "node": job.node,
                    "package_version": job.result.get("installed_version"),
                    "privilege_mode": job.result.get("privilege_mode"),
                    "pbs_token_status": job.result.get("pbs_token_status"),
                    "error_code": job.error_code,
                },
            )

        run_in_session(write)

    job = registry.start(
        cluster_id=cluster_pk,
        kind="agent_install",
        node=ssh.node,
        secrets=[*credentials.secrets(), *([pbs_token] if pbs_token else [])],
        work=work,
        on_finish=on_finish,
    )
    return _job_out(job)


def _template_prerequisites(cluster: PveCluster) -> None:
    missing = []
    if not cluster.lab_appliance_uplink_bridge:
        missing.append("the uplink bridge (network step)")
    if not cluster.lab_snippet_storage_id:
        missing.append("the snippet storage (storage step)")
    if missing:
        raise ResurgamApiError(
            code="lab_template_prerequisites_missing",
            message=f"Building the template needs {' and '.join(missing)} saved first: the build VM boots on the "
            "uplink and gets its first-boot config from the snippet storage.",
            status_code=400,
        )
    raise_if_invalid(uplink_config_from_cluster(cluster))


def _check_target(cluster, settings: Settings, trust, token, *, node: str, storage: str, vmid: int | None) -> int:
    timeout = settings.connectivity_timeout_seconds
    nodes = list_nodes(cluster.host, cluster.port, trust, token, timeout=timeout)
    if not nodes.reachable:
        raise upstream_error("pve", nodes.error)
    known = {n.node for n in nodes.nodes}
    if node not in known:
        raise ResurgamApiError(
            code="unknown_nodes",
            message=f"Node '{node}' is not part of this cluster. Pick from: {sorted(known)}.",
            status_code=400,
        )
    try:
        storages = list_storage_config(cluster.host, cluster.port, trust, token, timeout)
    except PveApiError as exc:
        raise upstream_error("pve", str(exc)) from exc
    target = next((s for s in storages if s.storage == storage), None)
    if target is None or "images" not in target.content or (target.nodes is not None and node not in target.nodes):
        raise ResurgamApiError(
            code="storage_unsuitable",
            message=f"Storage '{storage}' must exist, allow the 'images' content type and be available on node "
            f"'{node}'. Pick another storage.",
            status_code=400,
        )
    if vmid is None:
        result = next_free_vmid(cluster.host, cluster.port, trust, token, timeout=timeout)
        if not result.reachable or result.vmid is None:
            raise upstream_error("pve", result.error)
        return result.vmid
    result = check_vmid_free(cluster.host, cluster.port, trust, token, vmid, timeout=timeout)
    if not result.reachable:
        raise upstream_error("pve", result.error)
    if not result.free:
        raise ResurgamApiError(
            code="vmid_taken",
            message=f"VMID {vmid} is already in use on this cluster. Pick another one or leave it empty.",
            status_code=409,
        )
    return vmid


@router.post("/{cluster_id}/lab-setup/template-build-jobs", response_model=LabSetupJobOut, status_code=202)
def start_lab_template_build_job(
    cluster_id: uuid.UUID,
    body: LabTemplateBuildJobRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    run_in_session: SessionRunner = Depends(get_job_session_runner),
    user=Depends(_require_platform_admin),
) -> LabSetupJobOut:
    """Step 2, template: builds the probe appliance template on the node
    over a one-time SSH session (host key must match): base image (Debian 13
    genericcloud downloaded on the node -- needs HTTPS to cloud.debian.org --
    or an image already there), the shipped resurgam-lab-prober installed
    on first boot via cloud-init on the lab uplink (VLAN/IP as configured),
    cleaned and converted to a template with its prober version in the
    description; on success it becomes this cluster's
    `lab_appliance_template_vmid`. A failed build removes the VM it created.
    Needs the uplink bridge and snippet storage saved. 202 + a job to poll
    (several minutes). Audited: start and outcome; credentials never."""
    cluster = _get_cluster_or_404(db, cluster_id)
    _template_prerequisites(cluster)
    manifest = require_manifest(settings.lab_artifacts_dir)
    prober = require_artifact(settings.lab_artifacts_dir, LAB_PROBER_ARTIFACT)
    _ensure_no_running_job(db, cluster.id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    ssh = body.ssh
    vmid = _check_target(cluster, settings, trust, token, node=ssh.node, storage=body.storage, vmid=body.vmid)

    uplink = uplink_config_from_cluster(cluster)
    address = allocate_uplink_ip(db, cluster, purpose=PURPOSE_TEMPLATE_BUILD)
    actor = user.user_id
    correlation_id = getattr(request.state, "correlation_id", None)
    cluster_pk = cluster.id
    _audit(
        db,
        actor_user_id=actor,
        correlation_id=correlation_id,
        action="pve_cluster_lab_template_build_started",
        cluster_id=cluster_pk,
        detail={
            "node": ssh.node,
            "host": ssh.host,
            "port": ssh.port,
            "auth_method": ssh.auth_method,
            "vmid": vmid,
            "storage": body.storage,
            "image_source": body.image_source,
            "uplink_address": address,
        },
    )
    db.commit()

    spec = TemplateBuildSpec(
        node=ssh.node,
        vmid=vmid,
        storage=body.storage,
        snippet_storage=cluster.lab_snippet_storage_id,
        uplink=uplink,
        uplink_address=address,
        image_source=body.image_source,
        node_image=body.node_image if body.image_source == IMAGE_SOURCE_NODE else None,
        prober=prober,
        release_version=manifest.release_version,
    )
    credentials = _credentials(ssh)

    def commit_template(template_vmid: int, prober_version: str) -> None:
        def write(session: Session) -> None:
            target = session.get(PveCluster, cluster_pk)
            previous = target.lab_appliance_template_vmid
            target.lab_appliance_template_vmid = template_vmid
            _audit(
                session,
                actor_user_id=actor,
                correlation_id=correlation_id,
                action="pve_cluster_lab_template_built",
                cluster_id=cluster_pk,
                detail={
                    "node": ssh.node,
                    "vmid": template_vmid,
                    "previous_vmid": previous,
                    "storage": body.storage,
                    "prober_version": prober_version,
                },
            )

        run_in_session(write)

    def work(log: JobLog) -> None:
        log.set_result(uplink_address=address)
        session, shell = _connect(log, ssh, credentials)
        try:
            run_template_build(shell, log, spec, commit_template=commit_template)
        except SshError as exc:
            raise StepFailed(exc.code, exc.message) from exc
        finally:
            session.close()

    def on_finish(job: LabSetupJob) -> None:
        def write(session: Session) -> None:
            if address:
                release_uplink_ip(session, cluster_id=cluster_pk, address=address)
            if job.status != "succeeded":
                _audit(
                    session,
                    actor_user_id=actor,
                    correlation_id=correlation_id,
                    action="pve_cluster_lab_template_build_failed",
                    cluster_id=cluster_pk,
                    detail={"job_id": str(job.id), "node": job.node, "vmid": vmid, "error_code": job.error_code},
                )

        run_in_session(write)

    job = registry.start(
        cluster_id=cluster_pk,
        kind="template_build",
        node=ssh.node,
        secrets=credentials.secrets(),
        work=work,
        on_finish=on_finish,
    )
    return _job_out(job)


@router.get("/{cluster_id}/lab-setup/jobs/{job_id}", response_model=LabSetupJobOut)
def get_lab_setup_job(
    cluster_id: uuid.UUID,
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    _user=Depends(_require_platform_admin),
) -> LabSetupJobOut:
    """A setup job's state and live step log (poll every few seconds while
    `status` is `running`). Guided SSH jobs live in the API process's memory:
    404 once they are older than an hour after finishing, or after an API
    restart. Agent-run builds (`runner: agent`, ADR 0027) are kept in the
    database."""
    job = registry.get(job_id)
    if job is None or job.cluster_id != cluster_id:
        # Agent-run builds (ADR 0027) are stored in the database instead.
        from resurgam_api.api.v1.lab_pairing import find_agent_job

        agent_job = find_agent_job(db, cluster_id, job_id)
        if agent_job is not None:
            return agent_job
        raise ResurgamApiError(
            code="lab_setup_job_not_found",
            message="This setup job is unknown: it finished more than an hour ago, or the RESURGAM API was "
            "restarted meanwhile (a running job is then interrupted -- check the node and start it again).",
            status_code=404,
        )
    return _job_out(job)


# --- manual template build / helpers -------------------------------------------


def _peek_uplink_address(db: Session, cluster: PveCluster) -> str | None:
    import ipaddress

    cfg = uplink_config_from_cluster(cluster)
    if not cfg.is_static:
        return None
    taken = {
        row.ip_address for row in db.query(LabUplinkIpAllocation.ip_address).filter_by(pve_cluster_id=cluster.id).all()
    }
    start, end = int(ipaddress.IPv4Address(cfg.range_start)), int(ipaddress.IPv4Address(cfg.range_end))
    return next(
        (str(ipaddress.IPv4Address(v)) for v in range(start, end + 1) if str(ipaddress.IPv4Address(v)) not in taken),
        None,
    )


@router.get("/{cluster_id}/lab-setup/template-build-script", response_model=LabTemplateBuildScriptOut)
def get_lab_template_build_script(
    cluster_id: uuid.UUID,
    node: Annotated[PveNodeName, Query(description="Node to build the template on.")],
    storage: Annotated[
        str,
        Query(pattern=r"^[A-Za-z][A-Za-z0-9\-_.]*[A-Za-z0-9]$", description="Target storage (content `images`)."),
    ],
    vmid: Annotated[
        int | None, Query(ge=100, le=999999999, description="Template VMID; omitted = PVE's next free VMID.")
    ] = None,
    image_source: Annotated[
        str, Query(pattern=r"^(download|node_image)$", description="`download` or `node_image`.")
    ] = "download",
    node_image: Annotated[
        str | None, Query(max_length=255, description="Image on the node (with image_source `node_image`).")
    ] = None,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> LabTemplateBuildScriptOut:
    """Manual mode of "Build template": the same build as a shell script with
    this cluster's values filled in (storage, VMID, uplink bridge/VLAN/IP,
    snippet storage, shipped prober package and its SHA-256), to run as
    root on the node. Read-only; asks PVE only for the node/storage check
    and the next free VMID."""
    cluster = _get_cluster_or_404(db, cluster_id)
    _template_prerequisites(cluster)
    manifest = require_manifest(settings.lab_artifacts_dir)
    prober = require_artifact(settings.lab_artifacts_dir, LAB_PROBER_ARTIFACT)
    try:
        cleaned_image = validate_node_image(image_source, node_image)
    except ValueError as exc:
        raise ResurgamApiError(code="invalid_node_image", message=str(exc), status_code=422) from exc
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    resolved_vmid = _check_target(cluster, settings, trust, token, node=node, storage=storage, vmid=vmid)
    spec = TemplateBuildSpec(
        node=node,
        vmid=resolved_vmid,
        storage=storage,
        snippet_storage=cluster.lab_snippet_storage_id,
        uplink=uplink_config_from_cluster(cluster),
        uplink_address=_peek_uplink_address(db, cluster),
        image_source=image_source,
        node_image=cleaned_image,
        prober=prober,
        release_version=manifest.release_version,
    )
    download_path = f"{DOWNLOADS_PATH}/{LAB_PROBER_ARTIFACT}"
    return LabTemplateBuildScriptOut(
        node=node,
        vmid=resolved_vmid,
        storage=storage,
        prober_filename=prober.filename,
        prober_download_path=download_path,
        script=render_manual_script(spec, deb_download_url=f"/api/v1{download_path}"),
    )


@router.get("/{cluster_id}/lab-setup/next-vmid", response_model=NextVmidOut)
def get_lab_next_vmid(
    cluster_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> NextVmidOut:
    """PVE's next free VMID (`GET /cluster/nextid`) -- the default VMID for
    a new probe appliance template."""
    cluster = _get_cluster_or_404(db, cluster_id)
    trust, token = _cluster_trust_and_token(db, settings, cluster)
    result = next_free_vmid(cluster.host, cluster.port, trust, token, timeout=settings.connectivity_timeout_seconds)
    if not result.reachable or result.vmid is None:
        raise upstream_error("pve", result.error)
    return NextVmidOut(vmid=result.vmid)
