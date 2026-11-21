"""Builds the Recovery Lab probe appliance template on a lab node.

How the template was built so far (docs/recovery-lab-deploy-pipeline.md,
ADR 0020): by hand -- clone a Debian cloud image VM, inject throwaway
cloud-init credentials, SSH in, install `resurgam-lab-prober` plus
`qemu-guest-agent`, `dnsmasq`, `nftables`, `nmap`, strip the credentials,
convert to a template (VMIDs 9000 -> 9002 -> 9003 on the dev cluster). That
procedure produced real bugs and left no record of what a template holds.

What RESURGAM does now, with nothing interactive and no credentials inside
the guest. The build needs `qm`/`pvesm` on the node and runs either over the
one-time SSH session of the guided install (the backend's `RemoteShell`) or
locally on the node by the paired lab agent (`lab_agent.setup_jobs.LocalShell`,
ADR 0027) -- both satisfy the same `Shell` protocol, and the step log goes to
a `BuildLog` (the backend's `JobLog` / the agent's `ReportingLog`):

1. Base image: Debian 13 `genericcloud` (the image appliance/packer already
   uses), downloaded *on the node* from cloud.debian.org and checked against
   Debian's SHA512SUMS -- the node needs HTTPS access to cloud.debian.org.
   Offline: an image already on the node (absolute path, or a PVE volume
   with content `import`, e.g. `local:import/debian-13-genericcloud-amd64.qcow2`).
2. A small seed ISO (`genisoimage`, a qemu-server dependency for cloud-init)
   with the `resurgam-lab-prober` .deb shipped with this backend and a build
   script.
3. `qm create` with the image imported as scsi0 (`import-from`, pve-docs
   "Cloud-Init Support"), a cloud-init drive, the seed ISO, serial console,
   `agent: 1`, and the *lab uplink* NIC (bridge, VLAN tag, DHCP or an
   address from the static range) -- the build VM needs a Debian mirror for
   `qemu-guest-agent`, `dnsmasq`, `nftables`, `nmap`.
4. First boot: cloud-init (`cicustom` user data on the snippet storage) runs
   the build script, which writes a result file; RESURGAM polls it through
   the guest agent (`qm guest exec`).
5. Clean up inside the guest (`cloud-init clean`, machine-id, SSH host keys,
   so every clone gets fresh ones), shut down, drop the build-only config
   (seed ISO, `cicustom`, `ipconfig0`, `net0`), record the prober version in
   the VM description (`resurgam-lab-prober-version: <v>` -- read back by
   the readiness check), `qm template`.
6. On failure, the VM created by *this* build (and only it) is destroyed.

`render_manual_script` emits the same commands as a shell script for the
manual path.
"""

import json
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from resurgam_worker.lab_build.errors import StepFailed
from resurgam_worker.lab_build.protocols import BuildLog, PackageFile, Shell
from resurgam_worker.lab_build.uplink import UplinkConfig, uplink_ipconfig, uplink_nameserver, uplink_nic

# Same base image as appliance/packer/resurgam.pkr.hcl.
DEBIAN_IMAGE_FILENAME = "debian-13-genericcloud-amd64.qcow2"
DEBIAN_IMAGE_URL = f"https://cloud.debian.org/images/cloud/trixie/latest/{DEBIAN_IMAGE_FILENAME}"
DEBIAN_SHA512SUMS_URL = "https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS"

TEMPLATE_NAME = "resurgam-lab-appliance-template"
TEMPLATE_TAG = "resurgam-lab-appliance"
PROBER_VERSION_MARKER = "resurgam-lab-prober-version"
SEED_LABEL = "RESURGAMSEED"
RESULT_FILE = "/var/lib/resurgam-template-build/result.json"
BUILD_LOG = "/var/log/resurgam-template-build.log"
EXTRA_PACKAGES = ("qemu-guest-agent",)  # dnsmasq/nftables/nmap/iproute2/iputils-ping come as prober Depends

IMAGE_SOURCE_DOWNLOAD = "download"
IMAGE_SOURCE_NODE = "node_image"

NODE_IMAGE_PATH_PATTERN = r"^/[A-Za-z0-9_.+\-/]+$"
NODE_IMAGE_VOLID_PATTERN = r"^[A-Za-z][A-Za-z0-9\-_.]*:[A-Za-z0-9_.+\-/]+$"

BUILD_TIMEOUT_SECONDS = 1800
POLL_INTERVAL_SECONDS = 10
WORKDIR_PREFIX = "/var/tmp/resurgam-template."


