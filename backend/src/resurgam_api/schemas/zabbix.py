from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# Manifest §16 Observability: "Zabbix-kompatible Checks". Two concerns,
# matching what the user asked for directly -- job states and datastore
# capacity/usage -- each split into a Low-Level Discovery (LLD) endpoint
# (tells Zabbix *which* entities exist, so item prototypes are created
# automatically as datastores/jobs come and go) and a values endpoint
# (one flat payload per concern, meant to back a single Zabbix "master"
# HTTP agent item with per-entity "dependent items" extracting one value
# each via JSONPath preprocessing -- Zabbix's own recommended pattern
# since 5.0, rather than one HTTP poll per entity).
#
# LLD field names use Zabbix's own required `{#MACRO}` convention (see
# https://www.zabbix.com/documentation/current/en/manual/discovery/low_level_discovery)
# -- not valid Python identifiers, so every LLD entry aliases each field
# to its macro name; FastAPI serializes by alias by default.


class ZabbixDatastoreDiscoveryEntry(BaseModel):
    """One discovered datastore -- Zabbix uses these macros to create a
    usage-percent/used-bytes/etc. item prototype per real datastore."""

    model_config = ConfigDict(populate_by_name=True)

    datastore_id: str = Field(
        alias="{#DATASTORE_ID}",
        description="RESURGAM's own Datastore UUID -- matches the datastore_id key in GET /zabbix/datastores/usage.",
    )
    datastore_name: str = Field(alias="{#DATASTORE_NAME}", description="The datastore's PBS-native name.")
    pbs_instance_id: str = Field(alias="{#PBS_INSTANCE_ID}", description="The owning PbsInstance's UUID.")
    pbs_instance_name: str = Field(alias="{#PBS_INSTANCE_NAME}", description="The owning PBS instance's display name.")
    backend_type: str = Field(
        default="filesystem",
        alias="{#DATASTORE_BACKEND}",
        description="`filesystem` or `s3` (ADR 0033). For `s3` the usage items describe the local cache filesystem.",
    )


class ZabbixDatastoreDiscoveryOut(BaseModel):
    """Zabbix's own required LLD envelope: a top-level `data` array."""

    data: list[ZabbixDatastoreDiscoveryEntry]


class ZabbixDatastoreUsageValue(BaseModel):
    datastore_id: str = Field(description="Matches {#DATASTORE_ID} from GET /zabbix/discovery/datastores.")
    used_bytes: int = Field(
        description="Bytes in use on the datastore, as PBS reports it. For an S3 datastore (`backend_type` `s3`) "
        "this is the local cache filesystem, not the bucket -- see `s3_data_bytes`."
    )
    available_bytes: int = Field(description="Bytes still free on the datastore (S3: on the local cache filesystem).")
    total_bytes: int = Field(description="The datastore's total size in bytes (S3: of the local cache filesystem).")
    usage_percent: float = Field(
        description="used_bytes / total_bytes * 100, rounded to 2 decimals; 0 if total_bytes is 0."
    )
    captured_at: datetime = Field(description="When the Collector last measured this datastore's usage.")
    age_seconds: float = Field(
        description="Seconds since `captured_at` -- grows when the Collector stops reaching PBS; the basis "
        "for the template's 'usage not measured' trigger."
    )
    growth_bytes_per_day: int | None = Field(
        description="Net growth of used bytes per day over the last 30 days, after prune and garbage collection "
        "(same figure as the Capacity & Trends page); null during the first 7 days of history."
    )
    days_until_full: float = Field(
        description="Days until the datastore is full at `growth_bytes_per_day`. -1 when there is no full date "
        "(not growing, or fewer than 7 days of history) -- a number either way, so a Zabbix trigger resolves "
        "when growth stops. The template's 'full in' triggers fire between 0 and their threshold. Always -1 for "
        "an S3 datastore (a bucket has no fixed size, ADR 0033)."
    )
    backend_type: str = Field(
        default="filesystem", description="`filesystem` or `s3` (PBS 4.x S3-backed datastore, ADR 0033)."
    )
    s3_data_bytes: int | None = Field(
        default=None,
        description="S3 datastores only: chunk data in the bucket as found by the last garbage collection; null for "
        "filesystem datastores and before the first GC.",
    )


class ZabbixDatastoreUsageOut(BaseModel):
    """One flat payload covering every datastore's latest known usage --
    meant as a single Zabbix master item; datastores with no usage
    measurement yet (never synced) are omitted rather than sent as zeros.
    """

    datastores: list[ZabbixDatastoreUsageValue]


