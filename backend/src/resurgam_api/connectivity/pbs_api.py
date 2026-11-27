from resurgam_api.connectivity._http import request_json
from resurgam_api.connectivity.models import (
    AclListResult,
    DatastoreListResult,
    DatastoreMaintenanceResult,
    DatastoreUsageListResult,
    GroupNotesResult,
    NamespaceListResult,
    PbsPruneJob,
    PbsPruneJobListResult,
    PbsVerifyJob,
    PbsVerifyJobListResult,
    PermissionsResult,
    ProbeResult,
    RemoteAclEntry,
    RemoteDatastore,
    RemoteDatastoreUsage,
    RemoteListResult,
    RemoteRemote,
    RemoteS3Endpoint,
    RemoteSnapshot,
    RemoteSyncJob,
    RemoteTask,
    S3EndpointListResult,
    S3Statistics,
    SnapshotListResult,
    SyncJobListResult,
    TaskListResult,
    TaskLogLine,
    TaskLogResult,
    TrustConfig,
)

VERSION_PATH = "/api2/json/version"
DATASTORE_CONFIG_PATH = "/api2/json/config/datastore"
TASKS_PATH = "/api2/json/nodes/localhost/tasks"
TIME_PATH = "/api2/json/nodes/localhost/time"
DATASTORE_USAGE_PATH = "/api2/json/status/datastore-usage"
S3_CONFIG_PATH = "/api2/json/config/s3"
REMOTE_CONFIG_PATH = "/api2/json/config/remote"
SYNC_CONFIG_PATH = "/api2/json/config/sync"
PRUNE_CONFIG_PATH = "/api2/json/config/prune"
VERIFY_CONFIG_PATH = "/api2/json/config/verify"
ACL_PATH = "/api2/json/access/acl"
ACCESS_PERMISSIONS_PATH = "/api2/json/access/permissions"


def _get_data(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    path: str,
    timeout: float,
    params: dict | None = None,
) -> tuple[list | dict | str | None, str | None]:
    """GET a PBS API path and return its `data` field, or an error message.

    Shared by every PBS REST call in this module: resolves TLS trust, sends
    the request, and unwraps the `{"data": ...}` envelope PBS is assumed to
    use everywhere (docs/milestone-1-pbs-pve-api-assumptions.md).
    """
    return request_json("GET", host, port, trust, token, path, timeout, auth_scheme="PBSAPIToken", params=params)


