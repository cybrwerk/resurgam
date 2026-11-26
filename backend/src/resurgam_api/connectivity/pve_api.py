from urllib.parse import quote

from resurgam_api.connectivity._http import request_json
from resurgam_api.connectivity.models import (
    GuestListResult,
    JobIncludedGuestsResult,
    NodeListResult,
    PermissionsResult,
    PoolListResult,
    PoolMembersResult,
    ProbeResult,
    PveTaskStatus,
    RemoteGuest,
    RemoteNode,
    RemotePool,
    RemoteStorage,
    StorageListResult,
    TrustConfig,
    UnprotectedGuest,
    UnprotectedGuestListResult,
    VmidAvailabilityResult,
    VzdumpJob,
    VzdumpJobListResult,
)

VERSION_PATH = "/api2/json/version"
NODES_PATH = "/api2/json/nodes"
STORAGE_CONFIG_PATH = "/api2/json/storage"
CLUSTER_RESOURCES_PATH = "/api2/json/cluster/resources"
CLUSTER_NEXTID_PATH = "/api2/json/cluster/nextid"
CLUSTER_BACKUP_PATH = "/api2/json/cluster/backup"
NOT_BACKED_UP_PATH = "/api2/json/cluster/backup-info/not-backed-up"
ACCESS_PERMISSIONS_PATH = "/api2/json/access/permissions"


def _cluster_backup_job_path(job_id: str) -> str:
    return f"{CLUSTER_BACKUP_PATH}/{job_id}"


def _node_storage_status_path(node: str) -> str:
    return f"/api2/json/nodes/{node}/storage"


def _get_data(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    path: str,
    timeout: float,
    params: dict | None = None,
) -> tuple[list | dict | str | None, str | None]:
    """GET a PVE API path and return its `data` field, or an error message.

    Shared by every PVE REST call in this module — the auth header uses
    `PVEAPIToken=` with a `user@realm!tokenid=secret` token (note the `=`
    separator, not PBS's `:` — confirmed live,
    docs/milestone-1-pbs-pve-api-assumptions.md).
    """
    return request_json("GET", host, port, trust, token, path, timeout, auth_scheme="PVEAPIToken", params=params)


def _post_data(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    path: str,
    timeout: float,
    data: dict | None = None,
) -> tuple[list | dict | str | None, str | None]:
    """POST a PVE API path and return its `data` field, or an error
    message -- used for the EPIC 9 (manifest §11.2) restore-execution
    calls, the first PVE writes this codebase makes (everything before
    this was read-only).
    """
    return request_json(
        "POST", host, port, trust, token, path, timeout, auth_scheme="PVEAPIToken", data=data, ok_statuses=(200, 201)
    )


def _put_data(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    path: str,
    timeout: float,
    data: dict | None = None,
) -> tuple[list | dict | str | None, str | None]:
    """PUT a PVE API path and return its `data` field, or an error message
    -- for config-update calls (PVE uses PUT for `.../config` endpoints,
    POST for action/creation endpoints).
    """
    return request_json(
        "PUT", host, port, trust, token, path, timeout, auth_scheme="PVEAPIToken", data=data, ok_statuses=(200, 201)
    )


def _delete_data(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    path: str,
    timeout: float,
    params: dict | None = None,
) -> tuple[list | dict | str | None, str | None]:
    """DELETE a PVE API path and return its `data` field, or an error
    message. `params` is a query string, not a body -- PVE's own DELETE
    endpoints take options that way (e.g. `destroy_vm`'s `purge`/
    `destroy-unreferenced-disks` flags).
    """
    return request_json(
        "DELETE",
        host,
        port,
        trust,
        token,
        path,
        timeout,
        auth_scheme="PVEAPIToken",
        params=params,
        ok_statuses=(200, 201),
    )


def probe_pve(host: str, port: int, trust: TrustConfig, token: str, timeout: float = 5.0) -> ProbeResult:
    """Check reachability and read the version of a PVE cluster.

    `host` must be a bare hostname/IP (no scheme). Confirmed live against a
    real PVE 9.2 cluster — see docs/milestone-1-pbs-pve-api-assumptions.md.
    """
    data, error = _get_data(host, port, trust, token, VERSION_PATH, timeout)
    if error is not None:
        return ProbeResult(reachable=False, error=error)
    version = data.get("version") if isinstance(data, dict) else None
    return ProbeResult(reachable=True, version=version)


def list_nodes(host: str, port: int, trust: TrustConfig, token: str, timeout: float = 5.0) -> NodeListResult:
    """List cluster nodes via `GET /nodes` — field names confirmed live
    against a real PVE 9.2 cluster (2026-09-16).
    """
    data, error = _get_data(host, port, trust, token, NODES_PATH, timeout)
    if error is not None:
        return NodeListResult(reachable=False, error=error)

    try:
        nodes = [
            RemoteNode(
                node=entry["node"],
                status=entry.get("status"),
                cpu=entry.get("cpu"),
                maxmem=entry.get("maxmem"),
                uptime=entry.get("uptime"),
            )
            for entry in data
        ]
    except (TypeError, KeyError) as exc:
        return NodeListResult(reachable=False, error=f"unexpected response shape: {exc}")

    return NodeListResult(reachable=True, nodes=nodes)


def list_storages(
    host: str, port: int, trust: TrustConfig, token: str, node: str, timeout: float = 5.0
) -> StorageListResult:
    """List storages usable on one node, merged from two real endpoints
    (confirmed live, 2026-09-16 — see docs/milestone-1-pbs-pve-api-assumptions.md):

    - `GET /storage` (cluster-wide storage.cfg): the only place a
      `type: "pbs"` entry's `server`/`datastore`/`namespace` fields appear.
      A storage's `nodes` field (comma-separated) restricts which nodes it
      applies to — not filtered here explicitly, since...
    - `GET /nodes/{node}/storage` (per-node live status): `avail`/`used`/
      `total`/`active` — and, confirmed live, already scoped to storages
      actually available on that node, so it's used as the primary list
      and enriched with the config fields above by matching `storage` id.
    """
    config_data, error = _get_data(host, port, trust, token, STORAGE_CONFIG_PATH, timeout)
    if error is not None:
        return StorageListResult(reachable=False, error=error)

    try:
        config_by_id = {entry["storage"]: entry for entry in config_data}
    except (TypeError, KeyError) as exc:
        return StorageListResult(reachable=False, error=f"unexpected response shape: {exc}")

    status_data, error = _get_data(host, port, trust, token, _node_storage_status_path(node), timeout)
    if error is not None:
        return StorageListResult(reachable=False, error=error)

    try:
        storages = []
        for entry in status_data:
            storage_id = entry["storage"]
            config = config_by_id.get(storage_id, {})
            storage_type = entry.get("type") or config.get("type", "")
            content = entry.get("content") or config.get("content") or ""
            is_pbs = storage_type == "pbs"
            storages.append(
                RemoteStorage(
                    storage=storage_id,
                    type=storage_type,
                    content=[c for c in content.split(",") if c],
                    active=bool(entry["active"]) if "active" in entry else None,
                    shared=bool(config["shared"]) if "shared" in config else None,
                    avail_bytes=entry.get("avail"),
                    used_bytes=entry.get("used"),
                    total_bytes=entry.get("total"),
                    pbs_server=config.get("server") if is_pbs else None,
                    pbs_datastore=config.get("datastore") if is_pbs else None,
                    pbs_namespace=config.get("namespace") if is_pbs else None,
                )
            )
    except (TypeError, KeyError) as exc:
        return StorageListResult(reachable=False, error=f"unexpected response shape: {exc}")

    return StorageListResult(reachable=True, storages=storages)