def is_volume_id(image: str) -> bool:
    return bool(re.match(NODE_IMAGE_VOLID_PATTERN, image)) and not image.startswith("/")


def parse_prober_version(description: str | None) -> str | None:
    """The `resurgam-lab-prober-version: <v>` line RESURGAM writes into a
    template's description, or None (a hand-built or older template)."""
    if not description:
        return None
    match = re.search(rf"{PROBER_VERSION_MARKER}:\s*(\S+)", description)
    return match.group(1) if match else None


@dataclass
class TemplateBuildSpec:
    node: str
    vmid: int
    storage: str
    snippet_storage: str
    uplink: UplinkConfig
    uplink_address: str | None
    image_source: str
    node_image: str | None
    prober: PackageFile
    release_version: str
    name: str = TEMPLATE_NAME
    memory_mb: int = 1024
    cores: int = 1

    @property
    def snippet_filename(self) -> str:
        return f"resurgam-template-build-{self.vmid}.yaml"

    @property
    def snippet_volume(self) -> str:
        return f"{self.snippet_storage}:snippets/{self.snippet_filename}"

    @property
    def seed_iso_volume(self) -> str:
        """The seed ISO as a volume on the snippet storage (content `iso`).
        PVE 9 refuses a drive path that belongs to no storage ("unable to
        associate path ... to any storage", qemu-server `cleanup_drive_path`)
        -- found live 2026-11-21: `--ide3 /var/tmp/.../seed.iso` failed."""
        return f"{self.snippet_storage}:iso/resurgam-template-build-{self.vmid}-seed.iso"

    def image_ref(self, workdir: str) -> str:
        if self.image_source == IMAGE_SOURCE_DOWNLOAD:
            return f"{workdir}/{DEBIAN_IMAGE_FILENAME}"
        return self.node_image or ""


def render_build_script(spec: TemplateBuildSpec) -> str:
    """Runs once inside the build VM (from the seed ISO, started by
    cloud-init) and leaves its verdict in RESULT_FILE."""
    packages = " ".join(EXTRA_PACKAGES)
    return f"""#!/bin/sh
# RESURGAM probe appliance template build -- runs once inside the build VM.
set -u
SEED=$(dirname "$0")
mkdir -p "$(dirname {RESULT_FILE})"
exec >>{BUILD_LOG} 2>&1
fail() {{
    printf '{{"status":"failed","step":"%s"}}\\n' "$1" > {RESULT_FILE}
    exit 1
}}
export DEBIAN_FRONTEND=noninteractive
apt-get update || fail apt_update
apt-get install -y --no-install-recommends {packages} || fail install_guest_agent
systemctl start qemu-guest-agent || true
apt-get install -y --no-install-recommends "$SEED"/{spec.prober.filename} || fail install_prober
# The router bootstrap starts dnsmasq with its own per-run config; nothing
# should answer DHCP/DNS on the uplink before that.
systemctl disable --now dnsmasq || true
version=$(dpkg-query -W -f='${{Version}}' resurgam-lab-prober) || fail verify_prober
printf '{{"status":"ok","prober_version":"%s"}}\\n' "$version" > {RESULT_FILE}
"""


def render_user_data() -> str:
    mount = "/mnt/resurgam-seed"
    return (
        "#cloud-config\n"
        "# RESURGAM probe appliance template build (first boot only).\n"
        "runcmd:\n"
        f"  - [sh, -c, 'mkdir -p {mount} && mount -o ro /dev/disk/by-label/{SEED_LABEL} {mount} && "
        f"sh {mount}/build.sh; umount {mount} || true']\n"
    )


def render_description(spec: TemplateBuildSpec, prober_version: str, built_at: datetime) -> str:
    image = DEBIAN_IMAGE_FILENAME if spec.image_source == IMAGE_SOURCE_DOWNLOAD else spec.node_image
    return (
        f"RESURGAM Recovery Lab probe appliance template, built by RESURGAM {spec.release_version} on "
        f"{built_at.strftime('%Y-%m-%d %H:%M UTC')}.\n"
        f"{PROBER_VERSION_MARKER}: {prober_version}\n"
        f"base-image: {image}\n"
        "Cloned for every lab run -- do not start or edit it. Rebuild it from RESURGAM after an update."
    )


# --- the commands, shared by the guided run and the manual script ----------


PROBE_TEMPLATE_TOOLS = ("qm", "pvesm", "pvesh", "genisoimage", "curl", "sha512sum", "sha256sum")


