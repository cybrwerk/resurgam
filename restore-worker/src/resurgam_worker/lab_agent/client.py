from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

import httpx
from pydantic import BaseModel


class LabAgentJob(BaseModel):
    """Mirrors `backend`'s `schemas.lab_agent.LabAgentJobOut` -- kept as
    a plain duplicate rather than a shared import, since this package
    deliberately has no dependency on `backend` (ADR 0019: stays
    dependency-light, installable on infrastructure that never runs the
    RESURGAM backend itself).
    """

    member_id: str
    pbs_host: str
    pbs_port: int
    pbs_datastore: str
    pbs_namespace: str
    backup_type: str
    backup_id: str
    snapshot_time: str
    target_vmid: int
    target_node: str
    target_storage: str
    vnet_name: str
    pool: str
    # PBS backup-encryption keyfile content, when the snapshot's datastore
    # has one configured (docs/adr/0022) -- see the backend schema's own
    # docstring for why this is pushed per-job rather than left to local
    # agent config.
    encryption_keyfile: str | None = None


class ApplianceSnippetJob(BaseModel):
    """Mirrors `backend`'s `schemas.restore_test.ApplianceSnippetJobOut`
    -- a Recovery Lab run whose probe appliance cloud-init snippet still
    needs writing to local disk (Phase B: PVE has no REST endpoint to
    upload one, confirmed live 2026-09-21).
    """

    plan_id: str
    snippet_storage_id: str
    filename: str
    content: str


class FileIndexJob(BaseModel):
    """Mirrors `backend`'s `schemas.lab_agent.LabAgentFileIndexJobOut`
    (ADR 0021) -- covers both opt-in guest-exec methods; the
    `guest_exec_ephemeral`-only boot fields are null for
    `guest_exec_live`.
    """

    id: str
    file_index_method: Literal["guest_exec_live", "guest_exec_ephemeral"]
    target_vmid: int
    target_node: str
    exclude_patterns: list[str] = []
    target_storage: str | None = None
    pbs_host: str | None = None
    pbs_port: int | None = None
    pbs_datastore: str | None = None
    pbs_namespace: str | None = None
    backup_type: str | None = None
    backup_id: str | None = None
    snapshot_time: str | None = None
    pool: str | None = None
    encryption_keyfile: str | None = None


class FileIndexJobClaim(BaseModel):
    since: str | None = None
    is_full_reindex: bool


class FileIndexEntry(BaseModel):
    path: str
    filename: str
    type: Literal["file", "directory", "symlink", "other"]
    size_bytes: int | None = None
    mtime: str | None = None


class DiskMountJob(BaseModel):
    """Mirrors `backend`'s `schemas.disk_mount.DiskMountAgentJobOut` (ADR 0025)."""

    id: str
    kind: Literal["provision", "teardown"]
    target_node: str
    helper_name: str
    helper_vmid: int | None = None
    mapped_devices: list[dict] = []
    snippet_volume: str | None = None
    snapshot_path: str


class DiskMountDisk(BaseModel):
    archive: str
    serial: str
    slot: str


class DiskMountClaim(BaseModel):
    """Mirrors `backend`'s `schemas.disk_mount.DiskMountClaimOut` -- carries the
    share password; never log or persist it."""

    id: str
    target_node: str
    target_storage: str | None = None
    helper_name: str
    helper_template_vmid: int
    pbs_host: str
    pbs_port: int
    pbs_datastore: str
    pbs_namespace: str = ""
    snapshot_path: str
    disks: list[DiskMountDisk]
    net0: str
    ipconfig0: str
    snippet_storage: str
    snippet_filename: str
    user_data: str
    smb_username: str | None = None
    share_password: str | None = None
    encryption_keyfile: str | None = None

    def __repr__(self) -> str:  # keep secrets out of any accidental log line
        return f"DiskMountClaim(id={self.id!r}, helper_name={self.helper_name!r})"

    __str__ = __repr__


class InstantRecoveryJob(BaseModel):
    """Mirrors `backend`'s `schemas.instant_recovery.InstantRecoveryAgentJobOut`
    (ADR 0030). `start` must be claimed first; `watch` is a VM running from the
    backup (report a heartbeat); `finish` unmaps after every disk was moved;
    `discard` removes the VM and everything RESURGAM created for it."""

    id: str
    kind: Literal["start", "watch", "finish", "discard"]
    target_node: str
    target_vmid: int
    name: str = ""
    vm_created: bool = False
    mapped_devices: list[dict] = []
    snapshot_path: str