def _get_full_response(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    path: str,
    timeout: float,
    params: dict | None = None,
) -> tuple[dict | None, str | None]:
    """Like `_get_data`, but returns the full parsed JSON body rather than
    just its `data` field -- needed for `get_task_log` below, whose real
    envelope carries a `total` count as a *sibling* of `data`, which
    `_get_data`'s `.get("data")` unwrap would otherwise silently discard.
    """
    return request_json(
        "GET", host, port, trust, token, path, timeout, auth_scheme="PBSAPIToken", params=params, unwrap_data=False
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
    """PUT a PBS API path and return its `data` field, or an error message
    -- mirrors `pve_api.py`'s `_put_data`. This is the first PBS *write*
    this codebase makes; everything above is read-only.
    """
    return request_json(
        "PUT", host, port, trust, token, path, timeout, auth_scheme="PBSAPIToken", data=data, ok_statuses=(200, 201)
    )


def _post_data(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    path: str,
    timeout: float,
    data: dict | None = None,
) -> tuple[list | dict | str | None, str | None]:
    """POST a PBS API path and return its `data` field, or an error
    message -- mirrors `pve_api.py`'s `_post_data`, added for Job
    Administration's create endpoints (docs/adr/0023-backup-and-maintenance-job-administration).
    """
    return request_json(
        "POST", host, port, trust, token, path, timeout, auth_scheme="PBSAPIToken", data=data, ok_statuses=(200, 201)
    )


def _delete_data(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    path: str,
    timeout: float,
) -> tuple[list | dict | str | None, str | None]:
    """DELETE a PBS API path and return its `data` field, or an error
    message.
    """
    return request_json(
        "DELETE", host, port, trust, token, path, timeout, auth_scheme="PBSAPIToken", ok_statuses=(200, 201)
    )


def _with_delete(changes: dict, delete: list[str] | None) -> dict:
    """Merge a PBS `delete` list into a PUT body.

    PBS's config-update endpoints (`/config/prune/{id}`, `/config/verify/
    {id}`, `/config/sync/{id}`, `/config/datastore/{name}`) take a
    `delete` parameter of type *array* listing the properties to remove
    (pbs.proxmox.com/docs/api-viewer/apidoc.js). The body is sent form-
    encoded, so httpx encodes the list as repeated `delete=<prop>` keys --
    the same encoding PBS's own web UI uses for array parameters.
    """
    data = dict(changes)
    if delete:
        data["delete"] = sorted(delete)
    return data


class PbsWriteError(Exception):
    """Raised by every PBS write call below on failure -- mirrors
    `pve_api.PveWriteError`.
    """


def probe_pbs(host: str, port: int, trust: TrustConfig, token: str, timeout: float = 5.0) -> ProbeResult:
    """Check reachability and read the version of a PBS instance.

    `host` must be a bare hostname/IP (no scheme) — PBS is always accessed
    over HTTPS. `token` is the full auth-id:secret string PBS expects after
    `PBSAPIToken=` (see docs/milestone-1-pbs-pve-api-assumptions.md — the
    exact header/response shape here is unverified against a real PBS).
    """
    data, error = _get_data(host, port, trust, token, VERSION_PATH, timeout)
    if error is not None:
        return ProbeResult(reachable=False, error=error)
    version = data.get("version") if isinstance(data, dict) else None
    return ProbeResult(reachable=True, version=version)


def list_datastores(host: str, port: int, trust: TrustConfig, token: str, timeout: float = 5.0) -> DatastoreListResult:
    """List datastores configured on a PBS instance.

    Hits `/api2/json/config/datastore` — unverified against a real PBS; see
    docs/milestone-1-pbs-pve-api-assumptions.md.
    """
    data, error = _get_data(host, port, trust, token, DATASTORE_CONFIG_PATH, timeout)
    if error is not None:
        return DatastoreListResult(reachable=False, error=error)

    try:
        datastores = [
            RemoteDatastore(
                name=entry["name"], path=entry.get("path"), comment=entry.get("comment"), **parse_backend(entry)
            )
            for entry in data
        ]
    except (TypeError, KeyError) as exc:
        return DatastoreListResult(reachable=False, error=f"unexpected response shape: {exc}")

    return DatastoreListResult(reachable=True, datastores=datastores)


def parse_backend(entry: dict) -> dict:
    """The `backend` property of a PBS 4.x datastore config entry, e.g.
    `type=s3,client=<endpoint id>,bucket=<bucket>` (confirmed live, PBS 4.2),
    as `RemoteDatastore` keyword arguments. No `backend` = a filesystem
    datastore, as on every PBS before 4.0. Endpoint ids and bucket names
    contain no commas, so a plain split is enough."""
    raw = entry.get("backend")
    if not isinstance(raw, str) or not raw:
        return {"backend_type": "filesystem"}
    props: dict[str, str] = {}
    for part in raw.split(","):
        key, sep, value = part.partition("=")
        if sep:
            props[key.strip()] = _unquote_property_value(value.strip())
        elif "type" not in props:
            # The default key of the property string is `type`.
            props["type"] = key.strip()
    backend_type = props.get("type") or "filesystem"
    if backend_type != "s3":
        return {"backend_type": backend_type}
    return {"backend_type": "s3", "s3_endpoint": props.get("client"), "s3_bucket": props.get("bucket")}


def list_s3_endpoints(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float = 5.0
) -> S3EndpointListResult:
    """PBS 4.x S3 endpoint configs, `GET /api2/json/config/s3` (confirmed
    live, PBS 4.2; readable with Sys.Audit). An older PBS without S3 support
    answers 404 / an error -- reported as unreachable, the caller treats that
    as "no endpoints known" and changes nothing."""
    data, error = _get_data(host, port, trust, token, S3_CONFIG_PATH, timeout)
    if error is not None:
        return S3EndpointListResult(reachable=False, error=error)
    try:
        endpoints = [
            RemoteS3Endpoint(
                id=entry["id"],
                endpoint=entry["endpoint"],
                port=entry.get("port"),
                region=entry.get("region"),
                path_style=bool(entry.get("path-style", False)),
                fingerprint=entry.get("fingerprint"),
            )
            for entry in data
        ]
    except (TypeError, KeyError) as exc:
        return S3EndpointListResult(reachable=False, error=f"unexpected response shape: {exc}")
    return S3EndpointListResult(reachable=True, endpoints=endpoints)


def get_s3_statistics(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, store: str
) -> S3Statistics | None:
    """`s3-statistics` from `GET /admin/datastore/{store}/status` (confirmed
    live, PBS 4.2: get/put/post/head/delete counts plus uploaded/downloaded
    bytes). None when PBS can't be asked or the datastore isn't S3-backed."""
    data, error = _get_data(host, port, trust, token, f"/api2/json/admin/datastore/{store}/status", timeout)
    if error is not None or not isinstance(data, dict):
        return None
    stats = data.get("s3-statistics")
    if not isinstance(stats, dict):
        return None
    try:
        return S3Statistics(
            get=int(stats.get("get", 0)),
            put=int(stats.get("put", 0)),
            post=int(stats.get("post", 0)),
            head=int(stats.get("head", 0)),
            delete=int(stats.get("delete", 0)),
            uploaded_bytes=int(stats.get("uploaded", 0)),
            downloaded_bytes=int(stats.get("downloaded", 0)),
        )
    except (TypeError, ValueError):
        return None


def _datastore_config_item_path(name: str) -> str:
    return f"{DATASTORE_CONFIG_PATH}/{name}"


def get_datastore_maintenance_mode(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, name: str
) -> DatastoreMaintenanceResult:
    """Read one datastore's `maintenance-mode` via `GET
    /config/datastore/{name}` -- confirmed live (2026-09-19) that this
    per-item endpoint (unlike the list endpoint) carries the field, absent
    entirely when unset.

    The wire format is a real PBS "property string" with `type` as the
    schema's default/positional key: `<mode>[,message=<text>]` -- the
    mode itself carries no `type=` prefix (PBS both accepts and re-emits
    it bare), only `message` needs an explicit key. Two wrong formats
    were tried and rejected live before this one, in order: the initially
    assumed plain `type[,message]` free-text shape 400'd ("parameter
    verification failed... 'type': duplicate key" -- an un-prefixed
    message was parsed as a second positional `type` value); an explicit
    `type=<mode>,message=<text>` PUT was *accepted* (200) but GET then
    echoed it back without the `type=` prefix, proving that's not what
    PBS actually stores/expects for reads. Confirmed live 2026-09-19 by
    probing the real endpoint directly with each variant. The message is a
    double-quoted property-string value (`message="a, b \\"c\\""`): confirmed
    live 2026-11-21 that PBS accepts and stores it that way -- an unquoted
    message with a comma was rejected ("multiple values without a key").
    """
    data, error = _get_data(host, port, trust, token, _datastore_config_item_path(name), timeout)
    if error is not None:
        return DatastoreMaintenanceResult(reachable=False, error=error)
    if not isinstance(data, dict):
        return DatastoreMaintenanceResult(reachable=False, error=f"unexpected response shape: {data!r}")

    raw = data.get("maintenance-mode")
    if not raw:
        return DatastoreMaintenanceResult(reachable=True, mode=None, message=None)

    mode, _, rest = raw.partition(",")
    message = None
    if rest.startswith("message="):
        message = _unquote_property_value(rest[len("message=") :]) or None
    return DatastoreMaintenanceResult(reachable=True, mode=mode, message=message)


def _quote_property_value(value: str) -> str:
    """A PBS property-string value in double quotes, `\\` and `"` escaped --
    so a comma or `=` inside it stays part of the value."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote_property_value(value: str) -> str:
    """Reverses `_quote_property_value`; a bare (unquoted) value is returned
    as is, as older RESURGAM versions and other tools may have written one."""
    if not value.startswith('"'):
        return value
    out: list[str] = []
    escaped = False
    for char in value[1:]:
        if escaped:
            out.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            break
        else:
            out.append(char)
    return "".join(out)


def set_datastore_maintenance_mode(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    timeout: float,
    *,
    name: str,
    mode: str | None,
    message: str | None,
) -> None:
    """`PUT /config/datastore/{name}` to set or clear `maintenance-mode`.
    Requires `Datastore.Modify` on the token (a real privilege gap found
    live 2026-09-19 -- the default RESURGAM PBS token only had
    Audit/Read/Verify, since resolved). Raises `PbsWriteError` on failure.

    Wire format is `<mode>[,message=<text>]` -- a real PBS property
    string with `mode` as the bare/positional default key, confirmed live
    2026-09-19 (see `get_datastore_maintenance_mode`'s docstring for the
    two other formats tried and rejected first).
    """
    if mode is None:
        _, error = _put_data(
            host, port, trust, token, _datastore_config_item_path(name), timeout, data={"delete": "maintenance-mode"}
        )
    else:
        value = f"{mode},message={_quote_property_value(message)}" if message else mode
        _, error = _put_data(
            host, port, trust, token, _datastore_config_item_path(name), timeout, data={"maintenance-mode": value}
        )
    if error is not None:
        raise PbsWriteError(error)


def get_datastore_gc_schedule(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, name: str
) -> tuple[bool, str | None, str | None]:
    """Read one datastore's `gc-schedule` via `GET /config/datastore/{name}`.

    Garbage collection is *not* a separate job list the way prune/verify/
    sync are (docs/adr/0023-backup-and-maintenance-job-administration) -- PBS models it as a bare
    schedule string directly on the datastore's own config, so this reuses
    the same per-item config path `get_datastore_maintenance_mode` already
    established rather than adding a job-list-shaped abstraction for a
    field that isn't one. Returns `(reachable, gc_schedule, error)`.
    """
    data, error = _get_data(host, port, trust, token, _datastore_config_item_path(name), timeout)
    if error is not None:
        return False, None, error
    if not isinstance(data, dict):
        return False, None, f"unexpected response shape: {data!r}"
    return True, data.get("gc-schedule"), None


def set_datastore_gc_schedule(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, name: str, schedule: str | None
) -> None:
    """`PUT /config/datastore/{name}` to set (or, with `schedule=None`,
    clear) `gc-schedule`. Requires `Datastore.Modify`, same as maintenance-
    mode. Raises `PbsWriteError` on failure.
    """
    if schedule is None:
        _, error = _put_data(
            host, port, trust, token, _datastore_config_item_path(name), timeout, data={"delete": "gc-schedule"}
        )
    else:
        _, error = _put_data(
            host, port, trust, token, _datastore_config_item_path(name), timeout, data={"gc-schedule": schedule}
        )
    if error is not None:
        raise PbsWriteError(error)


def list_namespaces(
    host: str, port: int, trust: TrustConfig, token: str, datastore: str, timeout: float = 5.0
) -> NamespaceListResult:
    """List namespaces configured in a datastore, including the root.

    Hits `/api2/json/admin/datastore/{store}/namespace` — confirmed
    against a real PBS 4.2 instance to return `{"data": [{"ns": "<path>"},
    ...]}`, with the root namespace represented as `{"ns": ""}`.
    """
    path = f"/api2/json/admin/datastore/{datastore}/namespace"
    data, error = _get_data(host, port, trust, token, path, timeout)
    if error is not None:
        return NamespaceListResult(reachable=False, error=error)

    try:
        namespaces = [entry["ns"] for entry in data]
    except (TypeError, KeyError) as exc:
        return NamespaceListResult(reachable=False, error=f"unexpected response shape: {exc}")

    return NamespaceListResult(reachable=True, namespaces=namespaces)


# Files whose crypt mode says nothing about the backup's content: the manifest
# itself is only signed in an encrypted backup ("sign-only"), and the client log
# is uploaded afterwards without a mode.
_CRYPT_MODE_IGNORED_FILES = {"index.json.blob", "client.log.blob"}


def snapshot_crypt_mode(files: object) -> str | None:
    """Summarizes a PBS snapshot's per-file `crypt-mode` for its data
    archives: "none", "encrypt", "sign-only", "mixed" when they differ, or
    None when PBS reported no mode at all (ADR 0031). Live PBS 4.2 shapes:
    unencrypted VM backup -> every file "none"; encrypted backup -> data
    archives "encrypt", `index.json.blob` "sign-only"."""
    if not isinstance(files, list):
        return None
    modes = {
        f.get("crypt-mode")
        for f in files
        if isinstance(f, dict) and f.get("filename") not in _CRYPT_MODE_IGNORED_FILES and f.get("crypt-mode")
    }
    if not modes:
        return None
    return modes.pop() if len(modes) == 1 else "mixed"


def _list_snapshots_in_namespace(
    host: str, port: int, trust: TrustConfig, token: str, datastore: str, namespace: str, timeout: float
) -> tuple[list[RemoteSnapshot] | None, str | None]:
    path = f"/api2/json/admin/datastore/{datastore}/snapshots"
    params = {"ns": namespace} if namespace else None
    data, error = _get_data(host, port, trust, token, path, timeout, params=params)
    if error is not None:
        return None, error

    try:
        snapshots = [
            RemoteSnapshot(
                backup_type=entry["backup-type"],
                backup_id=str(entry["backup-id"]),
                backup_time=entry["backup-time"],
                namespace=namespace,
                verify_state=(entry.get("verification") or {}).get("state"),
                size=entry.get("size"),
                owner=entry.get("owner"),
                comment=entry.get("comment"),
                crypt_mode=snapshot_crypt_mode(entry.get("files")),
                key_fingerprint=entry.get("fingerprint") or None,
            )
            for entry in data
        ]
    except (TypeError, KeyError) as exc:
        return None, f"unexpected response shape: {exc}"
    return snapshots, None


def list_snapshots(
    host: str, port: int, trust: TrustConfig, token: str, datastore: str, timeout: float = 5.0
) -> SnapshotListResult:
    """List every snapshot in a datastore, across every namespace.

    Confirmed live against a real PBS 4.2 instance (2026-09-14) that this
    needs real work, not just a query param: `/admin/datastore/{store}/
    snapshots` is scoped to exactly *one* namespace per call (root when
    `ns` is omitted) and does **not** recurse into sub-namespaces — a
    `max-depth` parameter was tried and rejected outright (`400`, "schema
    does not allow additional properties"). A real datastore with an empty
    root namespace but hundreds of snapshots under `tenant-a` and
    `site-b/tenant-b` previously looked completely empty to RESURGAM.
    Fixed by listing namespaces first (`list_namespaces`) and querying
    each one explicitly; falls back to root-only if namespace listing
    itself is unreachable (e.g. a PBS version without namespace support),
    rather than failing the whole call outright. Confirmed live: a
    namespaced query's response entries don't carry their own
    `backup-namespace` field back, so each snapshot's namespace is
    attributed from the query that returned it, not parsed off the entry
    (the original assumption here was wrong).
    """
    ns_result = list_namespaces(host, port, trust, token, datastore, timeout)
    namespaces = ns_result.namespaces if ns_result.reachable and ns_result.namespaces else [""]

    all_snapshots: list[RemoteSnapshot] = []
    for namespace in namespaces:
        batch, error = _list_snapshots_in_namespace(host, port, trust, token, datastore, namespace, timeout)
        if error is not None:
            return SnapshotListResult(reachable=False, error=error)
        all_snapshots.extend(batch or [])

    return SnapshotListResult(reachable=True, snapshots=all_snapshots)


def list_tasks(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float = 5.0, limit: int = 2000
) -> TaskListResult:
    """List recent tasks (backup/verify/prune/GC/sync) on a PBS instance.

    Hits `/api2/json/nodes/localhost/tasks`, assuming the PBS node is always
    named `localhost` (PBS is single-node per instance, unlike PVE) — this
    part confirmed against a real PBS 4.2 instance. PBS defaults this
    endpoint to the most recent 50 tasks; `limit` raises that (verified via
    `?limit=`), but a single instance with more than `limit` tasks since the
    last sync will still miss the oldest ones — a stated simplification,
    not a silent one, same spirit as the Collector's snapshot-pruning gap.
    """
    data, error = _get_data(host, port, trust, token, TASKS_PATH, timeout, params={"limit": limit})
    if error is not None:
        return TaskListResult(reachable=False, error=error)

    try:
        tasks = [
            RemoteTask(
                upid=entry["upid"],
                task_type=entry["worker_type"],
                worker_id=entry.get("worker_id"),
                status=entry.get("status"),
                start_time=entry["starttime"],
                end_time=entry.get("endtime"),
                task_user=entry.get("user"),
            )
            for entry in data
        ]
    except (TypeError, KeyError) as exc:
        return TaskListResult(reachable=False, error=f"unexpected response shape: {exc}")

    return TaskListResult(reachable=True, tasks=tasks)


TASK_LOG_TAIL_LINES = 5000  # guard against an unbounded/runaway backup log


def _task_log_path(upid: str) -> str:
    return f"/api2/json/nodes/localhost/tasks/{upid}/log"


def get_task_log(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    timeout: float = 5.0,
    *,
    upid: str,
    tail_lines: int = TASK_LOG_TAIL_LINES,
) -> TaskLogResult:
    """Fetches the *last* `tail_lines` of a PBS task's log (for a failed
    task the actual error is almost always near the end, so a tail beats
    a head truncation for a bounded fetch). Confirmed live against a real
    PBS 4.2 instance (2026-09-17), including with a real UPID containing
    `\\x2d`/`\\x3a`-escaped characters in its worker-id segment, sent
    unencoded in the URL path exactly as stored: `start` is a 1-indexed
    absolute line number to begin from (`start=0` is a special-cased
    alias for "from the beginning", not a skip-count), and the envelope
    is `{"data": [...], "success": 1, "total": <int>}` -- `total` sits
    outside `data`, hence `_get_full_response` rather than `_get_data`.

    Two-step: a cheap first request (`limit=1`) discovers the log's real
    `total` line count, then a second request fetches the tail via
    `start=max(1, total - tail_lines + 1)`. If the first request's
    envelope doesn't carry a usable `total` for any reason, falls back to
    a head fetch (`start=0`) instead of failing outright -- a degraded
    but honest result, not a guess.
    """
    body, error = _get_full_response(
        host, port, trust, token, _task_log_path(upid), timeout, params={"start": 0, "limit": 1}
    )
    if error is not None:
        return TaskLogResult(reachable=False, error=error)
    if not isinstance(body, dict):
        return TaskLogResult(reachable=False, error=f"unexpected response shape: {body!r}")

    total = body.get("total")
    start = max(1, total - tail_lines + 1) if isinstance(total, int) else 0

    body, error = _get_full_response(
        host, port, trust, token, _task_log_path(upid), timeout, params={"start": start, "limit": tail_lines}
    )
    if error is not None:
        return TaskLogResult(reachable=False, error=error)
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return TaskLogResult(reachable=False, error=f"unexpected response shape: {body!r}")

    try:
        lines = [TaskLogLine(n=entry["n"], text=entry.get("t", "")) for entry in body["data"]]
    except (TypeError, KeyError) as exc:
        return TaskLogResult(reachable=False, error=f"unexpected response shape: {exc}")

    truncated = isinstance(total, int) and total > len(lines)
    return TaskLogResult(
        reachable=True, lines=lines, total=total if isinstance(total, int) else None, truncated=truncated
    )


def list_datastore_usage(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float = 5.0
) -> DatastoreUsageListResult:
    """List capacity usage for every datastore on a PBS instance.

    Hits `/api2/json/status/datastore-usage` — confirmed against a real PBS
    4.2 instance to return `{"data": [{"store", "used", "avail", "total",
    "backend-type", "gc-status": {...}, "history": [...], "history-start",
    "history-delta", ...}, ...]}`. Read: `store`/`used`/`avail`/`total`,
    `backend-type` and `gc-status.disk-bytes` (ADR 0033: for an S3 datastore
    used/avail/total describe the local cache filesystem, the bucket's data
    is what the last GC found). `history` (PBS's own periodic fill-ratio
    samples) stays unused -- RESURGAM stores its own
    `usage_measurements` rows instead of parsing PBS's, so multi-instance
    reporting has one consistent source (manifest §9.3), not two.
    """
    data, error = _get_data(host, port, trust, token, DATASTORE_USAGE_PATH, timeout)
    if error is not None:
        return DatastoreUsageListResult(reachable=False, error=error)

    try:
        usage = [
            RemoteDatastoreUsage(
                name=entry["store"],
                used_bytes=entry["used"],
                available_bytes=entry["avail"],
                total_bytes=entry["total"],
                backend_type=entry.get("backend-type") or "filesystem",
                gc_disk_bytes=_gc_disk_bytes(entry.get("gc-status")),
            )
            for entry in data
        ]
    except (TypeError, KeyError) as exc:
        return DatastoreUsageListResult(reachable=False, error=f"unexpected response shape: {exc}")

    return DatastoreUsageListResult(reachable=True, usage=usage)


def _gc_disk_bytes(gc_status: object) -> int | None:
    """`disk-bytes` of the last GC, or None when no GC has run yet (PBS then
    reports `upid: null` and zeros, confirmed live on a fresh S3 datastore)."""
    if not isinstance(gc_status, dict) or not gc_status.get("upid"):
        return None
    value = gc_status.get("disk-bytes")
    return int(value) if isinstance(value, int) else None


def get_server_timezone(host: str, port: int, trust: TrustConfig, token: str, timeout: float = 5.0) -> str | None:
    """The PBS server's own timezone (IANA name, e.g. `Europe/Zurich`), or
    None if it couldn't be read.

    PBS prune (`keep-hourly`/`keep-daily`/...) buckets snapshots by the
    server's *local* time (`pbs-datastore/src/prune.rs` formats each
    backup time with `strftime_local`), so "the last snapshot of a day"
    -- the one `keep-daily` keeps -- can only be computed with this zone.
    `/nodes/localhost/time` is readable by any authenticated token
    (`Permission::Anybody` in `src/api2/node/time.rs`) and returns
    `{"timezone": str, "time": int, "localtime": int}`.
    """
    data, error = _get_data(host, port, trust, token, TIME_PATH, timeout)
    if error is not None or not isinstance(data, dict):
        return None
    timezone = data.get("timezone")
    if not isinstance(timezone, str) or not timezone.strip() or len(timezone) > 64:
        return None
    return timezone.strip()


def get_group_notes(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    datastore: str,
    backup_type: str,
    backup_id: str,
    namespace: str = "",
    timeout: float = 5.0,
) -> GroupNotesResult:
    """Read a backup group's notes (PBS's per-*group* "Comment"/"Notes"
    field -- a separate, rarely-used field manually set on the group
    itself, distinct from `RemoteSnapshot.comment`, the per-*snapshot*
    field that actually receives a PVE vzdump job's `{{guestname}}`
    notes-template result. `collector.sync.sync_datastore_inventory`
    treats the snapshot comment as the primary `resolved_name` source and
    only calls this as a secondary fallback when it's empty -- confirmed
    live (2026-09-20) that group-notes is empty for essentially all real
    customer data, while the snapshot comment is populated for nearly all
    of it.

    Hits `/api2/json/admin/datastore/{store}/group-notes` — confirmed
    against a real PBS 4.2 instance to return `{"data": "<text>"}` (empty
    string, not absent, when no note is set), for both the root namespace
    and a real non-root group (`ns` sent as a query param, same as
    `list_snapshots`'s per-namespace calls).
    """
    path = f"/api2/json/admin/datastore/{datastore}/group-notes"
    params = {"backup-type": backup_type, "backup-id": backup_id}
    if namespace:
        params["ns"] = namespace
    data, error = _get_data(host, port, trust, token, path, timeout, params=params)
    if error is not None:
        return GroupNotesResult(reachable=False, error=error)
    return GroupNotesResult(reachable=True, notes=data if isinstance(data, str) else None)


def _remote_from_entry(entry: dict) -> RemoteRemote:
    return RemoteRemote(
        name=entry["name"],
        host=entry["host"],
        auth_id=entry.get("auth-id"),
        port=entry.get("port"),
        fingerprint=entry.get("fingerprint"),
        comment=entry.get("comment"),
    )


def list_remotes(host: str, port: int, trust: TrustConfig, token: str, timeout: float = 5.0) -> RemoteListResult:
    """List configured "Remotes" (other PBS servers this instance can pull
    from/push to for sync jobs) via `GET /config/remote`. Field shape
    confirmed live 2026-09-26 (see `RemoteRemote`'s own docstring).
    """
    data, error = _get_data(host, port, trust, token, REMOTE_CONFIG_PATH, timeout)
    if error is not None:
        return RemoteListResult(reachable=False, error=error)

    try:
        remotes = [_remote_from_entry(entry) for entry in data]
    except (TypeError, KeyError) as exc:
        return RemoteListResult(reachable=False, error=f"unexpected response shape: {exc}")

    return RemoteListResult(reachable=True, remotes=remotes)


def _remote_config_item_path(name: str) -> str:
    return f"{REMOTE_CONFIG_PATH}/{name}"


def create_remote(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    timeout: float,
    *,
    name: str,
    remote_host: str,
    auth_id: str,
    password: str,
    remote_port: int | None = None,
    fingerprint: str | None = None,
    comment: str | None = None,
) -> None:
    """`POST /config/remote` -- `password` is the remote PBS instance's
    own auth secret (a real API token secret or user password on *that*
    server), accepted here and forwarded straight through, never
    persisted by RESURGAM in any form. Deliberately different from the
    PBS-encryption-key case (docs/adr/0023-backup-and-maintenance-job-administration), where
    RESURGAM genuinely needs to re-present the secret on every future
    read: a remote's credential is used exclusively by *PBS's own*
    scheduler when a sync job actually runs, never by RESURGAM itself --
    there is nothing to re-use later, so there is nothing to store.
    """
    payload = {
        "name": name,
        "host": remote_host,
        "auth-id": auth_id,
        "password": password,
        "port": remote_port,
        "fingerprint": fingerprint,
        "comment": comment,
    }
    payload = {key: value for key, value in payload.items() if value is not None}
    _, error = _post_data(host, port, trust, token, REMOTE_CONFIG_PATH, timeout, data=payload)
    if error is not None:
        raise PbsWriteError(error)


def update_remote(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, name: str, changes: dict
) -> None:
    """`PUT /config/remote/{name}` -- `changes` already uses PBS's real
    wire field names (e.g. `{"host": "new.example.com"}`), built by the
    caller from only the fields an operator actually touched.
    """
    _, error = _put_data(host, port, trust, token, _remote_config_item_path(name), timeout, data=changes)
    if error is not None:
        raise PbsWriteError(error)


def delete_remote(host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, name: str) -> None:
    """`DELETE /config/remote/{name}`."""
    _, error = _delete_data(host, port, trust, token, _remote_config_item_path(name), timeout)
    if error is not None:
        raise PbsWriteError(error)


def _sync_job_from_entry(entry: dict) -> RemoteSyncJob:
    return RemoteSyncJob(
        job_id=entry["id"],
        local_store=entry["store"],
        remote_store=entry["remote-store"],
        remote_name=entry.get("remote"),
        ns=entry.get("ns"),
        schedule=entry.get("schedule"),
        remove_vanished=entry.get("remove-vanished"),
        owner=entry.get("owner"),
        comment=entry.get("comment"),
        sync_direction=entry.get("sync-direction") or "pull",
        remote_ns=entry.get("remote-ns"),
        max_depth=entry.get("max-depth"),
        group_filter=tuple(entry["group-filter"]) if isinstance(entry.get("group-filter"), list) else None,
        transfer_last=entry.get("transfer-last"),
    )


def list_sync_jobs(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float = 5.0, *, all_directions: bool = False
) -> SyncJobListResult:
    """List configured "Sync Jobs" via `GET /config/sync`. Field shape
    confirmed live 2026-09-26 (see `RemoteSyncJob`'s own docstring).

    PBS returns only pull jobs unless asked for `sync-direction=all` (its
    schema's default is `pull`). `all_directions=True` asks for both, and
    falls back to the plain call on a PBS older than 3.3, which rejects the
    unknown parameter.
    """
    if all_directions:
        data, error = _get_data(host, port, trust, token, SYNC_CONFIG_PATH, timeout, params={"sync-direction": "all"})
        if error is not None:
            data, error = _get_data(host, port, trust, token, SYNC_CONFIG_PATH, timeout)
    else:
        data, error = _get_data(host, port, trust, token, SYNC_CONFIG_PATH, timeout)
    if error is not None:
        return SyncJobListResult(reachable=False, error=error)

    try:
        sync_jobs = [_sync_job_from_entry(entry) for entry in data]
    except (TypeError, KeyError) as exc:
        return SyncJobListResult(reachable=False, error=f"unexpected response shape: {exc}")

    return SyncJobListResult(reachable=True, sync_jobs=sync_jobs)


def _sync_job_config_item_path(job_id: str) -> str:
    return f"{SYNC_CONFIG_PATH}/{job_id}"


def get_sync_job(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, job_id: str
) -> RemoteSyncJob | None:
    """`GET /config/sync/{id}` -- one job's full current definition."""
    data, error = _get_data(host, port, trust, token, _sync_job_config_item_path(job_id), timeout)
    if error is not None or not isinstance(data, dict):
        return None
    try:
        return _sync_job_from_entry(data)
    except (TypeError, KeyError):
        return None


def create_sync_job(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, job: RemoteSyncJob
) -> None:
    """`POST /config/sync` -- `job.job_id` is required and always sent
    explicitly (confirmed live 2026-09-26 this endpoint's create response
    is likewise empty, matching vzdump's own behavior -- same reasoning
    as `pve_api.create_vzdump_job`). No `disable` field -- confirmed live
    2026-09-27 rejected outright (see `RemoteSyncJob`'s own docstring).
    """
    if not job.job_id:
        raise ValueError("create_sync_job requires job.job_id to be set")
    payload = {
        "id": job.job_id,
        "store": job.local_store,
        "remote-store": job.remote_store,
        "remote": job.remote_name,
        "ns": job.ns,
        "schedule": job.schedule,
        "remove-vanished": job.remove_vanished,
        "owner": job.owner,
        "comment": job.comment,
    }
    payload = {key: value for key, value in payload.items() if value is not None}
    _, error = _post_data(host, port, trust, token, SYNC_CONFIG_PATH, timeout, data=payload)
    if error is not None:
        raise PbsWriteError(error)


# Property names PBS's `PUT /config/sync/{id}` accepts in its `delete`
# list (source: pbs.proxmox.com/docs/api-viewer/apidoc.js, `delete` items
# enum, checked 2026-10-06) -- restricted to the fields RESURGAM edits.
# `remote-store` is required by PBS and therefore not deletable.
SYNC_JOB_DELETABLE = frozenset({"remote", "owner", "comment", "schedule", "remove-vanished", "ns"})


def update_sync_job(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    timeout: float,
    *,
    job_id: str,
    changes: dict,
    delete: list[str] | None = None,
) -> None:
    """`PUT /config/sync/{id}` with only `changes` (already using PBS's
    real wire field names) plus an optional `delete` list of properties
    to remove (see `_with_delete`).
    """
    data = _with_delete(changes, delete)
    _, error = _put_data(host, port, trust, token, _sync_job_config_item_path(job_id), timeout, data=data)
    if error is not None:
        raise PbsWriteError(error)


def delete_sync_job(host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, job_id: str) -> None:
    """`DELETE /config/sync/{id}`."""
    _, error = _delete_data(host, port, trust, token, _sync_job_config_item_path(job_id), timeout)
    if error is not None:
        raise PbsWriteError(error)


def list_acl_entries(host: str, port: int, trust: TrustConfig, token: str, timeout: float = 5.0) -> AclListResult:
    """List every PBS-native ACL grant on this instance, raised directly
    by the user after RESURGAM's own "Access Control" report section was
    shown to only ever list RESURGAM's own role assignments -- a real
    PBS user with direct `Admin` access to a customer's entire datastore
    (granted natively in PBS, outside RESURGAM entirely) was completely
    invisible.

    Hits `/api2/json/access/acl` — confirmed live against a real PBS 4.2
    instance (2026-09-20): `{"data": [{"path", "roleid", "ugid",
    "ugid_type", "propagate"}, ...]}`, `propagate` already a real JSON
    boolean on the wire (unlike `list_vzdump_jobs`'s `enabled`, a `0`/`1`
    int) -- `bool(...)` here is just defensive, not a real cast.
    Instance-wide, not scoped to one datastore -- narrowing to
    which grants actually apply to a given customer's datastore is done
    at report-compute time (`reporting.recovery_evidence`), not here,
    since PBS's own ACL propagation semantics require comparing against
    every datastore a report might ask about, not just one.
    """
    data, error = _get_data(host, port, trust, token, ACL_PATH, timeout)
    if error is not None:
        return AclListResult(reachable=False, error=error)

    try:
        entries = [
            RemoteAclEntry(
                path=entry["path"],
                role=entry["roleid"],
                subject=entry["ugid"],
                subject_type=entry["ugid_type"],
                propagate=bool(entry.get("propagate", 1)),
            )
            for entry in data
        ]
    except (TypeError, KeyError) as exc:
        return AclListResult(reachable=False, error=f"unexpected response shape: {exc}")

    return AclListResult(reachable=True, entries=entries)


# --- Prune jobs (docs/adr/0023-backup-and-maintenance-job-administration) ------------------------------
#
# The *intended* home for retention policy in this project's design -- a
# real, explicit product decision (2026-09-26) to keep retention out of
# vzdump jobs entirely, confirmed live to already be how this real
# environment is actually configured (real PBS prune jobs already exist;
# the real vzdump jobs carry no prune settings of their own).


def _prune_job_from_entry(entry: dict) -> PbsPruneJob:
    return PbsPruneJob(
        id=entry["id"],
        store=entry["store"],
        schedule=entry.get("schedule"),
        ns=entry.get("ns"),
        keep_last=entry.get("keep-last"),
        keep_hourly=entry.get("keep-hourly"),
        keep_daily=entry.get("keep-daily"),
        keep_weekly=entry.get("keep-weekly"),
        keep_monthly=entry.get("keep-monthly"),
        keep_yearly=entry.get("keep-yearly"),
        disable=entry.get("disable") == 1,
        comment=entry.get("comment"),
    )


def _prune_job_path(job_id: str) -> str:
    return f"{PRUNE_CONFIG_PATH}/{job_id}"


def list_prune_jobs(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float = 5.0
) -> PbsPruneJobListResult:
    """List configured Prune Jobs via `GET /config/prune`. Field shape
    confirmed live 2026-09-26 (see `PbsPruneJob`'s own docstring).
    """
    data, error = _get_data(host, port, trust, token, PRUNE_CONFIG_PATH, timeout)
    if error is not None:
        return PbsPruneJobListResult(reachable=False, error=error)
    try:
        jobs = [_prune_job_from_entry(entry) for entry in data]
    except (TypeError, KeyError) as exc:
        return PbsPruneJobListResult(reachable=False, error=f"unexpected response shape: {exc}")
    return PbsPruneJobListResult(reachable=True, jobs=jobs)


def get_prune_job(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, job_id: str
) -> PbsPruneJob | None:
    """`GET /config/prune/{id}` -- one job's full current definition."""
    data, error = _get_data(host, port, trust, token, _prune_job_path(job_id), timeout)
    if error is not None or not isinstance(data, dict):
        return None
    try:
        return _prune_job_from_entry(data)
    except (TypeError, KeyError):
        return None


def create_prune_job(host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, job: PbsPruneJob) -> None:
    """`POST /config/prune` -- confirmed live (2026-09-26) required
    fields: `id`, `schedule`, `store`. `job.id` is always sent explicitly,
    same reasoning as `pve_api.create_vzdump_job`.
    """
    if not job.id:
        raise ValueError("create_prune_job requires job.id to be set")
    payload = {
        "id": job.id,
        "store": job.store,
        "schedule": job.schedule,
        "ns": job.ns,
        "keep-last": job.keep_last,
        "keep-hourly": job.keep_hourly,
        "keep-daily": job.keep_daily,
        "keep-weekly": job.keep_weekly,
        "keep-monthly": job.keep_monthly,
        "keep-yearly": job.keep_yearly,
        "disable": 1 if job.disable else None,
        "comment": job.comment,
    }
    payload = {key: value for key, value in payload.items() if value is not None}
    _, error = _post_data(host, port, trust, token, PRUNE_CONFIG_PATH, timeout, data=payload)
    if error is not None:
        raise PbsWriteError(error)


# PBS's `PUT /config/prune/{id}` `delete` enum (pbs.proxmox.com/docs/
# api-viewer/apidoc.js, checked 2026-10-06), restricted to the fields
# RESURGAM edits. `schedule` is NOT in PBS's enum: a prune job always
# needs one.
PRUNE_JOB_DELETABLE = frozenset(
    {"comment", "disable", "ns", "keep-last", "keep-hourly", "keep-daily", "keep-weekly", "keep-monthly", "keep-yearly"}
)


def update_prune_job(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    timeout: float,
    *,
    job_id: str,
    changes: dict,
    delete: list[str] | None = None,
) -> None:
    """`PUT /config/prune/{id}` with only `changes` (PBS's real wire
    field names, e.g. `{"keep-daily": 14}`) plus an optional `delete`
    list of properties to remove (see `_with_delete`)."""
    data = _with_delete(changes, delete)
    _, error = _put_data(host, port, trust, token, _prune_job_path(job_id), timeout, data=data)
    if error is not None:
        raise PbsWriteError(error)


def delete_prune_job(host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, job_id: str) -> None:
    """`DELETE /config/prune/{id}`."""
    _, error = _delete_data(host, port, trust, token, _prune_job_path(job_id), timeout)
    if error is not None:
        raise PbsWriteError(error)


# --- Verify jobs (docs/adr/0023-backup-and-maintenance-job-administration) -----------------------------


def _verify_job_from_entry(entry: dict) -> PbsVerifyJob:
    return PbsVerifyJob(
        id=entry["id"],
        store=entry["store"],
        schedule=entry.get("schedule"),
        ns=entry.get("ns"),
        outdated_after=entry.get("outdated-after"),
        ignore_verified=entry.get("ignore-verified"),
        comment=entry.get("comment"),
    )


def _verify_job_path(job_id: str) -> str:
    return f"{VERIFY_CONFIG_PATH}/{job_id}"


def list_verify_jobs(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float = 5.0
) -> PbsVerifyJobListResult:
    """List configured Verify Jobs via `GET /config/verify`."""
    data, error = _get_data(host, port, trust, token, VERIFY_CONFIG_PATH, timeout)
    if error is not None:
        return PbsVerifyJobListResult(reachable=False, error=error)
    try:
        jobs = [_verify_job_from_entry(entry) for entry in data]
    except (TypeError, KeyError) as exc:
        return PbsVerifyJobListResult(reachable=False, error=f"unexpected response shape: {exc}")
    return PbsVerifyJobListResult(reachable=True, jobs=jobs)


def get_verify_job(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, job_id: str
) -> PbsVerifyJob | None:
    """`GET /config/verify/{id}` -- one job's full current definition."""
    data, error = _get_data(host, port, trust, token, _verify_job_path(job_id), timeout)
    if error is not None or not isinstance(data, dict):
        return None
    try:
        return _verify_job_from_entry(data)
    except (TypeError, KeyError):
        return None


def create_verify_job(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, job: PbsVerifyJob
) -> None:
    """`POST /config/verify` -- confirmed live (2026-09-26) required
    fields: `id`, `store` (unlike prune, `schedule` is genuinely
    optional). `job.id` always sent explicitly. No `disable` field --
    confirmed live 2026-09-27 rejected outright (see `PbsVerifyJob`'s
    own docstring).
    """
    if not job.id:
        raise ValueError("create_verify_job requires job.id to be set")
    payload = {
        "id": job.id,
        "store": job.store,
        "schedule": job.schedule,
        "ns": job.ns,
        "outdated-after": job.outdated_after,
        "ignore-verified": None if job.ignore_verified is None else (1 if job.ignore_verified else 0),
        "comment": job.comment,
    }
    payload = {key: value for key, value in payload.items() if value is not None}
    _, error = _post_data(host, port, trust, token, VERIFY_CONFIG_PATH, timeout, data=payload)
    if error is not None:
        raise PbsWriteError(error)


# PBS's `PUT /config/verify/{id}` `delete` enum (pbs.proxmox.com/docs/
# api-viewer/apidoc.js, checked 2026-10-06), restricted to the fields
# RESURGAM edits.
VERIFY_JOB_DELETABLE = frozenset({"ignore-verified", "comment", "schedule", "outdated-after", "ns"})


def update_verify_job(
    host: str,
    port: int,
    trust: TrustConfig,
    token: str,
    timeout: float,
    *,
    job_id: str,
    changes: dict,
    delete: list[str] | None = None,
) -> None:
    """`PUT /config/verify/{id}` with only `changes` plus an optional
    `delete` list of properties to remove (see `_with_delete`)."""
    data = _with_delete(changes, delete)
    _, error = _put_data(host, port, trust, token, _verify_job_path(job_id), timeout, data=data)
    if error is not None:
        raise PbsWriteError(error)


def delete_verify_job(host: str, port: int, trust: TrustConfig, token: str, timeout: float, *, job_id: str) -> None:
    """`DELETE /config/verify/{id}`."""
    _, error = _delete_data(host, port, trust, token, _verify_job_path(job_id), timeout)
    if error is not None:
        raise PbsWriteError(error)


def get_own_permissions(
    host: str, port: int, trust: TrustConfig, token: str, timeout: float = 5.0
) -> PermissionsResult:
    """`GET /access/permissions` -- the calling PBS token's own real,
    effective privileges. PBS has this same introspection endpoint as PVE
    (confirmed live 2026-09-26, same `{path: {privilege: True}}` shape) --
    Job Administration's capability gating for every PBS-side job
    category (prune/verify/sync/remotes/gc) is built on this, mirroring
    `pve_api.get_own_permissions` exactly.
    """
    data, error = _get_data(host, port, trust, token, ACCESS_PERMISSIONS_PATH, timeout)
    if error is not None:
        return PermissionsResult(reachable=False, error=error)
    if not isinstance(data, dict):
        return PermissionsResult(reachable=False, error=f"unexpected response shape: {data!r}")
    return PermissionsResult(reachable=True, privileges=data)
