"""Builds the Disk Mounts "share helper" VM template on a lab node (ADR 0025 §2).

The same pipeline as the probe appliance template
(`resurgam_worker.lab_build.probe_template`, whose command builders are reused;
run over SSH or by the paired lab agent alike): Debian 13 genericcloud image downloaded on
the node and SHA-512-checked (or an image already there), `qm create` with
`import-from`, a cloud-init drive and the lab uplink NIC, a first boot that
installs packages from the Debian mirror and leaves a result file read back
through `qm guest exec`, then `cloud-init clean`, machine-id/host keys reset,
build-only options removed, a version marker in the description and
`qm template`. A failed build destroys only the VM it created.

Differences to the probe template: no seed ISO (no RESURGAM package goes into
this image -- the helper logic is delivered per mount through cloud-init, see
`disk_mounts/helper_config.py`), a different package set, and the share
services are left *disabled* so nothing answers SMB/NFS before a mount's own
setup has written its read-only configuration.
"""

import base64
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from resurgam_worker.lab_build.errors import StepFailed
from resurgam_worker.lab_build.probe_template import (
    BUILD_TIMEOUT_SECONDS,
    DEBIAN_IMAGE_FILENAME,
    IMAGE_SOURCE_DOWNLOAD,
    IMAGE_SOURCE_NODE,
    POLL_INTERVAL_SECONDS,
    RESULT_FILE,
    WORKDIR_PREFIX,
    _guest_exec_json,
    cmd_check_node_image,
    cmd_check_tools,
    cmd_check_vmid_free,
    cmd_destroy,
    cmd_download_image,
    cmd_guest_finalize,
    cmd_guest_log,
    cmd_guest_result,
    cmd_set_description,
    cmd_shutdown,
    cmd_template,
)
from resurgam_worker.lab_build.protocols import BuildLog, Shell
from resurgam_worker.lab_build.uplink import UplinkConfig, uplink_ipconfig, uplink_nameserver, uplink_nic

TEMPLATE_NAME = "resurgam-share-helper-template"
TEMPLATE_TAG = "resurgam-share-helper"
# Bump when the package set or the in-template preparation changes; the
# readiness check reports an older template as outdated.
TEMPLATE_SCHEMA_VERSION = "1"
TEMPLATE_VERSION_MARKER = "resurgam-share-helper-template"
BUILD_LOG = "/var/log/resurgam-template-build.log"
PACKAGES = (
    "qemu-guest-agent",
    "samba",
    "nfs-kernel-server",
    "ntfs-3g",
    "lvm2",
    "xfsprogs",
    "btrfs-progs",
    "dosfstools",
    "exfatprogs",
)
# Services that must stay off until a mount's setup configured them.
DISABLED_SERVICES = ("smbd", "nmbd", "samba-ad-dc", "nfs-server")


def parse_template_version(description: str | None) -> str | None:
    if not description:
        return None
    match = re.search(rf"{TEMPLATE_VERSION_MARKER}:\s*(\S+)", description)
    return match.group(1) if match else None


@dataclass
class ShareHelperTemplateSpec:
    node: str
    vmid: int
    storage: str
    snippet_storage: str
    uplink: UplinkConfig
    uplink_address: str | None
    image_source: str
    node_image: str | None
    release_version: str
    name: str = TEMPLATE_NAME
    memory_mb: int = 1024
    cores: int = 1

    @property
    def snippet_filename(self) -> str:
        return f"resurgam-share-helper-build-{self.vmid}.yaml"

    @property
    def snippet_volume(self) -> str:
        return f"{self.snippet_storage}:snippets/{self.snippet_filename}"

    def image_ref(self, workdir: str) -> str:
        if self.image_source == IMAGE_SOURCE_DOWNLOAD:
            return f"{workdir}/{DEBIAN_IMAGE_FILENAME}"
        return self.node_image or ""


