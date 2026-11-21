"""Schemas for RESURGAM-driven lab provisioning: shipped package downloads,
the one-time SSH login, guided agent install / template build jobs and
the manual template build script."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, model_validator

from resurgam_api.lab_setup.template_build import NODE_IMAGE_PATH_PATTERN, NODE_IMAGE_VOLID_PATTERN
from resurgam_api.restore_test.lab_readiness import DEFAULT_OVERLAY_DIR, DEFAULT_OVERLAY_STORAGE_ID
from resurgam_api.schemas.lab_setup import PveNodeName

_STORAGE_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9\-_.]*[A-Za-z0-9]$"
# Host name or IPv4/IPv6 literal -- no spaces, no shell metacharacters.
_HOST_PATTERN = r"^[A-Za-z0-9.:\-\[\]%]+$"
_FINGERPRINT_PATTERN = r"^SHA256:[A-Za-z0-9+/]{43}$"


class LabArtifactOut(BaseModel):
    name: str = Field(description="Package name: `resurgam-lab-agent` or `resurgam-lab-prober`.")
    version: str = Field(description="Debian package version, e.g. `0.2.1-1` (upstream part = RESURGAM VERSION).")
    architecture: str = Field(description="Debian architecture, e.g. `amd64`.")
    filename: str = Field(description="File name of the .deb.")
    sha256: str = Field(description="SHA-256 of the .deb (hex).")
    size: int = Field(description="Size in bytes.")
    download_path: str = Field(description="API path (below /api/v1) serving the .deb.")
    sha256_path: str = Field(description="API path serving a `sha256sum -c` compatible checksum line.")


class LabArtifactsOut(BaseModel):
    """The Recovery Lab packages shipped with this RESURGAM (built into the
    backend image from the same source tree, so they match this version)."""

    release_version: str = Field(description="RESURGAM release the packages were built for (VERSION file).")
    debian_release: str = Field(
        description="Debian codename the packages were built on, e.g. `trixie`; the lab node must run the same "
        "release (PVE 9 = Debian 13 trixie)."
    )
    artifacts: list[LabArtifactOut] = Field(description="The packages.")


class SshTargetOut(BaseModel):
    node: str = Field(description="The PVE node.")
    host: str = Field(description="Suggested SSH address: the node's IP as PVE reports it (`GET /cluster/status`).")
    host_source: Literal["cluster_status", "cluster_host"] = Field(
        description="`cluster_status`: the node's IP from PVE; `cluster_host`: PVE reported none, so the cluster's "
        "API host is suggested instead -- check it is really this node."
    )
    port: int = Field(description="Suggested SSH port (22).")
    username: str = Field(description="Suggested user name (`root`).")


class SshHostKeyRequest(BaseModel):
    """Step 1 of a guided SSH session: only connects and reads the host key.
    No credential is sent (they would go to a host not yet verified)."""

    node: PveNodeName = Field(description="The PVE node the session is for.")
    host: str = Field(min_length=1, max_length=255, pattern=_HOST_PATTERN, description="SSH address of the node.")
    port: int = Field(default=22, ge=1, le=65535, description="SSH port (default 22).")


class SshHostKeyOut(BaseModel):
    host: str = Field(description="The address that was contacted.")
    port: int = Field(description="The port that was contacted.")
    key_type: str = Field(description="Host key algorithm, e.g. `ssh-ed25519`.")
    fingerprint_sha256: str = Field(
        description="The host key's fingerprint as `ssh-keygen -lf` prints it (`SHA256:...`). Compare it with the "
        "node before confirming; it must be sent back unchanged as `confirmed_fingerprint`."
    )
    verify_command: str = Field(description="Command to run on the node (its console) to show its fingerprint.")


class SshLoginRequest(BaseModel):
    """One-time SSH login to a lab node. Never stored, logged, audited or
    returned; only held in memory while the job runs."""

    node: PveNodeName = Field(description="The PVE node to work on (must be the host behind `host`).")
    host: str = Field(min_length=1, max_length=255, pattern=_HOST_PATTERN, description="SSH address of the node.")
    port: int = Field(default=22, ge=1, le=65535, description="SSH port (default 22).")
    username: str = Field(
        default="root",
        min_length=1,
        max_length=32,
        pattern=r"^[a-z_][a-z0-9_.\-]*$",
        description="Login user: `root`, or a user allowed to run `sudo` without a password.",
    )
    auth_method: Literal["password", "private_key"] = Field(description="How to log in.")
    password: SecretStr | None = Field(default=None, description="Password (auth_method `password`). One-time.")
    private_key: SecretStr | None = Field(
        default=None,
        description="Private key in OpenSSH/PEM format incl. the BEGIN/END lines (auth_method `private_key`; "
        "ed25519, ECDSA or RSA). One-time.",
    )
    passphrase: SecretStr | None = Field(default=None, description="Passphrase of an encrypted private key.")
    confirmed_fingerprint: str = Field(
        pattern=_FINGERPRINT_PATTERN,
        description="The host key fingerprint from step 1 that the admin compared and confirmed. The session is "
        "refused before any credential is sent if the node presents a different key.",
    )

    @model_validator(mode="after")
    def _secret_for_method(self) -> "SshLoginRequest":
        if self.private_key is not None and len(self.private_key.get_secret_value()) > 16384:
            raise ValueError("private_key is too long (max 16 KiB).")
        if self.auth_method == "password" and not (self.password and self.password.get_secret_value()):
            raise ValueError("Enter the password (auth_method is 'password').")
        if self.auth_method == "private_key" and not (self.private_key and self.private_key.get_secret_value()):
            raise ValueError("Paste the private key (auth_method is 'private_key').")
        return self


class LabAgentInstallJobRequest(BaseModel):
    """Guided agent install: uploads the shipped resurgam-lab-agent .deb,
    installs it, writes /etc/default/resurgam-lab-agent with a freshly
    generated agent token (replaces the cluster's previous token once the
    file is written) and starts the service."""

    ssh: SshLoginRequest = Field(description="One-time SSH login to the lab node.")
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
    pbs_token: SecretStr | None = Field(
        default=None,
        description="This node's own PBS API token `<user>@<realm>!<name>:<secret>` (RESURGAM_LAB_AGENT_PBS_TOKEN, "
        "Datastore.Read on the backed-up datastores). Optional: if empty, the token already in the node's env file "
        "is kept; without any, the agent will not start. One-time, never stored by RESURGAM.",
    )

    @model_validator(mode="after")
    def _pbs_token_shape(self) -> "LabAgentInstallJobRequest":
        if self.pbs_token is not None:
            value = self.pbs_token.get_secret_value()
            if value and (any(c.isspace() for c in value) or "!" not in value or ":" not in value or len(value) > 512):
                raise ValueError("pbs_token must look like '<user>@<realm>!<token-name>:<secret>' without spaces.")
        return self


class LabTemplateBuildJobRequest(BaseModel):
    """Guided probe appliance template build on one lab node."""

    ssh: SshLoginRequest = Field(description="One-time SSH login to the node the template is built on.")
    storage: str = Field(
        pattern=_STORAGE_ID_PATTERN,
        max_length=64,
        description="Target storage for the template's disk and cloud-init drive (content `images`, on that node).",
    )
    vmid: int | None = Field(
        default=None,
        ge=100,
        le=999999999,
        description="VMID for the template; omitted = PVE's next free VMID (`GET /cluster/nextid`).",
    )
    image_source: Literal["download", "node_image"] = Field(
        default="download",
        description="`download`: the node downloads the Debian 13 genericcloud image from cloud.debian.org (needs "
        "internet on the node). `node_image`: use an image already on the node (`node_image`).",
    )
    node_image: str | None = Field(
        default=None,
        max_length=255,
        description="With image_source `node_image`: absolute path of a Debian 13 cloud image (qcow2/raw) on the "
        "node, or a PVE volume with content `import` (e.g. `local:import/debian-13-genericcloud-amd64.qcow2`).",
    )

    @model_validator(mode="after")
    def _image(self) -> "LabTemplateBuildJobRequest":
        self.node_image = validate_node_image(self.image_source, self.node_image)
        return self


def validate_node_image(image_source: str, node_image: str | None) -> str | None:
    """The cleaned `node_image` for image_source `node_image` (None otherwise);
    ValueError with a fix hint if it is missing or not a path/volume id."""
    import re

    if image_source != "node_image":
        return None
    image = (node_image or "").strip()
    if not image:
        raise ValueError("node_image is required when image_source is 'node_image'.")
    if ".." in image or not (re.match(NODE_IMAGE_PATH_PATTERN, image) or re.match(NODE_IMAGE_VOLID_PATTERN, image)):
        raise ValueError(
            "node_image must be an absolute path (e.g. /root/debian-13-genericcloud-amd64.qcow2) or a volume id "
            "(e.g. local:import/debian-13-genericcloud-amd64.qcow2)."
        )
    return image


class LabSetupJobStepOut(BaseModel):
    key: str = Field(
        description="Step id: agent install `connect`, `privileges`, `preflight`, `upload`, `install`, "
        "`configure`, `service`; template build `connect`, `privileges`, `preflight`, `image`, `seed`, `snippet`, "
        "`create`, `first_boot`, `finalize`, `cleanup`."
    )
    status: Literal["running", "ok", "warning", "failed", "skipped"] = Field(description="Step state.")
    message: str = Field(description="What happened, in English (secrets are redacted).")
    output: str | None = Field(
        default=None, description="Tail of the command output, if useful (secrets are redacted)."
    )
    started_at: datetime = Field(description="When the step started (UTC).")
    finished_at: datetime | None = Field(default=None, description="When it ended; `null` while running.")


class LabSetupJobOut(BaseModel):
    """A guided SSH job and its live step log. Kept in the API process's
    memory only (no credential in it); lost on an API restart."""

    id: UUID = Field(description="Job id; poll `GET /pve-clusters/{id}/lab-setup/jobs/{job_id}`.")
    cluster_id: UUID = Field(description="The PVE cluster.")
    kind: Literal["agent_install", "template_build", "share_helper_template_build"] = Field(
        description="What the job does (`share_helper_template_build`: the Disk Mounts helper template, ADR 0025)."
    )
    node: str = Field(description="The node it works on.")
    status: Literal["running", "succeeded", "failed"] = Field(description="Overall state.")
    started_at: datetime = Field(description="Start (UTC).")
    finished_at: datetime | None = Field(default=None, description="End (UTC); `null` while running.")
    steps: list[LabSetupJobStepOut] = Field(description="The step log, in order.")
    result: dict = Field(
        description="Facts once known: `privilege_mode` (root/sudo); agent install `installed_version`, "
        "`previous_version`, `pbs_token_status` (provided/kept/missing), `service_active`; template build `vmid`, "
        "`storage`, `template_vmid`, `prober_version`, `uplink_address`."
    )
    error_code: str | None = Field(
        default=None,
        description="Why it failed, e.g. `host_key_mismatch`, `auth_failed`, `privilege_missing`, "
        "`debian_release_mismatch`, `install_failed`, `image_download_failed`, `build_timeout`; `null` otherwise.",
    )
    error_message: str | None = Field(default=None, description="The failure in English; `null` otherwise.")
    runner: Literal["ssh", "agent"] = Field(
        default="ssh",
        description="`ssh`: the API drives it over a one-time SSH login. `agent`: the paired lab agent runs it on its "
        "node (ADR 0027); it survives an API restart.",
    )


class NextVmidOut(BaseModel):
    vmid: int = Field(description="PVE's next free VMID (`GET /cluster/nextid`).")


class LabTemplateBuildScriptOut(BaseModel):
    """The manual equivalent of the guided template build."""

    node: str = Field(description="Node the script is for.")
    vmid: int = Field(description="VMID the script creates.")
    storage: str = Field(description="Target storage.")
    prober_filename: str = Field(description="The resurgam-lab-prober .deb the script expects in /root/.")
    prober_download_path: str = Field(description="API path serving that .deb.")
    script: str = Field(description="Shell script to run as root on the node.")
