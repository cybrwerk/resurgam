from dataclasses import dataclass

from resurgam_api.models.pbs import TlsTrustMode


@dataclass(frozen=True)
class TrustConfig:
    trust_mode: TlsTrustMode
    fingerprint_sha256: str | None = None


@dataclass(frozen=True)
class ProbeResult:
    reachable: bool
    version: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class RemoteDatastore:
    name: str
    path: str | None = None
    comment: str | None = None


@dataclass(frozen=True)
class DatastoreListResult:
    reachable: bool
    datastores: list[RemoteDatastore] | None = None
    error: str | None = None


@dataclass(frozen=True)
class DatastoreMaintenanceResult:
    """A PBS datastore's `maintenance-mode` field, from `GET
    /config/datastore/{name}` -- real, documented since PBS 2.2+, format
    `type[,message]`. Absent on the wire means no maintenance mode is
    set, represented here as `mode=None`.
    """

    reachable: bool
    mode: str | None = None
    message: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class RemoteSnapshot:
    backup_type: str
    backup_id: str
    backup_time: int
    namespace: str = ""
    verify_state: str | None = None
    size: int | None = None
    # PBS's own reported owner (e.g. "tenant-a@pbs") — confirmed live against
    # real snapshot data, previously observed but not parsed/stored (see
    # docs/milestone-1-pbs-pve-api-assumptions.md's Snapshot listing
    # section). Surfaced read-only next to the customer picker, not used
    # for any auto-assignment logic.
    owner: str | None = None
    # A per-*snapshot* comment (distinct from the per-*group* "notes"
    # field `get_group_notes` reads) -- confirmed live (2026-09-20) that
    # this is where a real PVE vzdump job's `{{guestname}}` notes-template
    # result actually lands, not group-notes (group-notes is a separate,
    # rarely-used, manually-set field). Empty string, not absent, when
    # unset. This is why `resolved_name` was blank for real customer data
    # even though every backup had a real guest name visible in the PBS
    # web UI: the wrong field was being read.
    comment: str | None = None
    # Client-side encryption (ADR 0031): PBS reports a `crypt-mode` per file
    # and, for an encrypted snapshot, the key's `fingerprint` -- confirmed live
    # on PBS 4.2 (2026-11-22). `crypt_mode` summarizes the data archives:
    # "none", "encrypt", "sign-only", "mixed", or None when PBS gave nothing.
    crypt_mode: str | None = None
    key_fingerprint: str | None = None


@dataclass(frozen=True)
class SnapshotListResult:
    reachable: bool
    snapshots: list[RemoteSnapshot] | None = None
    error: str | None = None


@dataclass(frozen=True)
class NamespaceListResult:
    reachable: bool
    namespaces: list[str] | None = None
    error: str | None = None


@dataclass(frozen=True)
class GroupNotesResult:
    reachable: bool
    notes: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class RemoteTask:
    upid: str
    task_type: str
    worker_id: str | None
    status: str | None
    start_time: int
    end_time: int | None = None
    task_user: str | None = None


@dataclass(frozen=True)
class TaskListResult:
    reachable: bool
    tasks: list[RemoteTask] | None = None
    error: str | None = None


@dataclass(frozen=True)
class TaskLogLine:
    n: int
    text: str


@dataclass(frozen=True)
class TaskLogResult:
    """Result of `GET /nodes/localhost/tasks/{upid}/log` -- confirmed live
    against a real PBS 4.2 instance (2026-09-17): envelope is `{"data":
    [{"n": <int, 1-indexed>, "t": "<line>"}, ...], "success": 1, "total":
    <int>}` -- `total` sits *outside* `data`, unlike every other PBS call
    in this module, so this result carries it separately (`_get_data`'s
    plain `.get("data")` unwrap can't see it). `start` (the request param)
    is confirmed live to be a 1-indexed absolute line number to begin
    from, not a skip-count -- `start=0` is a special-cased alias for "from
    the beginning". `truncated` is set by the caller when fewer lines were
    fetched than the log actually has.
    """

    reachable: bool
    lines: list[TaskLogLine] | None = None
    total: int | None = None
    truncated: bool = False
    error: str | None = None


@dataclass(frozen=True)
class RemoteDatastoreUsage:
    name: str
    used_bytes: int
    available_bytes: int
    total_bytes: int