class InstantRecoveryClaim(BaseModel):
    """Mirrors `backend`'s `schemas.instant_recovery.InstantRecoveryClaimOut` --
    may carry the PBS encryption key; never log or persist it."""

    id: str
    target_node: str
    target_vmid: int
    target_storage: str
    name: str
    pbs_host: str
    pbs_port: int
    pbs_datastore: str
    pbs_namespace: str = ""
    snapshot_path: str
    network_mode: Literal["none", "isolated", "original", "custom"]
    network_bridge: str | None = None
    keep_mac: bool
    min_free_bytes: int
    encryption_keyfile: str | None = None

    def __repr__(self) -> str:
        return f"InstantRecoveryClaim(id={self.id!r}, target_vmid={self.target_vmid!r})"

    __str__ = __repr__


class SetupJob(BaseModel):
    """Mirrors `backend`'s `schemas.lab_agent_setup.LabAgentSetupJobOut`
    (ADR 0027): a template build to run on this node. `spec` is structured
    data only -- `lab_agent.setup_jobs` validates it and runs the build from
    `resurgam_worker.lab_build`; the agent never runs commands it is sent."""

    id: str
    kind: Literal["template_build", "share_helper_template_build"]
    node: str
    spec: dict


class SetupArtifact(BaseModel):
    """A package a setup job needs (the prober .deb), downloaded from the
    appliance and checked against `sha256`."""

    name: str
    filename: str
    version: str
    sha256: str


class LabAgentApiError(Exception):
    """The backend refused a call (`status_code` = its HTTP status) or could not
    be reached at all (`status_code` = 0: connect/read timeout, TLS, DNS ...).

    Transport errors are folded in here on purpose: every agent handler already
    treats `LabAgentApiError` as "the backend call failed, retry later", and a
    raw `httpx.HTTPError` (not an `OSError`) escaping one would kill the main
    loop or a running build thread on a network blip or an appliance update."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        if status_code == 0:
            message = f"RESURGAM backend unreachable: {body[:200]}"
        else:
            message = f"RESURGAM backend returned HTTP {status_code}: {body[:200]}"
        super().__init__(message)


class _GuardedHttp:
    """The few `httpx.Client` methods the agent uses, with every transport
    error re-raised as `LabAgentApiError(0, ...)`."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.get(url, **kwargs)
        except httpx.HTTPError as exc:
            raise _transport_error(exc) from exc

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.post(url, **kwargs)
        except httpx.HTTPError as exc:
            raise _transport_error(exc) from exc

    @contextmanager
    def stream(self, method: str, url: str, **kwargs: Any) -> Iterator[httpx.Response]:
        # Covers the request *and* the body reads done inside the `with`.
        try:
            with self._client.stream(method, url, **kwargs) as response:
                yield response
        except httpx.HTTPError as exc:
            raise _transport_error(exc) from exc

    def close(self) -> None:
        self._client.close()


def _transport_error(exc: httpx.HTTPError) -> LabAgentApiError:
    return LabAgentApiError(0, f"{type(exc).__name__}: {exc}")


