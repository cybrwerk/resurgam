"""Guided build of the Disk Mounts share helper template (ADR 0025 §2).

Same one-time SSH flow as the probe appliance template
(`api/v1/pve_lab_provisioning.py`, whose helpers are reused): the host key is
fetched and confirmed first (`POST /pve-clusters/{id}/lab-setup/ssh/host-key`),
the credentials live only in the job thread's memory, and the job is polled at
`GET /pve-clusters/{id}/lab-setup/jobs/{job_id}`.
"""

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from resurgam_api.api.v1.pve_clusters import _cluster_trust_and_token, _get_cluster_or_404
from resurgam_api.api.v1.pve_lab_provisioning import (
    SessionRunner,
    _audit,
    _check_target,
    _connect,
    _credentials,
    _ensure_no_running_job,
    _job_out,
    _template_prerequisites,
    get_job_session_runner,
)
from resurgam_api.auth.dependency import require_role
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_db
from resurgam_api.disk_mounts.share_helper_template import (
    TEMPLATE_SCHEMA_VERSION,
    ShareHelperTemplateSpec,
    run_share_helper_template_build,
)
from resurgam_api.errors import ResurgamApiError
from resurgam_api.lab_setup.jobs import JobLog, LabSetupJob, StepFailed, registry
from resurgam_api.lab_setup.ssh import SshError
from resurgam_api.lab_setup.template_build import IMAGE_SOURCE_NODE
from resurgam_api.lab_setup.uplink import (
    PURPOSE_TEMPLATE_BUILD,
    allocate_uplink_ip,
    release_uplink_ip,
    uplink_config_from_cluster,
)
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.pve import PveCluster
from resurgam_api.schemas.lab_provisioning import LabSetupJobOut, LabTemplateBuildJobRequest
from resurgam_api.version import running_version

router = APIRouter(prefix="/pve-clusters", tags=["disk-mounts"])

_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)


@router.post("/{cluster_id}/lab-setup/share-helper-template-build-jobs", response_model=LabSetupJobOut, status_code=202)
def start_share_helper_template_build_job(
    cluster_id: uuid.UUID,
    body: LabTemplateBuildJobRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    run_in_session: SessionRunner = Depends(get_job_session_runner),
    user=Depends(_require_platform_admin),
) -> LabSetupJobOut:
    """Builds the Disk Mounts share helper template on a lab node over a one-time
    SSH session (host key must match): Debian 13 genericcloud (downloaded on the
    node -- needs HTTPS to cloud.debian.org -- or an image already there), first
    boot on the lab uplink installs Samba, the NFS server and the file system
    tools from the Debian mirror, then it is cleaned and converted to a template;
    on success it becomes this cluster's `disk_mount_helper_template_vmid`. A
    failed build removes the VM it created. Needs the lab uplink bridge and
    snippet storage saved. 202 + a job to poll. Audited: start and outcome;
    credentials never."""
    cluster = _get_cluster_or_404(db, cluster_id)
    if not cluster.is_lab_compute:
        raise ResurgamApiError(
            code="not_lab_compute",
            message="Disk mounts run on a lab cluster: enable 'Lab compute' for this cluster first.",
            status_code=400,
        )
    _template_prerequisites(cluster)
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
        action="pve_cluster_share_helper_template_build_started",
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

    spec = ShareHelperTemplateSpec(
        node=ssh.node,
        vmid=vmid,
        storage=body.storage,
        snippet_storage=cluster.lab_snippet_storage_id,
        uplink=uplink,
        uplink_address=address,
        image_source=body.image_source,
        node_image=body.node_image if body.image_source == IMAGE_SOURCE_NODE else None,
        release_version=running_version(),
    )
    credentials = _credentials(ssh)

    def commit_template(template_vmid: int) -> None:
        def write(session: Session) -> None:
            target = session.get(PveCluster, cluster_pk)
            previous = target.disk_mount_helper_template_vmid
            target.disk_mount_helper_template_vmid = template_vmid
            _audit(
                session,
                actor_user_id=actor,
                correlation_id=correlation_id,
                action="pve_cluster_share_helper_template_built",
                cluster_id=cluster_pk,
                detail={
                    "node": ssh.node,
                    "vmid": template_vmid,
                    "previous_vmid": previous,
                    "storage": body.storage,
                    "template_version": TEMPLATE_SCHEMA_VERSION,
                },
            )

        run_in_session(write)

    def work(log: JobLog) -> None:
        log.set_result(uplink_address=address)
        session, shell = _connect(log, ssh, credentials)
        try:
            run_share_helper_template_build(shell, log, spec, commit_template=commit_template)
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
                    action="pve_cluster_share_helper_template_build_failed",
                    cluster_id=cluster_pk,
                    detail={"job_id": str(job.id), "node": job.node, "vmid": vmid, "error_code": job.error_code},
                )

        run_in_session(write)

    job = registry.start(
        cluster_id=cluster_pk,
        kind="share_helper_template_build",
        node=ssh.node,
        secrets=credentials.secrets(),
        work=work,
        on_finish=on_finish,
    )
    return _job_out(job)