def list_guests(host: str, port: int, trust: TrustConfig, token: str, timeout: float = 5.0) -> GuestListResult:
    """List every VM/CT cluster-wide via `GET /cluster/resources?type=vm`
    — cluster-wide, not per-node, since VMID uniqueness is cluster-wide in
    PVE (ADR 0009's caution about not assuming per-node/per-instance
    uniqueness). Field names confirmed live (2026-09-16).
    """
    data, error = _get_data(host, port, trust, token, CLUSTER_RESOURCES_PATH, timeout, params={"type": "vm"})
    if error is not None:
        return GuestListResult(reachable=False, error=error)

    try:
        guests = [
            RemoteGuest(
                vmid=entry["vmid"],
                node=entry["node"],
                guest_type=entry["type"],
                name=entry.get("name"),
                status=entry.get("status"),
            )
            for entry in data
        ]
    except (TypeError, KeyError) as exc:
        return GuestListResult(reachable=False, error=f"unexpected response shape: {exc}")

    return GuestListResult(reachable=True, guests=guests)


def _decode_prune_backups(raw: object) -> dict[str, int]:
    """`prune-backups` comes back from `GET /cluster/backup`/`GET
    /cluster/backup/{id}` as a nested JSON object with string-typed
    values (confirmed live 2026-09-27: `{"keep-daily": "7", "keep-weekly":
    "4"}`) -- NOT the flat property-string PVE's docs describe for the
    write side. Absent or malformed entries are dropped rather than
    raising, matching this module's general "best-effort parse" stance
    for optional PVE fields.
    """
    if not isinstance(raw, dict):
        return {}
    decoded: dict[str, int] = {}
    for key, value in raw.items():
        try:
            decoded[key] = int(value)
        except (TypeError, ValueError):
            continue
    return decoded


def _vzdump_job_from_entry(entry: dict) -> VzdumpJob:
    """Shared by `list_vzdump_jobs`/`get_vzdump_job` — both read the
    identical wire shape, just from `GET /cluster/backup` (list, every
    job) vs. `GET /cluster/backup/{id}` (one job) respectively. `enabled`
    is a real `0`/`1` int on the wire (confirmed live, 2026-09-19), cast
    to bool here. See `VzdumpJob`'s own docstring for this field set's
    reasoning, including `prune_keep_*`'s 2026-09-27 live-verified shape.
    """
    prune = _decode_prune_backups(entry.get("prune-backups"))
    return VzdumpJob(
        id=entry["id"],
        enabled=bool(entry.get("enabled", 1)),
        schedule=entry.get("schedule"),
        storage=entry.get("storage"),
        node=entry.get("node"),
        comment=entry.get("comment"),
        all_guests=entry.get("all") == 1,
        vmid=entry.get("vmid"),
        pool=entry.get("pool"),
        exclude=entry.get("exclude"),
        mode=entry.get("mode"),
        compress=entry.get("compress"),
        notes_template=entry.get("notes-template"),
        mailnotification=entry.get("mailnotification"),
        mailto=entry.get("mailto"),
        prune_keep_last=prune.get("keep-last"),
        prune_keep_hourly=prune.get("keep-hourly"),
        prune_keep_daily=prune.get("keep-daily"),
        prune_keep_weekly=prune.get("keep-weekly"),
        prune_keep_monthly=prune.get("keep-monthly"),
        prune_keep_yearly=prune.get("keep-yearly"),
    )


def list_vzdump_jobs(host: str, port: int, trust: TrustConfig, token: str, timeout: float = 5.0) -> VzdumpJobListResult:
    """List every configured vzdump backup job via `GET /cluster/backup`."""
    data, error = _get_data(host, port, trust, token, CLUSTER_BACKUP_PATH, timeout)
    if error is not None:
        return VzdumpJobListResult(reachable=False, error=error)

    try:
        jobs = [_vzdump_job_from_entry(entry) for entry in data]
    except (TypeError, KeyError) as exc:
        return VzdumpJobListResult(reachable=False, error=f"unexpected response shape: {exc}")

    return VzdumpJobListResult(reachable=True, jobs=jobs)


def list_not_backed_up_guests(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float = 5.0
) -> UnprotectedGuestListResult:
    """`GET /cluster/backup-info/not-backed-up` -- every guest no vzdump job
    covers (ADR 0032). PVE counts *disabled* jobs as coverage and silently
    leaves out guests the token lacks `VM.Audit` on (pve-manager
    `PVE/API2/Cluster/BackupInfo.pm`), so this is not the whole picture on
    its own -- see `list_job_included_vmids`.
    """
    data, error = _get_data(host, port, trust, token, NOT_BACKED_UP_PATH, timeout)
    if error is not None:
        return UnprotectedGuestListResult(reachable=False, error=error)
    try:
        guests = [
            UnprotectedGuest(vmid=int(entry["vmid"]), guest_type=entry["type"], name=entry.get("name"))
            for entry in data
        ]
    except (TypeError, KeyError, ValueError) as exc:
        return UnprotectedGuestListResult(reachable=False, error=f"unexpected response shape: {exc}")
    return UnprotectedGuestListResult(reachable=True, guests=guests)


def list_job_included_vmids(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, job_id: str
) -> JobIncludedGuestsResult:
    """`GET /cluster/backup/{id}/included_volumes` -- the guests a vzdump job
    covers (PVE resolves `all`, pools and `exclude` itself). The response is a
    tree whose `children` are the guests (`id` = VMID, api-viewer schema
    2026-11-26); their volumes are not needed here.
    """
    data, error = _get_data(
        host, port, trust, token, f"{_cluster_backup_job_path(quote(job_id, safe=''))}/included_volumes", timeout
    )
    if error is not None:
        return JobIncludedGuestsResult(reachable=False, error=error)
    try:
        vmids = frozenset(int(child["id"]) for child in (data or {}).get("children") or [])
    except (TypeError, KeyError, ValueError, AttributeError) as exc:
        return JobIncludedGuestsResult(reachable=False, error=f"unexpected response shape: {exc}")
    return JobIncludedGuestsResult(reachable=True, vmids=vmids)