def render_build_script() -> str:
    """Runs once inside the build VM (from cloud-init) and leaves its verdict in
    RESULT_FILE (the same path the probe build uses, so the shared
    `cmd_guest_result` reads it)."""
    packages = " ".join(PACKAGES)
    services = " ".join(DISABLED_SERVICES)
    return f"""#!/bin/sh
# RESURGAM Disk Mounts share helper template build -- runs once inside the build VM.
set -u
mkdir -p "$(dirname {RESULT_FILE})"
exec >>{BUILD_LOG} 2>&1
fail() {{
    printf '{{"status":"failed","step":"%s"}}\\n' "$1" > {RESULT_FILE}
    exit 1
}}
export DEBIAN_FRONTEND=noninteractive
apt-get update || fail apt_update
apt-get install -y --no-install-recommends qemu-guest-agent || fail install_guest_agent
systemctl start qemu-guest-agent || true
apt-get install -y --no-install-recommends {packages} || fail install_packages
# Nothing may answer SMB/NFS before a mount's own read-only setup ran.
for unit in {services}; do systemctl disable --now "$unit" 2>/dev/null || true; done
command -v python3 >/dev/null || fail python3_missing
for tool in smbd smbpasswd exportfs mount.ntfs-3g vgchange mkfs.xfs btrfs; do
    command -v "$tool" >/dev/null || fail "missing_$tool"
done
printf '{{"status":"ok","template_version":"{TEMPLATE_SCHEMA_VERSION}"}}\\n' > {RESULT_FILE}
"""


def render_user_data() -> str:
    script_b64 = base64.b64encode(render_build_script().encode()).decode()
    return (
        "#cloud-config\n"
        "# RESURGAM Disk Mounts share helper template build (first boot only).\n"
        "write_files:\n"
        "  - path: /var/lib/resurgam-template-build/build.sh\n"
        "    permissions: '0700'\n"
        "    encoding: b64\n"
        f"    content: {script_b64}\n"
        "runcmd:\n"
        "  - [sh, /var/lib/resurgam-template-build/build.sh]\n"
    )


def render_description(spec: ShareHelperTemplateSpec, built_at: datetime) -> str:
    image = DEBIAN_IMAGE_FILENAME if spec.image_source == IMAGE_SOURCE_DOWNLOAD else spec.node_image
    return (
        f"RESURGAM Disk Mounts share helper template, built by RESURGAM {spec.release_version} on "
        f"{built_at.strftime('%Y-%m-%d %H:%M UTC')}.\n"
        f"{TEMPLATE_VERSION_MARKER}: {TEMPLATE_SCHEMA_VERSION}\n"
        f"base-image: {image}\n"
        f"packages: {' '.join(PACKAGES)}\n"
        "Cloned for every disk mount -- do not start or edit it. Rebuild it from RESURGAM (Disk Mounts) after an "
        "update that asks for it."
    )


def cmd_qm_create(spec: ShareHelperTemplateSpec, workdir: str) -> str:
    assert spec.uplink.bridge
    args = [
        "qm",
        "create",
        str(spec.vmid),
        "--name",
        spec.name,
        "--ostype",
        "l26",
        "--memory",
        str(spec.memory_mb),
        "--cores",
        str(spec.cores),
        "--scsihw",
        "virtio-scsi-pci",
        "--scsi0",
        f"{spec.storage}:0,import-from={spec.image_ref(workdir)}",
        "--ide2",
        f"{spec.storage}:cloudinit",
        "--boot",
        "order=scsi0",
        "--serial0",
        "socket",
        "--vga",
        "serial0",
        "--agent",
        "enabled=1",
        "--net0",
        uplink_nic(spec.uplink.bridge, spec.uplink.vlan_tag),
        "--ipconfig0",
        uplink_ipconfig(spec.uplink, spec.uplink_address),
        "--cicustom",
        f"user={spec.snippet_volume}",
        "--tags",
        TEMPLATE_TAG,
    ]
    nameserver = uplink_nameserver(spec.uplink)
    if nameserver:
        args += ["--nameserver", nameserver]
    return " ".join(shlex.quote(a) for a in args)


def cmd_detach_build_config(spec: ShareHelperTemplateSpec) -> str:
    deleted = ["cicustom", "ipconfig0", "net0"]
    if uplink_nameserver(spec.uplink):
        deleted.append("nameserver")
    return f"qm set {spec.vmid} --delete {','.join(deleted)}"