def cmd_check_tools(tools: tuple[str, ...]) -> str:
    """Exits non-zero and prints `missing: <names>` unless every tool is on
    PATH. One `command -v` per tool: with several names, dash's
    `command -v` (the agent's `/bin/sh`) only reports the first -- found
    live 2026-11-20, every agent-run build failed its preflight with all
    tools installed (bash, the SSH path's login shell, reports them all)."""
    names = " ".join(shlex.quote(t) for t in tools)
    return (
        f'missing=; for t in {names}; do command -v "$t" >/dev/null 2>&1 || missing="$missing $t"; done; '
        '[ -z "$missing" ] || { echo "missing:$missing"; exit 1; }'
    )


def cmd_check_vmid_free(vmid: int) -> str:
    # `GET /cluster/nextid?vmid=N` fails if N is taken anywhere in the cluster.
    return f"pvesh get /cluster/nextid --vmid {vmid}"


def cmd_download_image(workdir: str) -> str:
    image = f"{workdir}/{DEBIAN_IMAGE_FILENAME}"
    sums = f"{workdir}/SHA512SUMS"
    return (
        f"curl -fsSL --retry 3 -o {sums} {DEBIAN_SHA512SUMS_URL} && "
        f"curl -fsSL --retry 3 -o {image} {DEBIAN_IMAGE_URL} && "
        f"expected=$(awk '$2==\"{DEBIAN_IMAGE_FILENAME}\" {{print $1}}' {sums}) && "
        f'actual=$(sha512sum {image} | cut -d" " -f1) && '
        '[ -n "$expected" ] && [ "$expected" = "$actual" ]'
    )


def cmd_check_node_image(image: str) -> str:
    if is_volume_id(image):
        return f"pvesm path {shlex.quote(image)}"
    return f"test -r {shlex.quote(image)}"


def cmd_seed_iso(workdir: str) -> str:
    return f"genisoimage -quiet -V {SEED_LABEL} -r -J -o {workdir}/seed.iso {workdir}/seed"


def cmd_qm_create(spec: TemplateBuildSpec, workdir: str) -> str:
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
        "--ide3",
        f"{spec.seed_iso_volume},media=cdrom",
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


def cmd_guest_result(vmid: int) -> str:
    return f"qm guest exec {vmid} --timeout 20 -- cat {RESULT_FILE}"


def cmd_guest_log(vmid: int) -> str:
    return f"qm guest exec {vmid} --timeout 20 -- tail -n 40 {BUILD_LOG}"


def cmd_guest_finalize(vmid: int) -> str:
    inner = (
        "rm -rf /var/lib/resurgam-template-build && cloud-init clean --logs --seed && rm -f /etc/ssh/ssh_host_* && "
        "truncate -s 0 /etc/machine-id && rm -f /var/lib/dbus/machine-id && sync"
    )
    return f"qm guest exec {vmid} --timeout 120 -- /bin/sh -c {shlex.quote(inner)}"


def cmd_shutdown(vmid: int) -> str:
    return f"qm shutdown {vmid} --timeout 180 || qm stop {vmid}"


def cmd_detach_build_config(spec: TemplateBuildSpec) -> str:
    deleted = ["ide3", "cicustom", "ipconfig0", "net0"]
    if uplink_nameserver(spec.uplink):
        deleted.append("nameserver")
    return f"qm set {spec.vmid} --delete {','.join(deleted)}"


def cmd_set_description(vmid: int, description: str) -> str:
    return f"qm set {vmid} --description {shlex.quote(description)}"


def cmd_template(vmid: int) -> str:
    return f"qm template {vmid}"


def cmd_destroy(vmid: int) -> str:
    return f"qm stop {vmid} --skiplock 1 >/dev/null 2>&1; qm destroy {vmid} --purge 1 --destroy-unreferenced-disks 1"


def _guest_exec_json(stdout: str) -> dict | None:
    """`qm guest exec` prints `{"exitcode":0,"exited":1,"out-data":"..."}`."""
    try:
        payload = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("exitcode") != 0:
        return None
    try:
        return json.loads(payload.get("out-data") or "")
    except ValueError:
        return None