def get_vzdump_job(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, job_id: str
) -> VzdumpJob | None:
    """`GET /cluster/backup/{id}` — one job's full current definition,
    for pre-filling an edit form (docs/adr/0023-backup-and-maintenance-job-administration:
    "import" means the existing list becomes clickable into a real edit
    form, not copying anything into a RESURGAM-owned table). Returns
    `None` on any error — callers already have `list_vzdump_jobs` for
    reachability/error detail if needed; this is the "open one job"
    single-purpose call.
    """
    data, error = _get_data(host, port, trust, token, _cluster_backup_job_path(job_id), timeout)
    if error is not None or not isinstance(data, dict):
        return None
    try:
        return _vzdump_job_from_entry(data)
    except (TypeError, KeyError):
        return None


def _encode_prune_backups(job: VzdumpJob) -> str | None:
    """Builds PVE's real write-side `prune-backups` value: a flat
    property string (`keep-daily=7,keep-weekly=4`), confirmed live
    2026-09-27 -- distinct from the nested-object shape `GET` returns
    (see `_decode_prune_backups`). `None` when no `prune_keep_*` field is
    set, so the key is omitted from the payload entirely rather than
    sent as an empty string.
    """
    parts = [
        f"{wire}={value}"
        for wire, value in (
            ("keep-last", job.prune_keep_last),
            ("keep-hourly", job.prune_keep_hourly),
            ("keep-daily", job.prune_keep_daily),
            ("keep-weekly", job.prune_keep_weekly),
            ("keep-monthly", job.prune_keep_monthly),
            ("keep-yearly", job.prune_keep_yearly),
        )
        if value is not None
    ]
    return ",".join(parts) if parts else None


def _vzdump_job_payload(job: VzdumpJob) -> dict:
    """Maps `VzdumpJob`'s Python field names back to PVE's real
    hyphenated wire names (`notes_template` -> `notes-template`), and
    drops `None` fields entirely — both `create_vzdump_job` (only fields
    actually filled in) and `update_vzdump_job` (a real partial update,
    confirmed live 2026-09-19 that PVE's `.../config`-style PUT merges
    rather than replaces) rely on absent-means-unset here, not an
    explicit null. Includes `id` when set — see `create_vzdump_job`'s
    docstring for why the caller must always set it.
    """
    raw = {
        "id": job.id or None,
        "schedule": job.schedule,
        "storage": job.storage,
        "node": job.node,
        "comment": job.comment,
        "all": 1 if job.all_guests else None,
        "vmid": job.vmid,
        "pool": job.pool,
        "exclude": job.exclude,
        "mode": job.mode,
        "compress": job.compress,
        "notes-template": job.notes_template,
        "mailnotification": job.mailnotification,
        "mailto": job.mailto,
        "enabled": 1 if job.enabled else 0,
        "prune-backups": _encode_prune_backups(job),
    }
    return {key: value for key, value in raw.items() if value is not None}


def create_vzdump_job(host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, job: VzdumpJob) -> None:
    """`POST /cluster/backup` — create a new vzdump job. `job.id` is
    **required** and always sent explicitly — confirmed live (2026-09-26)
    that `POST /cluster/backup`'s response body is empty even on success,
    and real PVE-autogenerated job IDs are random UUIDs with no
    creation-time ordering (`backup-<hex>-<hex>`) -- there is no reliable
    way to find "the job that was just created" from a subsequent list
    without knowing its ID in advance. Also confirmed live that PVE
    accepts and uses a caller-supplied `--id` as-is rather than rejecting
    it or generating its own. The caller (`api/v1/pve_clusters.py`)
    generates this ID itself before calling here.
    """
    if not job.id:
        raise ValueError("create_vzdump_job requires job.id to be set")
    payload = _vzdump_job_payload(job)
    _, error = _post_data(host, port, trust, token, CLUSTER_BACKUP_PATH, timeout, data=payload)
    if error is not None:
        raise PveWriteError(error)


def update_vzdump_job(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, job_id: str, changes: dict
) -> None:
    """`PUT /cluster/backup/{id}` with only `changes` — the caller builds
    this dict itself (already using PVE's real wire field names, e.g.
    `{"schedule": "03:00"}`), rather than requiring a full `VzdumpJob`
    reconstruction, since a real edit form only ever sends the fields an
    operator actually touched. Confirmed live (2026-09-19, ADR 0018) that
    this endpoint merges rather than replaces.
    """
    _, error = _put_data(host, port, trust, token, _cluster_backup_job_path(job_id), timeout, data=changes)
    if error is not None:
        raise PveWriteError(error)


def set_vzdump_job_enabled(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, job_id: str, enabled: bool
) -> None:
    """`PUT /cluster/backup/{id}` with only `enabled` set — a thin
    convenience wrapper around `update_vzdump_job`, kept as its own
    function since the existing enable/disable UI (ADR 0018) already
    calls it by this name.
    """
    update_vzdump_job(host, port, trust, token, timeout, job_id=job_id, changes={"enabled": 1 if enabled else 0})


def delete_vzdump_job(host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, job_id: str) -> None:
    """`DELETE /cluster/backup/{id}`."""
    _, error = _delete_data(host, port, trust, token, _cluster_backup_job_path(job_id), timeout)
    if error is not None:
        raise PveWriteError(error)


def get_own_permissions(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float = 5.0
) -> PermissionsResult:
    """`GET /access/permissions` — the calling token's own real,
    effective privileges, `{path: {privilege: True, ...}, ...}`.
    Job Administration's capability gating (docs/adr/0023-backup-and-maintenance-job-administration) is built
    entirely on this: whether RESURGAM offers create/edit/delete for a
    given job category is decided from what's actually granted here, not
    assumed from PVE's schema docs (which, confirmed live by ADR 0018,
    already proved incomplete once for this exact job type).
    """
    data, error = _get_data(host, port, trust, token, ACCESS_PERMISSIONS_PATH, timeout)
    if error is not None:
        return PermissionsResult(reachable=False, error=error)
    if not isinstance(data, dict):
        return PermissionsResult(reachable=False, error=f"unexpected response shape: {data!r}")
    return PermissionsResult(reachable=True, privileges=data)