def run_share_helper_template_build(
    shell: Shell,
    log: BuildLog,
    spec: ShareHelperTemplateSpec,
    *,
    commit_template: Callable[[int], None],
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
    now: Callable[[], datetime] | None = None,
    build_timeout: float = BUILD_TIMEOUT_SECONDS,
) -> None:
    sleep = sleep or time.sleep
    monotonic = monotonic or time.monotonic
    now = now or (lambda: datetime.now(UTC))
    step = log.start(
        "privileges",
        "Logged in as root." if shell.privilege_mode == "root" else "Logged in; using sudo -n for privileged steps.",
    )
    log.finish(step, "ok")
    log.set_result(privilege_mode=shell.privilege_mode, vmid=spec.vmid, storage=spec.storage)

    step = log.start("preflight", f"Checking tools on {spec.node} and that VMID {spec.vmid} is free.")
    tools = shell.run(cmd_check_tools(("qm", "pvesm", "pvesh", "curl", "sha512sum")), privileged=False, timeout=30)
    if not tools.ok:
        raise StepFailed(
            "tools_missing",
            "Some tools are missing on the node (needed: qm, pvesm, pvesh, curl, sha512sum). Is this a PVE node?",
            tools.combined(),
        )
    free = shell.run(cmd_check_vmid_free(spec.vmid), timeout=60)
    if not free.ok:
        raise StepFailed(
            "vmid_taken",
            f"VMID {spec.vmid} is already used in this cluster. Pick another one (or leave it empty for the next free "
            "id).",
            free.combined(),
        )
    if spec.image_source == IMAGE_SOURCE_NODE:
        found = shell.run(cmd_check_node_image(spec.node_image or ""), timeout=60)
        if not found.ok:
            raise StepFailed(
                "node_image_missing",
                f"The base image '{spec.node_image}' was not found on {spec.node} (an absolute path, or a volume "
                "with content 'import').",
                found.combined(),
            )
    log.finish(step, "ok", f"Tools found; VMID {spec.vmid} is free.")

    workdir = None
    snippet_path = None
    created = False
    try:
        made = shell.run(f"mktemp -d {WORKDIR_PREFIX}XXXXXX", privileged=False, timeout=30)
        workdir = made.stdout.strip()
        if not made.ok or not workdir.startswith(WORKDIR_PREFIX):
            raise StepFailed("command_failed", "Could not create a working directory under /var/tmp.", made.combined())

        if spec.image_source == IMAGE_SOURCE_DOWNLOAD:
            step = log.start(
                "image",
                f"Downloading {DEBIAN_IMAGE_FILENAME} from cloud.debian.org on the node and checking its SHA-512.",
            )
            download = shell.run(cmd_download_image(workdir), privileged=False, timeout=1800)
            if not download.ok:
                raise StepFailed(
                    "image_download_failed",
                    "The node could not download (or verify) the Debian cloud image. It needs HTTPS access to "
                    "cloud.debian.org -- or upload the image to the node and choose 'image already on the node'.",
                    download.combined(),
                )
            log.finish(step, "ok", "Debian 13 cloud image downloaded and verified.")
        else:
            log.note("image", "ok", f"Using the base image already on the node: {spec.node_image}.")

        step = log.start("snippet", f"Writing the first-boot cloud-init config to {spec.snippet_volume}.")
        resolved = shell.run(f"pvesm path {shlex.quote(spec.snippet_volume)}", timeout=60)
        snippet_path = resolved.stdout.strip()
        if not resolved.ok or not snippet_path.startswith("/"):
            raise StepFailed(
                "snippet_storage_unusable",
                f"Storage '{spec.snippet_storage}' gave no path for a snippet (needs the 'snippets' content type).",
                resolved.combined(),
            )
        staged = f"{workdir}/user-data.yaml"
        shell.session.put_bytes(render_user_data().encode(), staged, mode=0o644)
        written = shell.run(
            f"mkdir -p {shlex.quote(snippet_path.rsplit('/', 1)[0])} && "
            f"install -m 0644 {shlex.quote(staged)} {shlex.quote(snippet_path)}",
            timeout=60,
        )
        if not written.ok:
            raise StepFailed("command_failed", "Could not write the cloud-init snippet.", written.combined())
        log.finish(step, "ok")

        step = log.start("create", f"Creating VM {spec.vmid} on '{spec.storage}' (imports the base image).")
        create = shell.run(cmd_qm_create(spec, workdir), timeout=1800)
        if not create.ok:
            raise StepFailed("vm_create_failed", "qm create failed (output below).", create.combined())
        created = True
        log.finish(step, "ok", f"VM {spec.vmid} created.")

        step = log.start(
            "first_boot",
            "Starting the build VM: it installs Samba, the NFS server and the file system tools from the Debian "
            "mirror (a few minutes).",
        )
        start = shell.run(f"qm start {spec.vmid}", timeout=120)
        if not start.ok:
            raise StepFailed("vm_start_failed", "qm start failed.", start.combined())
        deadline = monotonic() + build_timeout
        verdict = None
        while monotonic() < deadline:
            sleep(POLL_INTERVAL_SECONDS)
            verdict = _guest_exec_json(shell.run(cmd_guest_result(spec.vmid), timeout=60).stdout)
            if verdict is not None:
                break
            status = shell.run(f"qm status {spec.vmid}", timeout=30)
            if status.ok and "stopped" in status.stdout:
                raise StepFailed("build_vm_stopped", "The build VM stopped before finishing.", status.combined())
        if verdict is None:
            raise StepFailed(
                "build_timeout",
                f"The build VM gave no result within {int(build_timeout // 60)} minutes. Most often it has no network "
                "on the uplink (bridge/VLAN/IP settings) or cannot reach a Debian mirror; check its console with "
                f"`qm terminal {spec.vmid}`.",
            )
        if verdict.get("status") != "ok":
            guest_log = shell.run(cmd_guest_log(spec.vmid), timeout=60)
            raise StepFailed(
                "build_failed",
                f"The build inside the VM failed at step '{verdict.get('step')}' (its log below). Usually the Debian "
                "mirror was not reachable.",
                guest_log.combined(),
            )
        log.finish(step, "ok", "Samba, NFS server and file system tools installed inside the VM.")

        step = log.start("finalize", "Cleaning up inside the VM, shutting it down and converting it to a template.")
        cleaned = shell.run(cmd_guest_finalize(spec.vmid), timeout=180)
        if not cleaned.ok:
            raise StepFailed("finalize_failed", "Cleaning up inside the VM failed.", cleaned.combined())
        for command, failure in (
            (cmd_shutdown(spec.vmid), "The build VM did not shut down."),
            (cmd_detach_build_config(spec), "Could not remove the build-only settings."),
            (cmd_set_description(spec.vmid, render_description(spec, now())), "Could not set the description."),
            (cmd_template(spec.vmid), "qm template failed."),
        ):
            result = shell.run(command, timeout=300)
            if not result.ok:
                raise StepFailed("finalize_failed", failure, result.combined())
        commit_template(spec.vmid)
        log.set_result(template_vmid=spec.vmid, template_version=TEMPLATE_SCHEMA_VERSION)
        log.finish(step, "ok", f"Template {spec.vmid} ({spec.name}) is ready and selected for disk mounts.")
    except BaseException:
        if created:
            step = log.start("cleanup", f"Removing the unfinished VM {spec.vmid} created by this build.")
            destroyed = shell.run(cmd_destroy(spec.vmid), timeout=300)
            log.finish(
                step,
                "ok" if destroyed.ok else "warning",
                f"VM {spec.vmid} removed."
                if destroyed.ok
                else f"VM {spec.vmid} could not be removed; delete it in PVE (qm destroy {spec.vmid}).",
                None if destroyed.ok else destroyed.combined(),
            )
        raise
    finally:
        if snippet_path:
            shell.run(f"rm -f {shlex.quote(snippet_path)}", timeout=60)
        if workdir and workdir.startswith(WORKDIR_PREFIX):
            shell.run(f"rm -rf {shlex.quote(workdir)}", timeout=120)