@dataclass(frozen=True)
class DatastoreUsageListResult:
    reachable: bool
    usage: list[RemoteDatastoreUsage] | None = None
    error: str | None = None


@dataclass(frozen=True)
class RemoteRemote:
    """A PBS "Remote" (another PBS server this instance can pull from/push
    to for sync jobs). Field names confirmed live (2026-09-26) against a
    real PBS 4.2 instance: `POST /config/remote` requires `name`, `host`,
    `auth-id`, `password`; `port`/`fingerprint`/`comment` are optional.
    `password` is write-only, like PVE's own `encryption-key`/`password`
    convention -- never returned by a GET, and RESURGAM never persists it
    either (see `create_remote`/`update_remote`'s own docstrings for why
    a plain pass-through is correct here, unlike the PBS-encryption-key
    case: PBS's own scheduler is the only thing that ever needs this
    secret again, not RESURGAM).
    """

    name: str
    host: str
    auth_id: str | None = None
    port: int | None = None
    fingerprint: str | None = None
    comment: str | None = None


@dataclass(frozen=True)
class RemoteListResult:
    reachable: bool
    remotes: list[RemoteRemote] | None = None
    error: str | None = None


@dataclass(frozen=True)
class RemoteSyncJob:
    """A PBS "Sync Job" (pull-based replication config). Field names
    confirmed live (2026-09-26) against a real PBS 4.2 instance: `POST
    /config/sync` requires `id`, `remote-store`, `store` -- `remote` is
    genuinely optional (a real PBS 4.x feature: omitting it means an
    intra-instance/namespace sync, not cross-server). **No `disable`
    field** -- confirmed live 2026-09-27 that both create and update
    reject it outright ("schema does not allow additional properties"),
    unlike prune jobs (which genuinely have one). A sync job can only be
    removed entirely, not toggled off; don't reintroduce this field
    without a fresh live create/update check.
    """

    job_id: str
    local_store: str
    remote_store: str
    remote_name: str | None = None
    ns: str | None = None
    schedule: str | None = None
    remove_vanished: bool | None = None
    owner: str | None = None
    comment: str | None = None


@dataclass(frozen=True)
class SyncJobListResult:
    reachable: bool
    sync_jobs: list[RemoteSyncJob] | None = None
    error: str | None = None


@dataclass(frozen=True)
class PbsPruneJob:
    """A PBS Prune Job (`/config/prune`) -- the *intended* home for
    retention policy in this project's design (docs/adr/0023-backup-and-
    maintenance-job-administration: a real, explicit product decision to keep retention
    out of vzdump jobs entirely). Field names confirmed live (2026-09-26):
    `POST /config/prune` requires `id`, `schedule`, `store`.
    """

    id: str
    store: str
    schedule: str | None = None
    ns: str | None = None
    keep_last: int | None = None
    keep_hourly: int | None = None
    keep_daily: int | None = None
    keep_weekly: int | None = None
    keep_monthly: int | None = None
    keep_yearly: int | None = None
    disable: bool = False
    comment: str | None = None


@dataclass(frozen=True)
class PbsPruneJobListResult:
    reachable: bool
    jobs: list[PbsPruneJob] | None = None
    error: str | None = None


@dataclass(frozen=True)
class PbsVerifyJob:
    """A PBS Verify Job (`/config/verify`). Field names confirmed live
    (2026-09-26): `POST /config/verify` requires `id`, `store` --
    `schedule` is genuinely optional (unlike prune's). **No `disable`
    field** -- confirmed live 2026-09-27 that both create and update
    reject it ("schema does not allow additional properties"), unlike
    prune jobs (which genuinely have one, confirmed the same day). Don't
    reintroduce this field without a fresh live create/update check.
    """

    id: str
    store: str
    schedule: str | None = None
    ns: str | None = None
    outdated_after: int | None = None
    ignore_verified: bool | None = None
    comment: str | None = None


@dataclass(frozen=True)
class PbsVerifyJobListResult:
    reachable: bool
    jobs: list[PbsVerifyJob] | None = None
    error: str | None = None


@dataclass(frozen=True)
class RemoteNode:
    """A PVE cluster node, from `GET /nodes` — field names confirmed live
    against a real PVE 9.2 cluster (2026-09-16), see
    docs/milestone-1-pbs-pve-api-assumptions.md.
    """

    node: str
    status: str | None = None
    cpu: float | None = None
    maxmem: int | None = None
    uptime: int | None = None