def check_vmid_free(
    host: str, port: int, trust: TrustConfig, token: str, vmid: int, timeout: float = 5.0
) -> VmidAvailabilityResult:
    """Check whether one VMID is free via `GET /cluster/nextid?vmid=N`.

    Confirmed live (2026-09-16): a taken VMID returns HTTP 400 with
    `{"errors": {"vmid": "VM <n> already exists"}}` — a real, expected
    outcome (`free=False`), distinguished here from a genuine connectivity
    failure rather than surfaced as an error.
    """
    data, error = _get_data(host, port, trust, token, CLUSTER_NEXTID_PATH, timeout, params={"vmid": vmid})
    if error is not None:
        if error.startswith("HTTP 400") and "already exists" in error:
            return VmidAvailabilityResult(reachable=True, vmid=vmid, free=False)
        return VmidAvailabilityResult(reachable=False, error=error)

    return VmidAvailabilityResult(reachable=True, vmid=int(data) if data is not None else vmid, free=True)


def next_free_vmid(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float = 5.0
) -> VmidAvailabilityResult:
    """Get PVE's own suggested next-free VMID via `GET /cluster/nextid`
    (no `vmid` param). Confirmed live: returns the id as a *string*
    (`{"data": "109"}`), cast to int here.
    """
    data, error = _get_data(host, port, trust, token, CLUSTER_NEXTID_PATH, timeout)
    if error is not None:
        return VmidAvailabilityResult(reachable=False, error=error)

    try:
        vmid = int(data) if data is not None else None
    except (TypeError, ValueError) as exc:
        return VmidAvailabilityResult(reachable=False, error=f"unexpected response shape: {exc}")

    return VmidAvailabilityResult(reachable=True, vmid=vmid, free=True)


# EPIC 9 (manifest §11.2): automated restore orchestration. Everything
# below is a PVE *write* -- the first this codebase makes; everything
# above is read-only. Exact request shapes are the standard, documented
# PVE REST API (never previously exercised in this project) and were
# confirmed/corrected against a real PVE 9.2 test cluster -- see
# docs/milestone-1-pbs-pve-api-assumptions.md for what was actually
# verified live vs. assumed from documentation.


def _qemu_restore_path(node: str) -> str:
    return f"/api2/json/nodes/{node}/qemu"


def _lxc_restore_path(node: str) -> str:
    return f"/api2/json/nodes/{node}/lxc"


def _qemu_config_path(node: str, vmid: int) -> str:
    return f"/api2/json/nodes/{node}/qemu/{vmid}/config"


def _lxc_config_path(node: str, vmid: int) -> str:
    return f"/api2/json/nodes/{node}/lxc/{vmid}/config"


def _qemu_start_path(node: str, vmid: int) -> str:
    return f"/api2/json/nodes/{node}/qemu/{vmid}/status/start"


def _qemu_stop_path(node: str, vmid: int) -> str:
    return f"/api2/json/nodes/{node}/qemu/{vmid}/status/stop"


def _qemu_status_current_path(node: str, vmid: int) -> str:
    return f"/api2/json/nodes/{node}/qemu/{vmid}/status/current"


def _qemu_delete_path(node: str, vmid: int) -> str:
    return f"/api2/json/nodes/{node}/qemu/{vmid}"


def _lxc_start_path(node: str, vmid: int) -> str:
    return f"/api2/json/nodes/{node}/lxc/{vmid}/status/start"


def _quote_upid(upid: str) -> str:
    """A UPID's last fields (task id, user) may hold characters such as `?`,
    `#` or `/` -- percent-encode it for the URL path; `:`/`@`/`!` stay
    readable (they are legal in a path segment)."""
    return quote(upid, safe=":@!")


def _task_status_path(node: str, upid: str) -> str:
    return f"/api2/json/nodes/{node}/tasks/{_quote_upid(upid)}/status"


def _task_path(node: str, upid: str) -> str:
    return f"/api2/json/nodes/{node}/tasks/{_quote_upid(upid)}"


class PveWriteError(Exception):
    """Raised by every EPIC 9 write call below on failure -- distinct from
    the read-only functions' `reachable=False` pattern, since a failed
    write (unlike a failed read) needs to be treated as an error the
    caller must handle explicitly, not a value to branch on silently.
    """


def start_vm_restore(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    timeout: float,
    *,
    node: str,
    vmid: int,
    archive: str,
    storage: str,
    unique: bool,
    force: bool,
    name: str | None = None,
    live_restore: bool = False,
    net0: str | None = None,
) -> str:
    """`POST /nodes/{node}/qemu` with `archive=<volid>` -- PVE's real REST
    equivalent of `qmrestore`. Returns the UPID. `unique` regenerates
    MAC addresses (used for `vmid_intent="new"`); `force` allows
    overwriting an existing VMID (used for `vmid_intent="existing"`).
    `name` fixes a real gap confirmed live (2026-09-17): without it, the
    restored VM keeps whatever name was baked into the backup archive, not
    RESURGAM's own `temp_name`. Also confirmed live: applying it requires
    `VM.Config.Options` on the token (a second, separate privilege gap
    found only by actually running this -- `VM.Allocate`/`VM.Config.Disk`/
    `VM.Config.Network`/`VM.PowerMgmt` alone weren't enough).

    `live_restore` -- confirmed real on this exact endpoint (2026-09-19,
    apidoc schema: "Start the VM immediately while importing or restoring
    in the background") -- Recovery Lab (manifest EPIC 10) uses this
    instead of the heavier PBS-image/COW-overlay "Instant Lab Mode" the
    manifest also describes (§23.2/§23.3): PBS already serves reads
    on-demand while a background job fills in the rest, no separate agent
    needed.

    `net0` -- confirmed real and accepted on this same endpoint
    (2026-09-19, apidoc schema's `net[n]` property), **correcting** the
    assumption `restore/execution.py` had relied on since EPIC 8/9 ("no
    inline override exists" for a VM restore, hence its always-a-follow-up
    `set_vm_network_config` finalize step) -- that assumption was never
    actually tested for this specific case; `POST /nodes/{node}/qemu` is
    the general create-VM endpoint and accepts the same `net[n]` params
    whether or not `archive` is given. Found live while debugging a real
    `live-restore` failure: live-restore starts the VM as *part of* the
    restore call itself, so an invalid/absent network config (e.g. the
    archive's original NIC referencing a bridge that doesn't exist on the
    target node) fails the restore outright, not just a later finalize
    step -- confirmed via the real task log ("starting VM for
    live-restore" ... "An error occurred during live-restore: bridge
    'vmbr1' does not exist"). Recovery Lab passes `net0` inline here
    specifically so the guest's network is valid from the first moment,
    which `live_restore` requires; Guided Restore (EPIC 8/9) is
    unaffected -- this parameter is purely additive and `None` by default.
    """
    params: dict = {
        "vmid": vmid,
        "archive": archive,
        "storage": storage,
        "unique": 1 if unique else 0,
        "force": 1 if force else 0,
    }
    if name is not None:
        params["name"] = name
    if live_restore:
        params["live-restore"] = 1
    if net0 is not None:
        params["net0"] = net0
    data, error = _post_data(host, port, trust, token, _qemu_restore_path(node), timeout, data=params)
    if error is not None or not isinstance(data, str):
        raise PveWriteError(error or f"unexpected response for start_vm_restore: {data!r}")
    return data