class LabAgentClient:
    """Thin wrapper around the `/lab-agent/*` HTTPS backchannel
    (`backend/src/resurgam_api/api/v1/lab_agent.py`) -- outbound-only,
    bearer-token authenticated, no database access (ADR 0019).
    """

    def __init__(
        self, backend_url: str, agent_token: str, *, timeout: float = 30.0, ca_file: str | None = None
    ) -> None:
        self._client = _GuardedHttp(
            httpx.Client(
                base_url=backend_url,
                headers={"Authorization": f"Bearer {agent_token}"},
                timeout=timeout,
                # The appliance's own CA (Caddy `tls internal`), written at pairing.
                verify=ca_file or True,
            )
        )

    def list_jobs(self) -> list[LabAgentJob]:
        response = self._client.get("/lab-agent/jobs")
        if response.status_code != 200:
            raise LabAgentApiError(response.status_code, response.text)
        return [LabAgentJob.model_validate(job) for job in response.json()]

    def claim_job(self, member_id: str) -> None:
        response = self._client.post(f"/lab-agent/jobs/{member_id}/claim")
        if response.status_code != 204:
            raise LabAgentApiError(response.status_code, response.text)

    def report_status(self, member_id: str, status: Literal["booted", "failed"], *, error: str | None = None) -> None:
        body: dict = {"status": status}
        if error is not None:
            body["error"] = error
        response = self._client.post(f"/lab-agent/jobs/{member_id}/status", json=body)
        if response.status_code != 204:
            raise LabAgentApiError(response.status_code, response.text)

    def list_appliance_jobs(self) -> list[ApplianceSnippetJob]:
        response = self._client.get("/lab-agent/appliance-jobs")
        if response.status_code != 200:
            raise LabAgentApiError(response.status_code, response.text)
        return [ApplianceSnippetJob.model_validate(job) for job in response.json()]

    def confirm_snippet_written(self, plan_id: str) -> None:
        response = self._client.post(f"/lab-agent/appliance-jobs/{plan_id}/write-snippet")
        if response.status_code != 204:
            raise LabAgentApiError(response.status_code, response.text)

    def list_file_index_jobs(self) -> list[FileIndexJob]:
        response = self._client.get("/lab-agent/file-index-jobs")
        if response.status_code != 200:
            raise LabAgentApiError(response.status_code, response.text)
        return [FileIndexJob.model_validate(job) for job in response.json()]

    def claim_file_index_job(self, job_id: str) -> FileIndexJobClaim:
        response = self._client.post(f"/lab-agent/file-index-jobs/{job_id}/claim")
        if response.status_code != 200:
            raise LabAgentApiError(response.status_code, response.text)
        return FileIndexJobClaim.model_validate(response.json())

    def submit_file_index_entries(self, job_id: str, entries: list[FileIndexEntry]) -> None:
        if not entries:
            return
        body = {"entries": [e.model_dump(mode="json") for e in entries]}
        response = self._client.post(f"/lab-agent/file-index-jobs/{job_id}/entries", json=body)
        if response.status_code != 204:
            raise LabAgentApiError(response.status_code, response.text)

    def report_file_index_status(
        self, job_id: str, status: Literal["completed", "failed"], *, error: str | None = None
    ) -> None:
        body: dict = {"status": status}
        if error is not None:
            body["error"] = error
        response = self._client.post(f"/lab-agent/file-index-jobs/{job_id}/status", json=body)
        if response.status_code != 204:
            raise LabAgentApiError(response.status_code, response.text)

    def report_backup_completed(self, vmid: int, node: str, at: str) -> None:
        response = self._client.post("/lab-agent/backup-completed", json={"vmid": vmid, "node": node, "at": at})
        if response.status_code != 204:
            raise LabAgentApiError(response.status_code, response.text)

    # --- Disk Mounts (ADR 0025) ---------------------------------------------

    def list_disk_mount_jobs(self) -> list[DiskMountJob]:
        response = self._client.get("/lab-agent/disk-mount-jobs")
        if response.status_code != 200:
            raise LabAgentApiError(response.status_code, response.text)
        return [DiskMountJob.model_validate(job) for job in response.json()]

    def claim_disk_mount_job(self, mount_id: str) -> DiskMountClaim:
        response = self._client.post(f"/lab-agent/disk-mount-jobs/{mount_id}/claim")
        if response.status_code != 200:
            raise LabAgentApiError(response.status_code, response.text)
        return DiskMountClaim.model_validate(response.json())

    def report_disk_mount_progress(
        self,
        mount_id: str,
        *,
        helper_vmid: int | None = None,
        mapped_device: dict | None = None,
        snippet_volume: str | None = None,
    ) -> None:
        body: dict = {}
        if helper_vmid is not None:
            body["helper_vmid"] = helper_vmid
        if mapped_device is not None:
            body["mapped_device"] = mapped_device
        if snippet_volume is not None:
            body["snippet_volume"] = snippet_volume
        response = self._client.post(f"/lab-agent/disk-mount-jobs/{mount_id}/progress", json=body)
        if response.status_code != 204:
            raise LabAgentApiError(response.status_code, response.text)

    def report_disk_mount_status(
        self,
        mount_id: str,
        status: Literal["ready", "failed"],
        *,
        error: str | None = None,
        cleaned_up: bool = False,
        addresses: list[str] | None = None,
        volumes: list[dict] | None = None,
    ) -> None:
        body: dict = {"status": status, "cleaned_up": cleaned_up}
        if error is not None:
            body["error"] = error[:4000]
        if addresses is not None:
            body["addresses"] = addresses
        if volumes is not None:
            body["volumes"] = volumes
        response = self._client.post(f"/lab-agent/disk-mount-jobs/{mount_id}/status", json=body)
        if response.status_code != 204:
            raise LabAgentApiError(response.status_code, response.text)

    def report_disk_mount_teardown(
        self, mount_id: str, status: Literal["done", "failed"], *, error: str | None = None
    ) -> None:
        body: dict = {"status": status}
        if error is not None:
            body["error"] = error[:4000]
        response = self._client.post(f"/lab-agent/disk-mount-jobs/{mount_id}/teardown-status", json=body)
        if response.status_code != 204:
            raise LabAgentApiError(response.status_code, response.text)

    # --- Instant Recovery (ADR 0030) -------------------------------------------

    def list_instant_recovery_jobs(self) -> list[InstantRecoveryJob]:
        response = self._client.get("/lab-agent/instant-recovery-jobs")
        if response.status_code != 200:
            raise LabAgentApiError(response.status_code, response.text)
        return [InstantRecoveryJob.model_validate(job) for job in response.json()]

    def claim_instant_recovery_job(self, plan_id: str) -> InstantRecoveryClaim:
        response = self._client.post(f"/lab-agent/instant-recovery-jobs/{plan_id}/claim")
        if response.status_code != 200:
            raise LabAgentApiError(response.status_code, response.text)
        return InstantRecoveryClaim.model_validate(response.json())

    def report_instant_recovery_progress(self, plan_id: str, **fields: Any) -> None:
        """One of `mapped_device`, `overlay_volume`, `imported_volume` (dicts),
        `vm_created` (bool) or `phase` ("starting") -- whatever just came into
        existence, so a discard after any crash knows what to remove."""
        body = {key: value for key, value in fields.items() if value is not None}
        response = self._client.post(f"/lab-agent/instant-recovery-jobs/{plan_id}/progress", json=body)
        if response.status_code != 204:
            raise LabAgentApiError(response.status_code, response.text)

    def report_instant_recovery_status(
        self,
        plan_id: str,
        status: Literal["running", "failed", "lost", "finished", "finish_failed", "discarded", "discard_failed"],
        *,
        error: str | None = None,
        cleaned_up: bool = False,
        skipped_disks: list[str] | None = None,
    ) -> None:
        body: dict = {"status": status, "cleaned_up": cleaned_up}
        if error is not None:
            body["error"] = error[:4000]
        if skipped_disks:
            body["skipped_disks"] = skipped_disks
        response = self._client.post(f"/lab-agent/instant-recovery-jobs/{plan_id}/status", json=body)
        if response.status_code != 204:
            raise LabAgentApiError(response.status_code, response.text)

    def report_instant_recovery_heartbeat(
        self, plan_id: str, *, overlay_bytes: int, storage_total_bytes: int, storage_free_bytes: int
    ) -> None:
        body = {
            "overlay_bytes": overlay_bytes,
            "storage_total_bytes": storage_total_bytes,
            "storage_free_bytes": storage_free_bytes,
        }
        response = self._client.post(f"/lab-agent/instant-recovery-jobs/{plan_id}/heartbeat", json=body)
        if response.status_code != 204:
            raise LabAgentApiError(response.status_code, response.text)

    # --- setup jobs (ADR 0027) ---------------------------------------------

    def list_setup_jobs(self) -> list[SetupJob]:
        response = self._client.get("/lab-agent/setup-jobs")
        if response.status_code != 200:
            raise LabAgentApiError(response.status_code, response.text)
        return [SetupJob.model_validate(job) for job in response.json()]

    def claim_setup_job(self, job_id: str) -> None:
        response = self._client.post(f"/lab-agent/setup-jobs/{job_id}/claim")
        if response.status_code != 204:
            raise LabAgentApiError(response.status_code, response.text)

    def report_setup_progress(self, job_id: str, steps: list[dict], result: dict) -> None:
        response = self._client.post(
            f"/lab-agent/setup-jobs/{job_id}/progress", json={"steps": steps, "result": result}
        )
        if response.status_code != 204:
            raise LabAgentApiError(response.status_code, response.text)

    def report_setup_status(
        self,
        job_id: str,
        status: Literal["succeeded", "failed"],
        *,
        steps: list[dict],
        result: dict,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        body = {
            "status": status,
            "steps": steps,
            "result": result,
            "error_code": error_code,
            "error_message": error_message,
        }
        response = self._client.post(f"/lab-agent/setup-jobs/{job_id}/status", json=body)
        if response.status_code != 204:
            raise LabAgentApiError(response.status_code, response.text)

    def setup_artifact(self, name: str) -> SetupArtifact:
        response = self._client.get(f"/lab-agent/setup-artifacts/{name}")
        if response.status_code != 200:
            raise LabAgentApiError(response.status_code, response.text)
        return SetupArtifact.model_validate(response.json())

    def download_setup_artifact(self, name: str, destination: str) -> None:
        with self._client.stream("GET", f"/lab-agent/setup-artifacts/{name}/file") as response:
            if response.status_code != 200:
                raise LabAgentApiError(response.status_code, response.read().decode(errors="replace"))
            with open(destination, "wb") as handle:
                handle.writelines(response.iter_bytes(1024 * 1024))

    def close(self) -> None:
        self._client.close()