class ZabbixJobDiscoveryEntry(BaseModel):
    """One discovered (PBS instance, task type) pair with at least one
    recorded task -- Zabbix creates a job-status item prototype per pair.
    Task-level (per scheduled job config) discovery isn't offered: this
    app's own Task history is already normalized across every real job
    type (vzdump/prune/verify/sync) onto this one (instance, type) shape,
    which is what actually answers "is backup/verify/prune/sync healthy
    on this PBS instance" -- the Zabbix use case raised directly by the user.
    """

    model_config = ConfigDict(populate_by_name=True)

    pbs_instance_id: str = Field(alias="{#PBS_INSTANCE_ID}", description="The PBS instance's UUID.")
    pbs_instance_name: str = Field(alias="{#PBS_INSTANCE_NAME}", description="The PBS instance's display name.")
    task_type: str = Field(
        alias="{#TASK_TYPE}",
        description="backup | verify | prune | garbage_collection | sync | unknown (this app's own normalized TaskType vocabulary).",
    )


class ZabbixJobDiscoveryOut(BaseModel):
    """Zabbix's own required LLD envelope: a top-level `data` array."""

    data: list[ZabbixJobDiscoveryEntry]


class ZabbixJobStatusValue(BaseModel):
    pbs_instance_id: str = Field(description="Matches {#PBS_INSTANCE_ID} from GET /zabbix/discovery/jobs.")
    task_type: str = Field(description="Matches {#TASK_TYPE} from GET /zabbix/discovery/jobs.")
    status: str = Field(description="The most recent task's status: ok | error | running | unknown.")
    last_started_at: datetime = Field(description="When the most recent task of this type started.")
    last_ended_at: datetime | None = Field(description="Null while the most recent task is still running.")
    age_seconds: float = Field(
        description="Seconds since the most recent task of this type started on this instance -- "
        "the basis for a Zabbix trigger like 'no backup task on this PBS instance in 26h'.",
    )


class ZabbixJobStatusOut(BaseModel):
    """One flat payload covering every discovered (instance, task type)
    pair's most recent task -- meant as a single Zabbix master item."""

    jobs: list[ZabbixJobStatusValue]


class ZabbixBackupGapsOut(BaseModel):
    """3-2-1 gap counts (ADR 0032) in one payload -- a single Zabbix master item.
    Same numbers as the "3-2-1 Gaps" page header; gaps with an exception are
    not counted."""

    unprotected_guests: int = Field(description="PVE guests no enabled backup job covers.")
    offsite_gaps: int = Field(
        description="Backup groups with an offsite gap: copy missing or lagging, same-PBS copy only, target not "
        "connected, or no sync job although the protection policy requires Sync/DR."
    )
    offsite_critical: int = Field(
        description="Of those, how many are critical (copy missing, or lag over RESURGAM's critical threshold)."
    )
    offsite_max_lag_hours: float = Field(
        description="Largest age in hours of a snapshot not copied offsite yet; -1 when no offsite copy is expected."
    )
    excepted: int = Field(description="Gaps accepted with an exception (for reference).")


class ZabbixSetupOut(BaseModel):
    """What the Zabbix monitoring wizard shows: the address Zabbix must use,
    the template, and what Zabbix will discover right now."""

    appliance_url: str = Field(
        description="The address Zabbix must call -- the appliance's configured domain (RESURGAM_DOMAIN). "
        "Any other name or IP is answered with 421 `unknown_host`. Goes into the {$RESURGAM.URL} macro."
    )
    template_name: str = Field(description="The template's name in Zabbix after import.")
    template_version: str = Field(
        description="The template's revision (its vendor version in Zabbix), to tell whether an imported copy is current."
    )
    min_zabbix_version: str = Field(description="The oldest Zabbix version the template imports into.")
    ca_certificate_available: bool = Field(
        description="Whether the appliance's own CA certificate can be downloaded (GET /zabbix/ca-certificate) -- "
        "needed only to switch on TLS verification in Zabbix."
    )
    datastores: int = Field(description="Datastores Zabbix will discover.")
    datastores_with_usage: int = Field(
        description="Of those, how many have a usage measurement yet (the others get values after the next sync)."
    )
    job_types: int = Field(
        description="(PBS instance, task type) pairs Zabbix will discover, without `unknown` (filtered by the template)."
    )