def start_ct_restore(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    timeout: float,
    *,
    node: str,
    vmid: int,
    archive: str,
    storage: str,
    unique: bool,
    force: bool,
    net0: str | None = None,
    hostname: str | None = None,
) -> str:
    """`POST /nodes/{node}/lxc` with `restore=1`, `archive=<volid>` --
    PVE's real REST equivalent of `pct restore`. Returns the UPID. `net0`
    is sent inline whenever given -- whether that's the right call for a
    given network mode (vs. following up with `set_ct_network_config`
    instead) is `restore/execution.py`'s policy decision, not this
    function's; this is a thin request wrapper only. `hostname` is LXC's
    name-equivalent param (not `name`, which is QEMU/VM-only) -- without
    it the restored container keeps the archive's original hostname, the
    same real gap `name` fixes for VMs; not yet re-verified live.
    """
    params: dict = {
        "vmid": vmid,
        "ostemplate": archive,
        "restore": 1,
        "storage": storage,
        "unique": 1 if unique else 0,
        "force": 1 if force else 0,
    }
    if net0 is not None:
        params["net0"] = net0
    if hostname is not None:
        params["hostname"] = hostname

    data, error = _post_data(host, port, trust, token, _lxc_restore_path(node), timeout, data=params)
    if error is not None or not isinstance(data, str):
        raise PveWriteError(error or f"unexpected response for start_ct_restore: {data!r}")
    return data


def get_task_status(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, node: str, upid: str
) -> PveTaskStatus:
    """`GET /nodes/{node}/tasks/{upid}/status` -- poll one async PVE task.
    `status` is `"running"` or `"stopped"`; success vs. failure is only
    knowable from `exitstatus` once `status == "stopped"`.
    """
    data, error = _get_data(host, port, trust, token, _task_status_path(node, upid), timeout)
    if error is not None:
        return PveTaskStatus(reachable=False, error=error)
    if not isinstance(data, dict):
        return PveTaskStatus(reachable=False, error=f"unexpected response shape: {data!r}")
    return PveTaskStatus(reachable=True, status=data.get("status"), exitstatus=data.get("exitstatus"))


def stop_task(host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, node: str, upid: str) -> None:
    """`DELETE /nodes/{node}/tasks/{upid}` -- stops a running task
    (manifest-adjacent admin capability, raised directly by the user:
    aborting an in-flight restore-plan execution). Confirmed live
    (2026-09-17) against a real PVE 9.2 node: requires `Sys.Modify` on
    the node -- a genuinely broader privilege than every other call in
    this file (all VM/CT-scoped until now), granted explicitly for this.
    Also confirmed live: calling this on an already-finished task is safe
    -- it returns the same `{"data": null}` success shape rather than
    erroring, so callers don't need to pre-check the task's own status
    first (a real race is possible: the task can finish between the
    caller's last poll and this call).
    """
    _data, error = _delete_data(host, port, trust, token, _task_path(node, upid), timeout)
    if error is not None:
        raise PveWriteError(error)


def get_vm_config(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, node: str, vmid: int
) -> dict:
    """`GET /nodes/{node}/qemu/{vmid}/config` -- the restored VM's actual
    current config (e.g. `{"net0": "virtio=AA:BB:...,bridge=vmbr0", ...}`).
    Used to read back the archive's real `net0` value so the "none"
    network mode can *disconnect* it (`link_down=1`) while preserving the
    original NIC identity, rather than deleting the device outright --
    PVE's config API has no partial-attribute update, so changing one
    sub-value of `net0` means reading the whole string back first.
    Confirmed live end-to-end (2026-09-17): the restored NIC's real MAC,
    model, and SDN-managed bridge were all preserved exactly, with only
    `link_down=1` added.
    """
    data, error = _get_data(host, port, trust, token, _qemu_config_path(node, vmid), timeout)
    if error is not None or not isinstance(data, dict):
        raise PveWriteError(error or f"unexpected response for get_vm_config: {data!r}")
    return data


def set_vm_network_config(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    timeout: float,
    *,
    node: str,
    vmid: int,
    net0: str | None = None,
    delete: str | None = None,
) -> None:
    """`PUT /nodes/{node}/qemu/{vmid}/config` -- the mandatory follow-up
    call for any non-`ORIGINAL` network mode, since `qmrestore`/the
    restore POST above has no inline network override (confirmed live
    during EPIC 8, ADR 0012). Exactly one of `net0`/`delete` should be
    given, matching `restore/command.py`'s `qm set --net0 ...` /
    `qm set --delete net0` branching.
    """
    params: dict = {}
    if net0 is not None:
        params["net0"] = net0
    if delete is not None:
        params["delete"] = delete
    _, error = _put_data(host, port, trust, token, _qemu_config_path(node, vmid), timeout, data=params)
    if error is not None:
        raise PveWriteError(error)


def set_ct_network_config(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, node: str, vmid: int, net0: str
) -> None:
    """`PUT /nodes/{node}/lxc/{vmid}/config` -- the structural fallback
    for CT network configuration, used by `restore/execution.py` when its
    `CT_RESTORE_SUPPORTS_INLINE_NET0` policy flag is false. Not exercised
    in the initial live-verification pass (which targeted a VM), left as
    a deliberately unverified path until a CT restore is verified live too.
    """
    _, error = _put_data(host, port, trust, token, _lxc_config_path(node, vmid), timeout, data={"net0": net0})
    if error is not None:
        raise PveWriteError(error)


def get_ct_config(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, node: str, vmid: int
) -> dict:
    """`GET /nodes/{node}/lxc/{vmid}/config` -- a restored container's real
    config, read back so the post-restore network step can change every NIC
    (`net0`, `net1`, ...) while keeping its name, MAC and the rest."""
    data, error = _get_data(host, port, trust, token, _lxc_config_path(node, vmid), timeout)
    if error is not None or not isinstance(data, dict):
        raise PveWriteError(error or f"unexpected response for get_ct_config: {data!r}")
    return data


def set_guest_network_config(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    timeout: float,
    *,
    node: str,
    vmid: int,
    guest_type: str,
    nets: dict[str, str],
) -> None:
    """`PUT /nodes/{node}/{qemu|lxc}/{vmid}/config` with the new value of
    every NIC (`{"net0": "...", "net1": "..."}`) in one call -- the
    post-restore network step (`restore/execution.py` `network_updates`)."""
    if not nets:
        return
    path = _qemu_config_path(node, vmid) if guest_type == "qemu" else _lxc_config_path(node, vmid)
    _, error = _put_data(host, port, trust, token, path, timeout, data=dict(nets))
    if error is not None:
        raise PveWriteError(error)


