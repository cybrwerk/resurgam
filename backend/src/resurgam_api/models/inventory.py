import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from resurgam_api.db import Base
from resurgam_api.inventory.file_index_defaults import default_file_index_exclude_patterns
from resurgam_api.models.base import CreatedAtMixin, UUIDPrimaryKeyMixin


class BackupType(str, Enum):
    VM = "vm"
    CT = "ct"
    HOST = "host"


class VerifyStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    NONE = "none"
    UNKNOWN = "unknown"


class TaskType(str, Enum):
    BACKUP = "backup"
    VERIFY = "verify"
    PRUNE = "prune"
    GARBAGE_COLLECTION = "garbage_collection"
    SYNC = "sync"
    UNKNOWN = "unknown"


class TaskStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    RUNNING = "running"
    UNKNOWN = "unknown"


class ProtectionStatus(str, Enum):
    """Manifest §9.4's per-backup-group status. Computed on read
    (`inventory/protection.py`), never stored — it's a function of the
    group's current state and its policy, not a fact to persist."""

    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class ProtectionPolicy(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Manifest §9.5: what "protected" means for a set of backup groups.
    Belongs to exactly one customer; a backup group opts in by setting its
    `protection_policy_id` (nullable — no policy means status UNKNOWN, not
    a default pass). `sync_dr_required` is stored but not yet evaluated —
    that needs Sync-/DR-Status (EPIC 6, not built), a deliberate gap like
    the Collector's own stated simplifications, not a silent one.
    """

    __tablename__ = "protection_policies"
    __table_args__ = (UniqueConstraint("customer_id", "name", name="uq_protection_policy_customer_name"),)

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    expected_interval_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    max_time_since_last_backup_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_verify_deadline_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_snapshot_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sync_dr_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class BackupGroup(Base, UUIDPrimaryKeyMixin):
    """Manifest §9.4 "Backup-Inventar": one protected VM/CT/host, identified
    by its PBS backup-type + backup-id within a datastore (and namespace).
    Populated by the Collector (`collector/sync.py`) — never written to by
    end users directly.

    A VMID can be deleted and reused for a completely different guest,
    raised directly by the user: `collector.sync.sync_datastore_inventory`
    detects this (a new snapshot's own PBS comment differs from this
    row's already-established `resolved_name`) and splits history rather
    than silently blending two unrelated VMs' data into one timeline --
    this row gets `superseded_at` set and a fresh `BackupGroup` row opens
    for the same identity tuple, starting blank (no policy, no customer
    override, file indexing off), never inheriting this row's settings.
    Already-recorded snapshots are never reclassified; only newly synced
    ones route to whichever generation is open when they're processed.
    At most one *current* (`superseded_at IS NULL`) row per identity
    tuple is enforced at the database level via a partial unique index
    (migration `0039_backup_group_generations.py`), not a plain
    `UniqueConstraint` here, since many superseded rows can share the
    same identity tuple over time.
    """

    __tablename__ = "backup_groups"

    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Distinct from `superseded_at` (which means "renamed/replaced by a
    # newer generation at this same identity tuple") -- this means the
    # backup itself is simply gone from PBS (deleted, pruned away
    # entirely), with no successor. Raised directly by the user after a
    # whole namespace deleted on PBS kept showing as live in RESURGAM for
    # hours: the Collector was purely additive, never noticing removal.
    # Set/cleared by `collector.sync.sync_datastore_inventory` each pass
    # by comparing the live snapshot listing against every currently-open
    # (`superseded_at IS NULL`) local row for the datastore -- a group
    # PBS reports again later (undeleted, or a fresh backup at the same
    # identity) just clears this rather than opening a new generation,
    # since nothing about its identity actually changed. History (past
    # snapshots, report references, Recovery Lab runs) is preserved,
    # never deleted -- this only hides it from "current" listings.
    removed_from_pbs_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    datastore_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datastores.id", ondelete="CASCADE"), nullable=False
    )
    namespace: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    backup_type: Mapped[str] = mapped_column(String(20), nullable=False)
    backup_id: Mapped[str] = mapped_column(String(100), nullable=False)
    resolved_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Not from the manifest — PBS's own reported owner (e.g. "tenant-a@pbs"),
    # confirmed live against real snapshot data but not previously stored.
    # Read-only, surfaced next to the manual customer picker as a hint —
    # not used for any auto-assignment logic (a heuristic customer-match
    # from a free-text field carries real tenant-isolation risk, same
    # caution as ADR 0009's rejected cross-instance VM identity idea).
    # Taken from whichever snapshot was most recently synced, like
    # `last_verify_status` below.
    owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_backup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_verify_status: Mapped[str] = mapped_column(String(20), nullable=False, default=VerifyStatus.UNKNOWN.value)
    protection_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("protection_policies.id", ondelete="SET NULL"), nullable=True
    )
    # Not from the manifest — added for finer-grained tenancy than
    # "one customer per datastore": a shared datastore can hold VMs
    # belonging to different customers. Null means "inherit the
    # datastore's own customer assignment"; set means "this specific
    # group belongs to a different customer than its datastore" — an
    # explicit override, never a second, competing assignment.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    # Manifest §13 File Indexing (ADR 0016), per-VM opt-in raised directly
    # by the user in place of a customer-wide-only switch: a group is
    # actually indexed only when this is true *and* its effective
    # customer's CustomerFileIndexSettings.enabled is also true -- the
    # customer-level flag stays a fast "pause everything for this
    # customer" kill switch, this one picks exactly which backup groups.
    file_index_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Manifest §13 File Indexing (ADR 0016), raised directly by the user
    # after watching a real VM's index job spend most of its time walking
    # an irrelevant kernel-headers tree: a list of regex patterns matched
    # against each discovered entry's path during indexing. A match skips
    # the entry (and, for VM disks, its whole subtree) entirely. Defaults
    # to `DEFAULT_FILE_INDEX_EXCLUDE_PATTERNS` for every newly synced
    # group -- active out of the box, not merely suggested; changing a
    # group's own list away from the default is the operator's
    # intentional action via the exclude-patterns endpoint below, not
    # something that happens automatically.
    file_index_exclude_patterns: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=default_file_index_exclude_patterns
    )
    # Guest-exec File Indexing (ADR 0021): "external_mount" (default,
    # unchanged -- PBS's own file-restore/catalog tools walking the
    # backup externally) vs two opt-in guest-side methods that solve
    # encrypted disks (the guest has already decrypted everything to
    # boot/run) and enable true incremental indexing (find -newermt /
    # LastWriteTime filtering, since both run inside the guest itself):
    # "guest_exec_live" (a PVE hookscript fires guest-exec against the
    # already-running production VM at its own real backup's
    # backup-end phase -- a PVE VM backup never stops the guest) or
    # "guest_exec_ephemeral" (boots a disposable snapshot clone via
    # Recovery Lab's zero-copy map/boot machinery, indexes that, then
    # destroys it -- never touches production). VM-only in practice --
    # CT/host's single dump_catalog() call already has no boot/
    # encryption problem to solve.
    file_index_method: Mapped[str] = mapped_column(String(20), nullable=False, default="external_mount")
    # Required only for "guest_exec_live": the PVE cluster hosting this
    # group's *production* VM -- deliberately distinct from any
    # `PveCluster.is_lab_compute` cluster (Recovery Lab's disposable-clone
    # pool). `BackupGroup` has no other PVE linkage (it's populated purely
    # from PBS-side data by `collector.sync`) -- this is the one place
    # that ties a group back to a specific cluster, and only
    # guest_exec_live needs it: `api/v1/lab_agent.py`'s `_owning_cluster_id`
    # uses it to scope which agent (authenticated per-cluster) is allowed
    # to see/claim this group's guest-exec-live jobs, and
    # `report_backup_completed` uses it to confirm a backup-completed
    # report actually came from the right cluster's agent. The hookscript
    # itself is a node-level vzdump `script:` hook (confirmed live
    # 2026-09-24, see ADR 0021's addendum), not something reconciled per
    # VM against this field. Set together with `file_index_method` via
    # `set-file-index-method`, required (not merely allowed) once that
    # method is chosen.
    guest_exec_live_pve_cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pve_clusters.id", ondelete="SET NULL"), nullable=True
    )
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BackupSnapshot(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "backup_snapshots"
    __table_args__ = (UniqueConstraint("backup_group_id", "snapshot_time", name="uq_backup_snapshot_identity"),)

    backup_group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backup_groups.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verify_status: Mapped[str] = mapped_column(String(20), nullable=False, default=VerifyStatus.UNKNOWN.value)
    # BigInteger, not Integer: a single VM/CT backup routinely exceeds
    # INT4's ~2.1GB ceiling — confirmed necessary against real PBS usage
    # data (a single datastore's `used` bytes was ~2.18TB).
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Client-side encryption as PBS reports it (ADR 0031): "none", "encrypt",
    # "sign-only", "mixed", or NULL when unknown (not synced since 0071).
    crypt_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # The encryption key's fingerprint ("ad:a6:9b:...") for an encrypted snapshot.
    key_fingerprint: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TaskRun(Base, UUIDPrimaryKeyMixin):
    """Manifest §4.1: the Collector normalizes PBS task history (backup,
    verify, prune, garbage collection, sync results).
    """

    __tablename__ = "task_runs"
    __table_args__ = (UniqueConstraint("pbs_instance_id", "upid", name="uq_task_run_upid"),)

    pbs_instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pbs_instances.id", ondelete="CASCADE"), nullable=False
    )
    upid: Mapped[str] = mapped_column(String(255), nullable=False)
    task_type: Mapped[str] = mapped_column(String(30), nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=TaskStatus.UNKNOWN.value)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    task_user: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PbsAclEntry(Base, UUIDPrimaryKeyMixin):
    """A PBS-native ACL grant, mirrored from `GET /access/acl` -- raised
    directly by the user after the Recovery Evidence report's "Access
    Control" section was shown to only ever list RESURGAM's own role
    assignments, completely missing real access granted natively in PBS
    (a real user with `Admin` on a customer's whole datastore, invisible
    to RESURGAM). A live mirror of PBS's *current* ACL state, not
    history -- re-synced by delete-then-reinsert per instance
    (`collector.sync.sync_instance_acl`), matching how `TaskRun`'s
    sibling sync functions refresh their own tables. Instance-scoped
    (`pbs_instance_id`), not datastore-scoped: PBS's ACL API is
    instance-wide, and one grant's `path` can apply to many datastores
    (`/`, `/datastore`) or just one (`/datastore/<name>`) or a namespace
    within one -- resolving which datastore(s) a given entry actually
    covers is report-compute-time logic
    (`reporting.recovery_evidence._acl_entry_applies_to_datastore`), not
    something this table itself encodes.
    """

    __tablename__ = "pbs_acl_entries"

    pbs_instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pbs_instances.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    role: Mapped[str] = mapped_column(String(100), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(20), nullable=False)
    propagate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