@dataclass(frozen=True)
class NodeListResult:
    reachable: bool
    nodes: list[RemoteNode] | None = None
    error: str | None = None


@dataclass(frozen=True)
class RemotePool:
    """A PVE resource pool (`GET /pools`) -- confirmed live 2026-09-27:
    `[{"poolid": ..., "comment": ...}]`, `comment` absent means no
    comment set. Used as one of the two real ways to target a vzdump
    job at a set of guests (docs/adr/0023-backup-and-maintenance-job-
    administration): explicit `vmid`s, or an existing pool.
    """

    poolid: str
    comment: str | None = None


@dataclass(frozen=True)
class PoolListResult:
    reachable: bool
    pools: list[RemotePool] | None = None
    error: str | None = None


@dataclass(frozen=True)
class PoolMembersResult:
    """`GET /pools/{poolid}` -- the single-pool detail endpoint, distinct
    from `GET /pools` (`PoolListResult` above): this one includes real
    guest membership, used by Recovery Lab cleanup as a guardrail before
    destroying a VM (raised directly by the user) -- only ever destroy a
    `vmid` actually confirmed a member of `resurgam-lab`, never trust a
    stored `target_vmid`/`appliance_vmid` blindly.
    """

    reachable: bool
    member_vmids: frozenset[int] | None = None
    error: str | None = None


@dataclass(frozen=True)
class RemoteStorage:
    """A PVE storage, merged from two real endpoints (confirmed live,
    2026-09-16): `GET /storage` (cluster-wide config — the only place a
    `type: "pbs"` entry's `server`/`datastore`/`namespace` fields appear)
    and `GET /nodes/{node}/storage` (per-node live status — `avail`/
    `used`/`total`/`active`, not present on the config listing). A PBS-type
    storage entry is scoped to exactly one PBS namespace, not a whole
    datastore — confirmed live (a real `pbs-main` storage entry carried
    `namespace: "tenant-a"`), which matters for `check_pbs_storage_visibility`.
    """

    storage: str
    type: str
    content: list[str]
    active: bool | None = None
    shared: bool | None = None
    avail_bytes: int | None = None
    used_bytes: int | None = None
    total_bytes: int | None = None
    pbs_server: str | None = None
    pbs_datastore: str | None = None
    pbs_namespace: str | None = None


@dataclass(frozen=True)
class StorageListResult:
    reachable: bool
    storages: list[RemoteStorage] | None = None
    error: str | None = None


@dataclass(frozen=True)
class RemoteGuest:
    """A VM or CT, from `GET /cluster/resources?type=vm` — cluster-wide,
    not per-node, since VMID uniqueness is cluster-wide in PVE (ADR 0009's
    caution about not assuming per-node/per-instance uniqueness). Field
    names confirmed live against a real PVE 9.2 cluster (2026-09-16).
    """

    vmid: int
    node: str
    guest_type: str  # "qemu" | "lxc"
    name: str | None = None
    status: str | None = None


@dataclass(frozen=True)
class GuestListResult:
    reachable: bool
    guests: list[RemoteGuest] | None = None
    error: str | None = None