def start_guest(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, node: str, vmid: int, guest_type: str
) -> str:
    """`POST /nodes/{node}/qemu/{vmid}/status/start` (VM) or
    `.../lxc/{vmid}/status/start` (CT) -- only ever called when the
    operator explicitly set `start_after_restore=True`; manifest §11.2
    keeps the guest powered off by default. Returns the UPID.
    """
    path = _qemu_start_path(node, vmid) if guest_type == "qemu" else _lxc_start_path(node, vmid)
    data, error = _post_data(host, port, trust, token, path, timeout)
    if error is not None or not isinstance(data, str):
        raise PveWriteError(error or f"unexpected response for start_guest: {data!r}")
    return data


# Recovery Lab (manifest EPIC 10, ADR 0019). Everything below is new SDN
# zone/vnet, pool, and VM-clone write/read support -- none of these
# endpoints were ever called by this codebase before, confirmed against
# the real PVE 9.2 apidoc schema (2026-09-19) but not yet exercised live.

SDN_ZONES_PATH = "/api2/json/cluster/sdn/zones"
SDN_VNETS_PATH = "/api2/json/cluster/sdn/vnets"
SDN_RELOAD_PATH = "/api2/json/cluster/sdn"
POOLS_PATH = "/api2/json/pools"


def _sdn_zone_path(name: str) -> str:
    return f"{SDN_ZONES_PATH}/{name}"


def _sdn_vnet_path(name: str) -> str:
    return f"{SDN_VNETS_PATH}/{name}"


def _pool_path(poolid: str) -> str:
    return f"{POOLS_PATH}/{poolid}"


def _qemu_clone_path(node: str, vmid: int) -> str:
    return f"/api2/json/nodes/{node}/qemu/{vmid}/clone"


def get_sdn_zone(host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, name: str) -> dict | None:
    """`GET /cluster/sdn/zones/{zone}` -- used to read a zone's real
    `type` (e.g. `simple` vs `vlan`/`vxlan`/`evpn`) so Recovery Lab can
    decide whether a run's Application Group members must share one PVE
    node (Simple zone: isolated per-node bridges, no cross-node path) or
    may spread across the cluster (a zone type that actually spans nodes).
    Returns `None` (not an error) if the zone doesn't exist yet -- the
    caller's job to create it, not this function's. Confirmed live
    (2026-09-19): PVE reports a missing zone as `HTTP 500` with body
    `{"message": "sdn zone '<name>' does not exist\n"}`, **not** a `404`
    as first assumed -- both are treated as "absent" here.
    """
    data, error = _get_data(host, port, trust, token, _sdn_zone_path(name), timeout)
    if error is not None:
        if error.startswith("HTTP 404") or "does not exist" in error:
            return None
        raise PveWriteError(error)
    return data if isinstance(data, dict) else None


def create_sdn_zone(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, name: str, zone_type: str = "simple"
) -> None:
    """`POST /cluster/sdn/zones` -- creates RESURGAM's own default Lab
    isolation zone when the cluster's admin hasn't configured one of
    their own (`PveCluster.lab_sdn_zone_name`). Requires `SDN.Allocate`
    on `/sdn/zones` (confirmed absent on the current token, 2026-09-19 --
    to be granted when this is actually wired up and tested live).
    """
    _, error = _post_data(host, port, trust, token, SDN_ZONES_PATH, timeout, data={"zone": name, "type": zone_type})
    if error is not None:
        raise PveWriteError(error)


def create_sdn_vnet(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, vnet_name: str, zone_name: str
) -> None:
    """`POST /cluster/sdn/vnets` -- one fresh, uniquely-named VNet per Lab
    run (manifest §22.5), under whichever zone is in use for the target
    cluster. Requires `SDN.Allocate` on `/sdn/zones/{zone}`.
    """
    _, error = _post_data(
        host, port, trust, token, SDN_VNETS_PATH, timeout, data={"vnet": vnet_name, "zone": zone_name}
    )
    if error is not None:
        raise PveWriteError(error)


def delete_sdn_vnet(host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, vnet_name: str) -> None:
    """`DELETE /cluster/sdn/vnets/{vnet}` -- part of a Lab run's cleanup.

    Treats "already gone" as success -- same PVE error shape as
    `get_sdn_zone`'s own missing-object case (`HTTP 500` with "does not
    exist" in the body, not a `404`). Raised directly by the user from a
    real cleanup call that 500'd on a vnet a previous attempt had already
    removed: `cleanup_restore_test_plan`'s own docstring says it must be
    safely callable repeatedly, so a vnet that's already gone is not a
    real failure here.
    """
    _, error = _delete_data(host, port, trust, token, _sdn_vnet_path(vnet_name), timeout)
    if error is not None:
        if error.startswith("HTTP 404") or "does not exist" in error:
            return
        raise PveWriteError(error)


def apply_sdn_changes(host: str, port: int, trust: TrustConfig, token: str, timeout: float) -> None:
    """`PUT /cluster/sdn` -- "Apply sdn controller changes && reload."
    PVE's SDN changes (zone/vnet create or delete) are staged and don't
    take effect on the wire until this is called -- confirmed real from
    the apidoc schema; the exact timing (call once after a batch of
    changes, vs. after every single change) needs live confirmation
    before being trusted, not assumed.
    """
    _, error = _put_data(host, port, trust, token, SDN_RELOAD_PATH, timeout)
    if error is not None:
        raise PveWriteError(error)


def list_pools(host: str, port: int, trust: TrustConfig, token: str, timeout: float = 5.0) -> PoolListResult:
    """`GET /pools` -- every resource pool on the cluster, confirmed live
    2026-09-27: `[{"poolid": ..., "comment": ...}]`. One of the two real
    ways to target a vzdump job at a set of guests (the other being an
    explicit `vmid` list) -- surfaced as a picker so an operator selects
    an existing pool rather than typing its id by hand.
    """
    data, error = _get_data(host, port, trust, token, POOLS_PATH, timeout)
    if error is not None:
        return PoolListResult(reachable=False, error=error)
    try:
        pools = [RemotePool(poolid=entry["poolid"], comment=entry.get("comment")) for entry in data]
    except (TypeError, KeyError) as exc:
        return PoolListResult(reachable=False, error=f"unexpected response shape: {exc}")
    return PoolListResult(reachable=True, pools=pools)