def run_template_build(
    shell: Shell,
    log: BuildLog,
    spec: TemplateBuildSpec,
    *,
    commit_template: Callable[[int, str], None],
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
    tools = shell.run(cmd_check_tools(PROBE_TEMPLATE_TOOLS), privileged=False, timeout=30)
    if not tools.ok:
        raise StepFailed(
            "tools_missing",
            "Some tools are missing on the node (needed: qm, pvesm, pvesh, genisoimage, curl, sha512sum, sha256sum). "
            "Is this a PVE node? genisoimage: apt-get install genisoimage.",
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
    iso_path = ""
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

        step = log.start("seed", f"Uploading {spec.prober.filename} and the build script (seed ISO).")
        seed_dir = f"{workdir}/seed"
        shell.run(f"mkdir -p {seed_dir}", privileged=False, timeout=30)
        remote_deb = f"{seed_dir}/{spec.prober.filename}"
        shell.session.put_file(str(spec.prober.path), remote_deb, mode=0o644)
        digest = shell.run(f"sha256sum {shlex.quote(remote_deb)}", privileged=False, timeout=60)
        if not digest.ok or digest.stdout.split()[:1] != [spec.prober.sha256]:
            raise StepFailed(
                "upload_corrupted", "The uploaded prober package's SHA-256 does not match.", digest.combined()
            )
        shell.session.put_bytes(render_build_script(spec).encode(), f"{seed_dir}/build.sh", mode=0o644)
        iso = shell.run(cmd_seed_iso(workdir), privileged=False, timeout=120)
        if not iso.ok:
            raise StepFailed("command_failed", "genisoimage could not build the seed ISO.", iso.combined())
        resolved_iso = shell.run(f"pvesm path {shlex.quote(spec.seed_iso_volume)}", timeout=60)
        iso_path = resolved_iso.stdout.strip()
        if not resolved_iso.ok or not iso_path.startswith("/"):
            raise StepFailed(
                "snippet_storage_unusable",
                f"Storage '{spec.snippet_storage}' gave no path for the seed ISO (needs the 'iso' content type besides "
                "'snippets').",
                resolved_iso.combined(),
            )
        placed = shell.run(
            f"mkdir -p {shlex.quote(iso_path.rsplit('/', 1)[0])} && "
            f"install -m 0644 {shlex.quote(workdir + '/seed.iso')} {shlex.quote(iso_path)}",
            timeout=120,
        )
        if not placed.ok:
            raise StepFailed(
                "command_failed", "Could not place the seed ISO on the snippet storage.", placed.combined()
            )
        log.finish(step, "ok", f"Seed ISO with resurgam-lab-prober {spec.prober.version} ready.")

        step = log.start("snippet", f"Writing the first-boot cloud-init config to {spec.snippet_volume}.")
        resolved = shell.run(f"pvesm path {shlex.quote(spec.snippet_volume)}", timeout=60)
        snippet_path = resolved.stdout.strip()
        if not resolved.ok or not snippet_path.startswith("/"):
            raise StepFailed(
                "snippet_storage_unusable",
                f"Storage '{spec.snippet_storage}' gave no path for a snippet (needs the 'snippets' content type, see "
                "the storage step).",
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
            "Starting the build VM: it installs the prober and its tools from the Debian mirror (a few minutes).",
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
            parsed = None
            try:
                parsed = json.loads(guest_log.stdout).get("out-data")
            except (ValueError, AttributeError):
                parsed = guest_log.combined()
            raise StepFailed(
                "build_failed",
                f"The build inside the VM failed at step '{verdict.get('step')}' (its log below). Usually the Debian "
                "mirror was not reachable.",
                parsed,
            )
        prober_version = str(verdict.get("prober_version") or "")
        if prober_version != spec.prober.version:
            raise StepFailed(
                "prober_version_mismatch",
                f"The VM reports resurgam-lab-prober {prober_version or 'unknown'}, expected {spec.prober.version}.",
            )
        log.finish(step, "ok", f"resurgam-lab-prober {prober_version} installed inside the VM.")

        step = log.start("finalize", "Cleaning up inside the VM, shutting it down and converting it to a template.")
        cleaned = shell.run(cmd_guest_finalize(spec.vmid), timeout=180)
        if not cleaned.ok:
            raise StepFailed("finalize_failed", "Cleaning up inside the VM failed.", cleaned.combined())
        for command, failure in (
            (cmd_shutdown(spec.vmid), "The build VM did not shut down."),
            (cmd_detach_build_config(spec), "Could not remove the build-only settings."),
            (
                cmd_set_description(spec.vmid, render_description(spec, prober_version, now())),
                "Could not set the template description.",
            ),
            (cmd_template(spec.vmid), "qm template failed."),
        ):
            result = shell.run(command, timeout=300)
            if not result.ok:
                raise StepFailed("finalize_failed", failure, result.combined())
        commit_template(spec.vmid, prober_version)
        log.set_result(template_vmid=spec.vmid, prober_version=prober_version)
        log.finish(step, "ok", f"Template {spec.vmid} ({spec.name}) is ready and selected for this cluster.")
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
        if iso_path:
            shell.run(f"rm -f {shlex.quote(iso_path)}", timeout=60)
        if workdir and workdir.startswith(WORKDIR_PREFIX):
            shell.run(f"rm -rf {shlex.quote(workdir)}", timeout=120)


def render_manual_script(spec: TemplateBuildSpec, *, deb_download_url: str) -> str:
    """The same build as a shell script to run as root on the node. Uses a
    fixed working directory so every path in it is literal (the generated
    commands quote their arguments, which would stop `$VAR` expansion)."""
    workdir = f"{WORKDIR_PREFIX}{spec.vmid}"
    image_block = (
        f"# Base image: downloaded here (needs HTTPS to cloud.debian.org), checked against Debian's SHA512SUMS.\n"
        f"{cmd_download_image(workdir)}\n"
        if spec.image_source == IMAGE_SOURCE_DOWNLOAD
        else f"# Base image already on the node:\n{cmd_check_node_image(spec.node_image or '')}\n"
    )
    nameserver_note = (
        f"# Uplink: static address {spec.uplink_address} from the RESURGAM range (reserved only while a guided build "
        "runs -- make sure it is free now).\n"
        if spec.uplink.is_static
        else ""
    )
    return f"""#!/bin/sh
# RESURGAM: build the Recovery Lab probe appliance template on node '{spec.node}' (run as root there).
# The same steps RESURGAM's guided "Build template" runs. Afterwards pick VMID {spec.vmid} as the template.
set -eu
# 1. Copy {spec.prober.filename} (SHA-256 {spec.prober.sha256}) to this node first, e.g. download it
#    from RESURGAM ({deb_download_url}) and scp it to /root/.
DEB=/root/{spec.prober.filename}
echo "{spec.prober.sha256}  $DEB" | sha256sum -c -

{cmd_check_vmid_free(spec.vmid)}
WORKDIR={workdir}
rm -rf "$WORKDIR" && mkdir -p "$WORKDIR"
trap 'rm -rf "$WORKDIR"' EXIT

{image_block}
# 2. Seed ISO with the prober package and the in-guest build script.
mkdir -p "$WORKDIR/seed"
cp "$DEB" "$WORKDIR/seed/"
cat > "$WORKDIR/seed/build.sh" <<'RESURGAM_BUILD_SH'
{render_build_script(spec)}RESURGAM_BUILD_SH
{cmd_seed_iso(workdir)}
SEED_ISO=$(pvesm path {spec.seed_iso_volume})
mkdir -p "$(dirname "$SEED_ISO")"
install -m 0644 "$WORKDIR/seed.iso" "$SEED_ISO"

# 3. First-boot cloud-init config on the snippet storage.
SNIPPET=$(pvesm path {spec.snippet_volume})
mkdir -p "$(dirname "$SNIPPET")"
cat > "$SNIPPET" <<'RESURGAM_USER_DATA'
{render_user_data()}RESURGAM_USER_DATA

# 4. Build VM on the lab uplink (bridge/VLAN/IP as configured in RESURGAM).
{nameserver_note}{cmd_qm_create(spec, workdir)}
qm start {spec.vmid}

# 5. Wait for the in-guest build (up to 30 minutes).
for i in $(seq 1 180); do
  sleep 10
  if {cmd_guest_result(spec.vmid)} 2>/dev/null | grep -q '\\\\"status\\\\":\\\\"ok\\\\"'; then ok=1; break; fi
done
[ "${{ok:-}}" = 1 ] || {{ echo "build did not finish -- see: {cmd_guest_log(spec.vmid)}" >&2; exit 1; }}

# 6. Clean up inside the guest, shut down, drop build-only settings, convert to a template.
{cmd_guest_finalize(spec.vmid)}
{cmd_shutdown(spec.vmid)}
{cmd_detach_build_config(spec)}
{cmd_set_description(spec.vmid, render_description(spec, spec.prober.version, datetime.now(UTC)))}
{cmd_template(spec.vmid)}
rm -f "$SNIPPET" "$SEED_ISO"
echo "Template {spec.vmid} is ready."
"""
