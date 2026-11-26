from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

GapExceptionKindLiteral = Literal["pve_guest", "pve_cluster", "backup_group", "datastore"]
GuestGapReasonLiteral = Literal["no_job", "only_disabled_jobs"]
OffsiteStatusLiteral = Literal[
    "ok", "lagging", "missing", "local_copy_only", "target_not_connected", "no_copy_job", "excepted", "not_required"
]

_OFFSITE_STATUS_HELP = (
    "`ok` (an offsite copy holds every snapshot, or the oldest one not copied yet is younger than the warning "
    "threshold), `lagging` (the oldest snapshot not copied yet is older than the warning threshold), `missing` (a sync "
    "job copies this group offsite but no copy exists and its oldest backup is older than the warning threshold), "
    "`local_copy_only` (the only copy is on the same PBS), `target_not_connected` (a sync job copies it to a PBS that "
    "is not connected to RESURGAM, so the copy cannot be checked), `no_copy_job` (no sync job covers this group), "
    "`excepted` (a platform admin accepted the gap, see `exception`), `not_required` (the customer's offsite 3-2-1-1-0 "
    "leg is marked not applicable)."
)


class GapExceptionOut(BaseModel):
    id: UUID = Field(description="ID of this exception row.")
    target_kind: GapExceptionKindLiteral = Field(
        description=(
            "What the exception covers: `pve_guest` (one guest, `pve_cluster_id` + `vmid`), `pve_cluster` (every "
            "guest of a cluster), `backup_group` (one backup group by `datastore_id` + `namespace` + `backup_type` + "
            "`backup_id`, also its later generations) or `datastore` (every backup group in a datastore)."
        )
    )
    target_label: str = Field(description="The target, readable (guest, cluster, workload or datastore name).")
    pve_cluster_id: UUID | None = Field(description="Set for `pve_guest` and `pve_cluster`.")
    vmid: int | None = Field(description="Set for `pve_guest`.")
    datastore_id: UUID | None = Field(description="Set for `backup_group` and `datastore`.")
    namespace: str | None = Field(description="Set for `backup_group`; empty string = root namespace.")
    backup_type: str | None = Field(description="Set for `backup_group`: `vm`, `ct` or `host`.")
    backup_id: str | None = Field(description="Set for `backup_group`: the PBS backup ID (usually the VMID).")
    active: bool = Field(description="True = the gap is accepted; false = this row revoked an earlier exception.")
    reason: str = Field(description="Why the gap is accepted or the exception revoked, as entered.")
    expires_at: datetime | None = Field(description="When the exception stops counting; null = until revoked.")
    created_by_user_id: UUID = Field(description="Who recorded this row.")
    created_by_name: str | None = Field(description="Display name of `created_by_user_id`.")
    created_at: datetime = Field(description="When this row was recorded.")


class CreateGapExceptionRequest(BaseModel):
    target_kind: GapExceptionKindLiteral = Field(description="What to except; see `GapExceptionOut.target_kind`.")
    pve_cluster_id: UUID | None = Field(default=None, description="Required for `pve_guest` and `pve_cluster`.")
    vmid: int | None = Field(default=None, ge=1, description="Required for `pve_guest`.")
    datastore_id: UUID | None = Field(default=None, description="Required for `backup_group` and `datastore`.")
    namespace: str | None = Field(
        default=None, max_length=255, description="For `backup_group`; empty or omitted = root namespace."
    )
    backup_type: Literal["vm", "ct", "host"] | None = Field(default=None, description="Required for `backup_group`.")
    backup_id: str | None = Field(
        default=None, min_length=1, max_length=100, description="Required for `backup_group`."
    )
    active: bool = Field(
        default=True,
        description="True records an exception; false revokes the current exception for this target.",
    )
    reason: str = Field(
        min_length=10,
        max_length=2000,
        description=(
            "Why, in words an auditor understands (e.g. 'Copied to tape weekly, see change 1234'). "
            "Stored permanently and shown in the Recovery Evidence report."
        ),
    )
    expires_at: datetime | None = Field(
        default=None,
        description="Optional end date (must be in the future); after it the gap counts again. Only with `active`.",
    )

    @model_validator(mode="after")
    def _targets_match_kind(self):
        required = {
            "pve_guest": ("pve_cluster_id", "vmid"),
            "pve_cluster": ("pve_cluster_id",),
            "backup_group": ("datastore_id", "backup_type", "backup_id"),
            "datastore": ("datastore_id",),
        }[self.target_kind]
        missing = [name for name in required if getattr(self, name) is None]
        if missing:
            raise ValueError(f"{self.target_kind} needs {', '.join(missing)}")
        if self.expires_at is not None and not self.active:
            raise ValueError("expires_at only applies to an active exception")
        return self


class GuestGapOut(BaseModel):
    pve_cluster_id: UUID = Field(description="The PVE cluster the guest runs on.")
    pve_cluster_name: str = Field(description="Display name of that cluster.")
    vmid: int = Field(description="The guest's VMID.")
    guest_type: str = Field(description="`qemu` (VM) or `lxc` (container).")
    name: str | None = Field(description="The guest's name in PVE.")
    reason: GuestGapReasonLiteral = Field(
        description=(
            "`no_job`: no backup job covers the guest (PVE's own `not-backed-up` list). `only_disabled_jobs`: only "
            "disabled backup jobs cover it (PVE's list counts those as coverage, RESURGAM does not)."
        )
    )
    first_seen_at: datetime = Field(description="Since when RESURGAM has seen this guest without an enabled job.")
    last_seen_at: datetime = Field(description="When the last check still found the gap.")
    exception: GapExceptionOut | None = Field(
        description="The active exception for this guest or its whole cluster; null = it counts as a gap."
    )