def create_pool(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, poolid: str, comment: str | None = None
) -> None:
    """`POST /pools` -- RESURGAM's own fixed-name Lab pool, created
    idempotently per lab-enabled cluster on first use. Requires
    `Pool.Allocate` on `/pool/{poolid}` (confirmed absent on the current
    token, 2026-09-19).
    """
    params: dict = {"poolid": poolid}
    if comment is not None:
        params["comment"] = comment
    _, error = _post_data(host, port, trust, token, POOLS_PATH, timeout, data=params)
    if error is not None:
        raise PveWriteError(error)


def get_pool_members(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, poolid: str
) -> PoolMembersResult:
    """`GET /pools/{poolid}` -- the single-pool detail endpoint (distinct
    from `list_pools`'s `GET /pools`, which never includes membership):
    `{"members": [{"vmid": 111, "type": "qemu", ...}, ...]}`, `vmid`
    absent on any non-guest member (e.g. a storage entry) and skipped.

    Recovery Lab cleanup's guardrail before destroying a VM (raised
    directly by the user): a stored `target_vmid`/`appliance_vmid` is
    only ever destroyed once confirmed here as an actual member of
    `resurgam-lab` (`restore_test/execution.py`'s `DEFAULT_POOL_NAME`),
    never trusted blindly.
    """
    data, error = _get_data(host, port, trust, token, _pool_path(poolid), timeout)
    if error is not None:
        return PoolMembersResult(reachable=False, error=error)
    if not isinstance(data, dict):
        return PoolMembersResult(reachable=False, error=f"unexpected response shape: {data!r}")
    members = data.get("members") or []
    vmids = frozenset(m["vmid"] for m in members if isinstance(m, dict) and "vmid" in m)
    return PoolMembersResult(reachable=True, member_vmids=vmids)


def clone_vm(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    timeout: float,
    *,
    node: str,
    source_vmid: int,
    target_vmid: int,
    name: str,
    target_node: str | None = None,
    target_storage: str | None = None,
    pool: str | None = None,
    full: bool = True,
) -> str:
    """`POST /nodes/{node}/qemu/{vmid}/clone` -- clones the Lab probe
    appliance template fresh for each run (manifest EPIC 10's "several
    labs on several clusters" concurrency needs each run to have its own
    appliance, not one shared/reconfigured VM). Returns the UPID. Requires
    `VM.Clone` on the source template's path plus `VM.Allocate` on the
    target/pool, `Datastore.AllocateSpace`, and `SDN.Use` on the vnet it's
    later attached to (confirmed from the apidoc schema's own permission
    description, 2026-09-19) -- `VM.Clone` confirmed absent on the current
    token, to be granted when this is wired up and tested live.
    """
    params: dict = {"newid": target_vmid, "name": name, "full": 1 if full else 0}
    if target_node is not None:
        params["target"] = target_node
    if target_storage is not None:
        params["storage"] = target_storage
    if pool is not None:
        params["pool"] = pool
    data, error = _post_data(host, port, trust, token, _qemu_clone_path(node, source_vmid), timeout, data=params)
    if error is not None or not isinstance(data, str):
        raise PveWriteError(error or f"unexpected response for clone_vm: {data!r}")
    return data


def stop_vm(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, node: str, vmid: int
) -> str | None:
    """`POST /nodes/{node}/qemu/{vmid}/status/stop` -- a hard stop (not a
    graceful guest shutdown, deliberately: Recovery Lab cleanup is
    tearing down a disposable restore-test clone, not something worth
    waiting on a guest OS to shut down cleanly). Returns the UPID; the
    caller polls it to completion (`get_task_status`) before deleting,
    same two-step shape as `clone_vm`'s own caller in `lab_agent.py`.
    Safe to call on an already-stopped VM -- PVE no-ops it.

    Returns `None` (not an error) if the VM doesn't exist at all -- same
    "already gone" tolerance as `delete_sdn_vnet`/`destroy_vm`, needed
    here for a Recovery Lab member whose restore never actually ran
    (still `PENDING`) but whose `target_vmid` was pre-assigned at plan-
    creation time regardless.
    """
    data, error = _post_data(host, port, trust, token, _qemu_stop_path(node, vmid), timeout)
    if error is not None:
        if error.startswith("HTTP 404") or "does not exist" in error:
            return None
        raise PveWriteError(error)
    if not isinstance(data, str):
        raise PveWriteError(f"unexpected response for stop_vm: {data!r}")
    return data


def destroy_vm(host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, node: str, vmid: int) -> None:
    """`DELETE /nodes/{node}/qemu/{vmid}` -- permanently removes a VM and
    its disks. The VM must already be stopped (`stop_vm`, polled to
    completion) -- PVE rejects deleting a running VM.

    `purge=1` also drops the vmid from any backup-job/replication config
    still referencing it; `destroy-unreferenced-disks=1` also removes
    disks not attached to the VM's current config but still owned by it
    (e.g. an old, replaced Recovery Lab appliance clone's disk). Both
    are the right defaults specifically because every VM this is called
    on is a disposable Recovery Lab restore-test clone or probe
    appliance (ADR 0019), never a real customer VM -- this function
    itself doesn't enforce that; the caller
    (`cleanup_restore_test_plan`) must confirm pool membership first.

    Treats "already gone" as success, same shape/reasoning as
    `delete_sdn_vnet`.
    """
    _, error = _delete_data(
        host,
        port,
        trust,
        token,
        _qemu_delete_path(node, vmid),
        timeout,
        params={"purge": 1, "destroy-unreferenced-disks": 1},
    )
    if error is not None:
        if error.startswith("HTTP 404") or "does not exist" in error:
            return
        raise PveWriteError(error)


def configure_lab_appliance_network(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    timeout: float,
    *,
    node: str,
    vmid: int,
    net0: str,
    net1: str,
    ipconfig0: str,
    ipconfig1: str,
    cicustom: str,
    nameserver: str | None = None,
) -> None:
    """`PUT /nodes/{node}/qemu/{vmid}/config` -- the freshly-cloned probe
    appliance's dual-NIC + cloud-init wiring (Recovery Lab Phase B).
    `net0`/`ipconfig0` put it on the run's isolated Lab VNet as that
    segment's own static gateway (it runs its own DHCP/firewall/SNAT --
    PVE never provisions a subnet on the VNet itself, a direct user
    correction: the PVE host must never sit in the lab's network data
    path). `net1`/`ipconfig1` is its uplink onto an existing, already-
    connected bridge for reaching the RESURGAM backend. `cicustom` points
    at the run's snippet (`<storage>:snippets/<file>`) written by
    `resurgam-lab-agent` (see `restore_test/execution.py` -- no PVE REST
    endpoint exists to upload a snippet file, confirmed live 2026-09-21).
    `nameserver` (cloud-init DNS, space-separated `address-list`) is only
    sent for a static uplink (`lab_setup/uplink.py`); with DHCP the guest
    takes DNS from its lease.
    """
    params = {"net0": net0, "net1": net1, "ipconfig0": ipconfig0, "ipconfig1": ipconfig1, "cicustom": cicustom}
    if nameserver:
        params["nameserver"] = nameserver
    _, error = _put_data(host, port, trust, token, _qemu_config_path(node, vmid), timeout, data=params)
    if error is not None:
        raise PveWriteError(error)


