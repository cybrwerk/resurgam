"""Template builds run by the agent on its own node (ADR 0027).

Instead of a one-time SSH login from the appliance, a paired agent builds the
Recovery Lab probe template and the Disk Mounts share helper template itself:
the backend queues a job with a structured spec (VMID, storage, uplink NIC
settings, image source), the agent validates every field, runs the very same
build code the backend's SSH path uses (`resurgam_worker.lab_build`) through a
local shell, and reports the step log back. The agent never executes command
text it receives -- every command is generated here from validated values
(and shell-quoted by the build code).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from resurgam_worker.lab_agent.client import LabAgentApiError, LabAgentClient, SetupJob
from resurgam_worker.lab_agent.config import LabAgentConfig
from resurgam_worker.lab_build.errors import StepFailed
from resurgam_worker.lab_build.probe_template import (
    IMAGE_SOURCE_DOWNLOAD,
    IMAGE_SOURCE_NODE,
    NODE_IMAGE_PATH_PATTERN,
    NODE_IMAGE_VOLID_PATTERN,
    TemplateBuildSpec,
    run_template_build,
)
from resurgam_worker.lab_build.share_helper_template import (
    ShareHelperTemplateSpec,
    run_share_helper_template_build,
)
from resurgam_worker.lab_build.uplink import IP_MODE_DHCP, IP_MODES, UplinkConfig

logger = logging.getLogger("resurgam_worker.lab_agent.setup_jobs")

PROBER_ARTIFACT = "resurgam-lab-prober"
MAX_STEP_OUTPUT = 4000
# The step log is sent at least this often while a step runs, so the backend
# can tell a long first boot from a dead agent.
HEARTBEAT_SECONDS = 30.0

_NODE_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_STORAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9\-_.]{0,62}[A-Za-z0-9]$")
_BRIDGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]{0,14}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z.+~\-]{1,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DEB_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+\-~]*\.deb")


class InvalidSpec(StepFailed):
    def __init__(self, message: str):
        super().__init__("invalid_spec", f"The job's settings were rejected by the agent: {message}")


# --- local execution ---------------------------------------------------------------------


@dataclass(frozen=True)
class LocalResult:
    exit_status: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_status == 0

    def combined(self) -> str:
        text = (self.stdout + ("\n" if self.stdout and self.stderr else "") + self.stderr).strip()
        return text[-MAX_STEP_OUTPUT:]


class LocalSession:
    def put_bytes(self, data: bytes, remote_path: str, *, mode: int = 0o600) -> None:
        with open(remote_path, "wb") as handle:
            handle.write(data)
        os.chmod(remote_path, mode)

    def put_file(self, local_path: str, remote_path: str, *, mode: int = 0o644) -> None:
        shutil.copyfile(local_path, remote_path)
        os.chmod(remote_path, mode)


class LocalShell:
    """The build's shell, on this node. The agent runs as root (its unit's
    `User=root`), so privileged and unprivileged commands are the same."""

    privilege_mode = "root"

    def __init__(self) -> None:
        self.session = LocalSession()

    def run(self, command: str, *, privileged: bool = True, timeout: float = 120.0) -> LocalResult:
        try:
            done = subprocess.run(  # noqa: S603 -- commands come from lab_build, built from validated values
                ["/bin/sh", "-c", command], capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            return LocalResult(124, out, f"timed out after {int(timeout)} s")
        return LocalResult(done.returncode, done.stdout, done.stderr)


# --- the step log, reported to the backend --------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ReportingLog:
    """`BuildLog` that keeps the steps and sends them to the backend on every
    change (and as a heartbeat while a long step runs)."""

    def __init__(self, client: LabAgentClient, job_id: str):
        self._client = client
        self._job_id = job_id
        self._lock = threading.Lock()
        self.steps: list[dict[str, Any]] = []
        self.result: dict[str, Any] = {}

    def _send(self) -> None:
        with self._lock:
            steps = [dict(s) for s in self.steps]
            result = dict(self.result)
        try:
            self._client.report_setup_progress(self._job_id, steps, result)
        except Exception as exc:  # noqa: BLE001 -- best effort; must never kill a build or the heartbeat
            logger.warning("setup job %s: could not report progress: %s", self._job_id, exc)

    def start(self, key: str, message: str) -> dict[str, Any]:
        step = {"key": key, "status": "running", "message": message, "output": None, "started_at": _now()}
        with self._lock:
            self.steps.append(step)
        self._send()
        return step

    def finish(self, step: dict[str, Any], status: str, message: str | None = None, output: str | None = None) -> None:
        with self._lock:
            step["status"] = status
            if message is not None:
                step["message"] = message
            if output:
                step["output"] = output[-MAX_STEP_OUTPUT:]
            step["finished_at"] = _now()
        self._send()

    def note(self, key: str, status: str, message: str, output: str | None = None) -> None:
        self.finish(self.start(key, message), status, output=output)

    def set_result(self, **values: Any) -> None:
        with self._lock:
            self.result.update(values)

    def fail_running(self, message: str, output: str | None) -> None:
        with self._lock:
            for step in self.steps:
                if step["status"] == "running":
                    step["status"] = "failed"
                    step["message"] = message
                    if output:
                        step["output"] = output[-MAX_STEP_OUTPUT:]
                    step["finished_at"] = _now()

    def heartbeat(self, stop: threading.Event) -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                self._send()
            except Exception:  # pragma: no cover -- _send already swallows; belt and braces for the thread
                logger.exception("setup job %s: heartbeat failed", self._job_id)

    def snapshot(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        with self._lock:
            return [dict(s) for s in self.steps], dict(self.result)


# --- spec validation -------------------------------------------------------------------


def _str(spec: dict, key: str, pattern: re.Pattern[str], label: str) -> str:
    value = spec.get(key)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise InvalidSpec(f"{label} ({key}) is missing or malformed.")
    return value


def _int(spec: dict, key: str, low: int, high: int, label: str, *, optional: bool = False) -> int | None:
    value = spec.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise InvalidSpec(f"{label} ({key}) must be a number from {low} to {high}.")
    return value


def _ipv4(value: Any, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    try:
        return str(ipaddress.IPv4Address(value))
    except (ipaddress.AddressValueError, ValueError, TypeError) as exc:
        raise InvalidSpec(f"{label} is not an IPv4 address.") from exc


def parse_uplink(raw: Any) -> UplinkConfig:
    if not isinstance(raw, dict):
        raise InvalidSpec("uplink settings are missing.")
    bridge = _str(raw, "bridge", _BRIDGE_RE, "the uplink bridge")
    vlan = _int(raw, "vlan_tag", 1, 4094, "the uplink VLAN tag", optional=True)
    mode = raw.get("ip_mode") or IP_MODE_DHCP
    if mode not in IP_MODES:
        raise InvalidSpec("the uplink IP mode is unknown.")
    if mode == IP_MODE_DHCP:
        return UplinkConfig(bridge=bridge, vlan_tag=vlan)
    dns = raw.get("dns_servers") or []
    if not isinstance(dns, list) or len(dns) > 3:
        raise InvalidSpec("the uplink DNS servers are malformed.")
    return UplinkConfig(
        bridge=bridge,
        vlan_tag=vlan,
        ip_mode=mode,
        range_start=_ipv4(raw.get("range_start"), "the uplink range start"),
        range_end=_ipv4(raw.get("range_end"), "the uplink range end"),
        prefix_length=_int(raw, "prefix_length", 1, 32, "the uplink prefix length"),
        gateway=_ipv4(raw.get("gateway"), "the uplink gateway"),
        dns_servers=[_ipv4(d, "an uplink DNS server") for d in dns],
    )


@dataclass(frozen=True)
class CommonSpec:
    vmid: int
    storage: str
    snippet_storage: str
    uplink: UplinkConfig
    uplink_address: str | None
    image_source: str
    node_image: str | None
    release_version: str


def parse_common(spec: dict) -> CommonSpec:
    if not isinstance(spec, dict):
        raise InvalidSpec("the spec is not an object.")
    image_source = spec.get("image_source")
    if image_source not in (IMAGE_SOURCE_DOWNLOAD, IMAGE_SOURCE_NODE):
        raise InvalidSpec("the image source is unknown.")
    node_image = None
    if image_source == IMAGE_SOURCE_NODE:
        node_image = spec.get("node_image")
        if (
            not isinstance(node_image, str)
            or not (
                re.fullmatch(NODE_IMAGE_PATH_PATTERN, node_image)
                or (re.fullmatch(NODE_IMAGE_VOLID_PATTERN, node_image) and not node_image.startswith("/"))
            )
            or ".." in node_image.split("/")
        ):
            raise InvalidSpec("the node image must be an absolute path or a PVE volume id.")
    uplink = parse_uplink(spec.get("uplink"))
    address = _ipv4(spec.get("uplink_address"), "the uplink address", optional=True)
    if uplink.is_static and address is None:
        raise InvalidSpec("a static uplink needs an allocated address.")
    return CommonSpec(
        vmid=_int(spec, "vmid", 100, 999_999_999, "the VMID"),
        storage=_str(spec, "storage", _STORAGE_RE, "the target storage"),
        snippet_storage=_str(spec, "snippet_storage", _STORAGE_RE, "the snippet storage"),
        uplink=uplink,
        uplink_address=address,
        image_source=image_source,
        node_image=node_image,
        release_version=_str(spec, "release_version", _VERSION_RE, "the RESURGAM version"),
    )


# --- running a job ---------------------------------------------------------------------


@dataclass(frozen=True)
class DownloadedPackage:
    name: str
    filename: str
    version: str
    sha256: str
    path: Path


def fetch_prober(client: LabAgentClient, workdir: str, log: ReportingLog) -> DownloadedPackage:
    step = log.start("package", "Downloading resurgam-lab-prober from RESURGAM.")
    info = client.setup_artifact(PROBER_ARTIFACT)
    if (
        not _DEB_RE.fullmatch(info.filename)
        or not _SHA256_RE.fullmatch(info.sha256)
        or not _VERSION_RE.fullmatch(info.version)
    ):
        raise StepFailed("artifact_invalid", "RESURGAM described the prober package with invalid values.")
    destination = os.path.join(workdir, info.filename)
    client.download_setup_artifact(PROBER_ARTIFACT, destination)
    check = LocalShell().run(f"sha256sum {shlex.quote(destination)}", timeout=120)
    if not check.ok or check.stdout.split()[:1] != [info.sha256]:
        raise StepFailed(
            "upload_corrupted", "The downloaded prober package's SHA-256 does not match.", check.combined()
        )
    log.finish(step, "ok", f"resurgam-lab-prober {info.version} downloaded and verified.")
    return DownloadedPackage(PROBER_ARTIFACT, info.filename, info.version, info.sha256, Path(destination))


def this_node(config: LabAgentConfig) -> str:
    return config.node_name or socket.gethostname().split(".")[0]


def run_setup_job(job: SetupJob, config: LabAgentConfig, client: LabAgentClient, log: ReportingLog) -> None:
    """Validates and runs one job; raises StepFailed on any failure."""
    node = this_node(config)
    if job.node != node or not _NODE_RE.fullmatch(job.node):
        raise StepFailed(
            "wrong_node",
            f"This job is for node '{job.node}', but this agent runs on '{node}' (set RESURGAM_LAB_AGENT_NODE_NAME "
            "if the PVE node name differs from the host name).",
        )
    common = parse_common(job.spec)
    shell = LocalShell()
    if job.kind == "template_build":
        workdir = tempfile.mkdtemp(prefix="resurgam-setup-")
        try:
            prober = fetch_prober(client, workdir, log)
            spec = TemplateBuildSpec(
                node=node,
                vmid=common.vmid,
                storage=common.storage,
                snippet_storage=common.snippet_storage,
                uplink=common.uplink,
                uplink_address=common.uplink_address,
                image_source=common.image_source,
                node_image=common.node_image,
                prober=prober,
                release_version=common.release_version,
            )
            run_template_build(shell, log, spec, commit_template=lambda vmid, version: None)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    elif job.kind == "share_helper_template_build":
        spec = ShareHelperTemplateSpec(
            node=node,
            vmid=common.vmid,
            storage=common.storage,
            snippet_storage=common.snippet_storage,
            uplink=common.uplink,
            uplink_address=common.uplink_address,
            image_source=common.image_source,
            node_image=common.node_image,
            release_version=common.release_version,
        )
        run_share_helper_template_build(shell, log, spec, commit_template=lambda vmid: None)
    else:  # pragma: no cover -- the client model only admits the two kinds
        raise StepFailed("unknown_kind", f"Unknown setup job kind '{job.kind}'.")


FINAL_REPORT_ATTEMPTS = 10


def _report_final(client: LabAgentClient, job_id: str, log: ReportingLog, **kwargs: Any) -> None:
    """Sends the outcome, retrying through backend outages (an appliance update
    restarts it for a minute or two) -- losing it would leave the job `running`
    until the backend's stale-job timeout."""
    steps, result = log.snapshot()
    for attempt in range(FINAL_REPORT_ATTEMPTS):
        try:
            client.report_setup_status(job_id, steps=steps, result=result, **kwargs)
            return
        except LabAgentApiError as exc:
            if 400 <= exc.status_code < 500 and exc.status_code not in (408, 429):
                logger.warning("setup job %s: the backend rejected the outcome: %s", job_id, exc)
                return
            logger.warning("setup job %s: reporting the outcome failed (attempt %d): %s", job_id, attempt + 1, exc)
        except Exception as exc:  # noqa: BLE001 -- keep retrying whatever the failure
            logger.warning("setup job %s: reporting the outcome failed (attempt %d): %s", job_id, attempt + 1, exc)
        if attempt + 1 < FINAL_REPORT_ATTEMPTS:
            time.sleep(min(60, 2**attempt))
    logger.error("setup job %s: giving up reporting the outcome", job_id)


def process_setup_job(job: SetupJob, config: LabAgentConfig, client: LabAgentClient) -> None:
    client.claim_setup_job(job.id)
    logger.info("setup job %s: claimed (%s, node %s)", job.id, job.kind, job.node)
    log = ReportingLog(client, job.id)
    stop = threading.Event()
    beat = threading.Thread(target=log.heartbeat, args=(stop,), daemon=True)
    beat.start()
    try:
        run_setup_job(job, config, client, log)
    except StepFailed as exc:
        log.fail_running(exc.message, exc.output)
        _report_final(client, job.id, log, status="failed", error_code=exc.code, error_message=exc.message)
        logger.warning("setup job %s failed: %s", job.id, exc.message)
    except Exception as exc:
        logger.exception("setup job %s: unexpected failure", job.id)
        log.fail_running(f"Unexpected error: {exc}", None)
        _report_final(client, job.id, log, status="failed", error_code="agent_error", error_message=str(exc))
    else:
        _report_final(client, job.id, log, status="succeeded")
        logger.info("setup job %s succeeded", job.id)
    finally:
        stop.set()