class PveClusterGapCheckOut(BaseModel):
    pve_cluster_id: UUID = Field(description="The PVE cluster.")
    pve_cluster_name: str = Field(description="Display name of the cluster.")
    checked_at: datetime | None = Field(
        description="When RESURGAM last checked the cluster for unprotected guests; null = not yet."
    )
    error: str | None = Field(
        description="Why the last check failed; null = it worked. On failure the previous result stays."
    )


class OffsiteGapOut(BaseModel):
    backup_group_id: UUID = Field(description="The source backup group (on the primary datastore).")
    workload_label: str = Field(description="Readable workload name (PBS notes / PVE name, else type/ID).")
    backup_type: str = Field(description="`vm`, `ct` or `host`.")
    backup_id: str = Field(description="The PBS backup ID (usually the VMID).")
    namespace: str = Field(description="PBS namespace of the group; empty string = root.")
    datastore_id: UUID = Field(description="The datastore holding the source group.")
    datastore_name: str = Field(description="Its name.")
    pbs_instance_name: str = Field(description="The PBS instance of that datastore.")
    customer_id: UUID | None = Field(description="The group's effective customer.")
    customer_name: str | None = Field(description="Display name of `customer_id`.")
    status: OffsiteStatusLiteral = Field(description="Offsite status after exceptions: " + _OFFSITE_STATUS_HELP)
    raw_status: OffsiteStatusLiteral = Field(
        description="The status before an exception or the leg setting overrode it (same values as `status`)."
    )
    severity: Literal["critical", "warning"] | None = Field(
        description=(
            "How bad the gap is (from `status`): `missing` and a lag over the critical threshold are critical; "
            "a lag over the warning threshold, `local_copy_only`, `target_not_connected` and `no_copy_job` (only "
            "when the group's protection policy requires Sync/DR) are warnings; null when it is no gap."
        )
    )
    copy_location: str | None = Field(
        description="Where the copy is or should be (PBS / datastore, or the remote's name if not connected)."
    )
    copy_backup_group_id: UUID | None = Field(description="The copy's own backup group, if RESURGAM sees it.")
    sync_jobs: list[str] = Field(description="Sync jobs that cover this group, readable.")
    latest_backup_at: datetime | None = Field(description="Newest snapshot of the source group.")
    latest_copy_at: datetime | None = Field(description="Newest snapshot of the offsite copy.")
    oldest_uncopied_at: datetime | None = Field(
        description="Oldest source snapshot newer than the copy's newest, i.e. not copied yet."
    )
    uncopied_count: int = Field(description="How many source snapshots are not copied yet.")
    lag_hours: float | None = Field(
        description="Age in hours of `oldest_uncopied_at` (0 = fully copied); null when there is no offsite copy job."
    )
    exception: GapExceptionOut | None = Field(
        description="The active exception for this group or its datastore, if any."
    )


class BackupGapThresholds(BaseModel):
    offsite_lag_warning_hours: int = Field(
        ge=1,
        le=8760,
        description="Offsite copy counts as lagging (warning) from this age of the oldest uncopied snapshot.",
    )
    offsite_lag_critical_hours: int = Field(
        ge=1, le=8760, description="From this age it is critical. Must be at least the warning value."
    )

    @model_validator(mode="after")
    def _critical_not_below_warning(self):
        if self.offsite_lag_critical_hours < self.offsite_lag_warning_hours:
            raise ValueError("offsite_lag_critical_hours must be >= offsite_lag_warning_hours")
        return self


class BackupGapSummary(BaseModel):
    unprotected_guests: int = Field(description="Guests without an enabled backup job and without an exception.")
    offsite_gaps: int = Field(description="Backup groups with an offsite gap (any `severity`), without an exception.")
    offsite_critical: int = Field(description="Of those, how many are critical.")
    offsite_max_lag_hours: float | None = Field(
        description="Largest `lag_hours` over all offsite copies that exist or are expected; null = none."
    )
    offsite_without_copy_job: int = Field(
        description=(
            "Backup groups no sync job covers (status `no_copy_job`). Only those whose protection policy requires "
            "Sync/DR count as a gap (`offsite_gaps`, alarms); the rest are listed but raise nothing."
        )
    )
    excepted: int = Field(description="Gaps currently covered by an exception (guests and backup groups).")


class BackupGapsOut(BaseModel):
    summary: BackupGapSummary = Field(description="Counts for the page header, alarms and Zabbix.")
    thresholds: BackupGapThresholds = Field(description="The offsite lag thresholds in effect.")
    pve_clusters: list[PveClusterGapCheckOut] = Field(
        description="Last unprotected-guest check per PVE cluster. Empty for non-platform-admins."
    )
    unprotected_guests: list[GuestGapOut] = Field(
        description=(
            "Guests no enabled backup job covers, including excepted ones (see `exception`). PVE guests have no "
            "customer, so this list is empty for non-platform-admins."
        )
    )
    offsite: list[OffsiteGapOut] = Field(
        description="Offsite status of every source backup group the caller may see, including healthy ones."
    )