def _qemu_agent_ping_path(node: str, vmid: int) -> str:
    return f"/api2/json/nodes/{node}/qemu/{vmid}/agent/ping"


def _qemu_agent_network_get_interfaces_path(node: str, vmid: int) -> str:
    return f"/api2/json/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"


def agent_ping(host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, node: str, vmid: int) -> bool:
    """`POST /nodes/{node}/qemu/{vmid}/agent/ping` -- Level 1 of the
    3-tier probe hierarchy (ADR 0019): is the guest agent reachable
    inside a just-booted Recovery Lab member. Confirmed live (2026-09-21)
    this is a POST, not GET -- PVE's own apidoc schema lists it under
    "Execute ping." This is a normal PVE-REST-proxied guest-agent
    management-channel query (QMP-backed), not a network-path operation,
    so it stays legitimately backend-driven even though Level 2/3 checks
    are delegated to the probe appliance. PVE reports "agent not running
    yet" (and "No QEMU guest agent configured" if the VM config itself
    never set `agent: 1` -- see `restore-worker`'s `create_vm`, which
    must set it) as an error response, not a `false` value in `data` --
    both are treated as "not reachable yet" (an expected polling state),
    not raised as a hard failure.
    """
    data, error = _post_data(host, port, trust, token, _qemu_agent_ping_path(node, vmid), timeout)
    if error is not None:
        return False
    return isinstance(data, dict)


def agent_network_get_interfaces(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, node: str, vmid: int
) -> list[str]:
    """`GET /nodes/{node}/qemu/{vmid}/agent/network-get-interfaces` --
    resolves a Recovery Lab member's real IP once its guest agent is
    reachable, so Level 2/3 checks have a target. Filtered to non-
    loopback, non-link-local IPv4 addresses -- same management-channel
    reasoning as `agent_ping`, not a network-path operation.
    """
    data, error = _get_data(host, port, trust, token, _qemu_agent_network_get_interfaces_path(node, vmid), timeout)
    if error is not None or not isinstance(data, dict):
        return []
    addresses: list[str] = []
    for iface in data.get("result", []) or []:
        if not isinstance(iface, dict):
            continue
        for entry in iface.get("ip-addresses", []) or []:
            if not isinstance(entry, dict) or entry.get("ip-address-type") != "ipv4":
                continue
            ip = entry.get("ip-address")
            if isinstance(ip, str) and ip != "127.0.0.1" and not ip.startswith("169.254."):
                addresses.append(ip)
    return addresses


# --- running a backup job now -----------------------------------------------------------
#
# PVE has no "run job" endpoint (checked against PVE's own API schema,
# apidoc.js, 2026-10-11: /cluster/backup/{id} only has GET/PUT/DELETE). Its
# GUI's "Run now" calls `POST /nodes/{node}/vzdump` with the job's settings
# on each node that holds guests of the job -- RESURGAM does the same.


def run_vzdump(host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, node: str, params: dict) -> str:
    """`POST /nodes/{node}/vzdump` -- starts a backup task on `node`; returns
    its UPID. Needs `VM.Backup` on the guests and `Datastore.AllocateSpace`
    on the storage; `prune-backups` additionally needs `Datastore.Allocate`
    on the storage, and `bwlimit`/`performance`/`ionice` need `Sys.Modify`
    on `/` (PVE API schema, apidoc.js)."""
    data, error = _post_data(host, port, trust, token, f"/api2/json/nodes/{node}/vzdump", timeout, data=params)
    if error is not None or not isinstance(data, str):
        raise PveWriteError(error or f"unexpected response for vzdump: {data!r}")
    return data


TASK_LOG_PAGE_SIZE = 500


def get_task_log(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, node: str, upid: str, start: int = 0
) -> list[tuple[int, str]] | None:
    """`GET /nodes/{node}/tasks/{upid}/log?start=N&limit=500` -- task log
    lines after skipping the first `start` lines, as (line number, text).
    PVE's line numbers `n` are 1-based (`dump_logfile`: `next if $count++ <
    $start; push {n => $count}`), so the last returned `n` is the next
    `start`. An empty log comes back as PVE's placeholder `{n: 1, t: "no
    content"}`; it is dropped here, so the caller sees no lines and does not
    advance. None when unreadable."""
    data, error = _get_data(
        host,
        port,
        trust,
        token,
        f"{_task_path(node, upid)}/log",
        timeout,
        params={"start": start, "limit": TASK_LOG_PAGE_SIZE},
    )
    if error is not None or not isinstance(data, list):
        return None
    lines = []
    for entry in data:
        if isinstance(entry, dict) and isinstance(entry.get("n"), int):
            lines.append((entry["n"], str(entry.get("t", ""))))
    if lines == [(1, "no content")]:
        return []
    return lines


def _qemu_move_disk_path(node: str, vmid: int) -> str:
    return f"/api2/json/nodes/{node}/qemu/{vmid}/move_disk"


def move_vm_disk(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    timeout: float,
    *,
    node: str,
    vmid: int,
    disk: str,
    storage: str,
    delete_source: bool = True,
    bwlimit_kib: int | None = None,
) -> str:
    """`POST /nodes/{node}/qemu/{vmid}/move_disk` -- Instant Recovery's "Move to
    storage" (ADR 0030 §5). Returns the UPID of the `qmmove` task.

    Parameters per the qemu-server source (`API2/Qemu.pm`, `move_vm_disk`):
    `disk`, `storage`, `delete` ("Delete the original disk after successful
    copy"), `bwlimit` (KiB/s). Permission: `VM.Config.Disk` on the VM and
    `Datastore.AllocateSpace` on the target storage. For a running VM the copy
    is a QEMU `blockdev-mirror` with `sync: full` (`clone_disk`), so the result
    holds the overlay *and* the backup data underneath it; PVE switches the VM
    to the new disk without a restart. Not live-verified yet (ADR 0030, live
    checks 3 and 4)."""
    params: dict = {"disk": disk, "storage": storage, "delete": 1 if delete_source else 0}
    if bwlimit_kib is not None:
        params["bwlimit"] = bwlimit_kib
    data, error = _post_data(host, port, trust, token, _qemu_move_disk_path(node, vmid), timeout, data=params)
    if error is not None or not isinstance(data, str):
        raise PveWriteError(error or f"unexpected response for move_vm_disk: {data!r}")
    return data