@dataclass(frozen=True)
class VzdumpJob:
    """One configured vzdump backup job, from `GET /cluster/backup`/`GET
    /cluster/backup/{id}` (docs/adr/0018, docs/adr/0023-backup-and-maintenance-job-administration).
    Deliberately still not PVE's full ~30-field schema (`bwlimit`,
    `ionice`, `dumpdir`/`tmpdir`, `stopwait`/`lockwait`, `fleecing`,
    `performance`, `script`, `pbs-change-detection-mode`, `exclude-path`,
    `repeat-missed`, `zstd`/`pigz` thread tuning) -- the common,
    practically useful subset instead, matching this project's established
    "ship the common case, document the gap" pattern.

    `prune_keep_*` mirrors `PbsPruneJob`'s own field names on purpose --
    the intended, primary home for retention is still PBS's own Prune Job
    (scoped per datastore+namespace, `PbsPruneJob`), never this. But real
    live vzdump jobs on this environment were found (2026-09-27) to
    already carry their own `prune-backups` alongside separately-existing
    PBS Prune Jobs -- an operator choice this project doesn't get to
    override, so RESURGAM now surfaces and lets it be edited, while the
    UI cross-checks it against the target storage's real PBS Prune Job(s)
    (via `pbs_datastore`/`pbs_namespace` on `RemoteStorage`) and warns on
    a conflict or a gap, rather than silently hiding one mechanism.

    Wire format confirmed live 2026-09-27: `GET /cluster/backup` returns
    `prune-backups` as a nested JSON object with string-typed values
    (`{"keep-daily": "7", "keep-weekly": "4"}`), decoded here into ints;
    `PUT`/`POST` expect the flatted PVE property-string form instead
    (`keep-daily=7,keep-weekly=4`), built by
    `pve_api._encode_prune_backups`.
    """

    id: str
    enabled: bool
    schedule: str | None = None
    storage: str | None = None
    node: str | None = None
    comment: str | None = None
    all_guests: bool = False
    vmid: str | None = None
    pool: str | None = None
    exclude: str | None = None
    mode: str | None = None
    compress: str | None = None
    notes_template: str | None = None
    mailnotification: str | None = None
    mailto: str | None = None
    prune_keep_last: int | None = None
    prune_keep_hourly: int | None = None
    prune_keep_daily: int | None = None
    prune_keep_weekly: int | None = None
    prune_keep_monthly: int | None = None
    prune_keep_yearly: int | None = None


@dataclass(frozen=True)
class VzdumpJobListResult:
    reachable: bool
    jobs: list[VzdumpJob] | None = None
    error: str | None = None


@dataclass(frozen=True)
class PermissionsResult:
    """A token's real, effective privileges from `GET
    /access/permissions` -- PVE and PBS each expose this same shape
    (`{path: {privilege: True, ...}, ...}`), confirmed live against both
    this session (docs/adr/0023-backup-and-maintenance-job-administration). Job Administration's
    whole capability-gating design rests on this: which of import/edit/
    create RESURGAM offers for a given job category is driven by what's
    actually in here, not assumed from documentation.
    """

    reachable: bool
    privileges: dict[str, dict[str, bool]] | None = None
    error: str | None = None


@dataclass(frozen=True)
class PveTaskStatus:
    """Result of `GET /nodes/{node}/tasks/{upid}/status` -- a single async
    PVE task's current state (EPIC 9, manifest §11.2). `status` is PVE's
    own value: `"running"` while in progress, `"stopped"` once finished --
    success vs. failure is distinguished by `exitstatus` ("OK" means
    success), not by `status` itself. Deliberately not a reuse of
    `RemoteTask`/`TaskListResult`, which model PBS's list-recent-tasks
    response shape, a genuinely different endpoint.
    """

    reachable: bool
    status: str | None = None
    exitstatus: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class VmidAvailabilityResult:
    """Result of `GET /cluster/nextid[?vmid=N]` — confirmed live
    (2026-09-16): no `vmid` param returns the next free id as a *string*
    (`{"data": "109"}`); a candidate `vmid` that's already taken returns
    HTTP 400 with `{"errors": {"vmid": "VM <n> already exists"}}` — a real,
    expected outcome (`free=False`), not a connectivity error.
    """

    reachable: bool
    vmid: int | None = None
    free: bool | None = None
    error: str | None = None


@dataclass(frozen=True)
class RemoteAclEntry:
    """One PBS ACL grant, from `GET /access/acl` -- confirmed live
    (2026-09-20), raised directly by the user: RESURGAM's own
    "Access Control" report section only ever listed its *own* role
    assignments, completely missing real PBS-native grants (a real
    example found live: a PBS user with `Admin` on `/datastore/<name>`,
    invisible to RESURGAM). `path` follows PBS's own ACL hierarchy
    (`/`, `/datastore`, `/datastore/<name>`, `/datastore/<name>/<ns>`,
    ...); `propagate` governs whether the grant applies to paths
    *beneath* `path`, not to `path` itself (see
    `reporting.recovery_evidence._acl_entry_applies_to_datastore`, which
    encodes PBS's own propagation semantics.
    """

    path: str
    role: str
    subject: str
    subject_type: str
    propagate: bool


@dataclass(frozen=True)
class AclListResult:
    reachable: bool
    entries: list[RemoteAclEntry] | None = None
    error: str | None = None
