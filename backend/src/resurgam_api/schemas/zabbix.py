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


class ZabbixDatastoreDiscoveryOut(BaseModel):
    """Zabbix's own required LLD envelope: a top-level `data` array."""

    data: list[ZabbixDatastoreDiscoveryEntry]


class ZabbixDatastoreUsageValue(BaseModel):
    datastore_id: str = Field(description="Matches {#DATASTORE_ID} from GET /zabbix/discovery/datastores.")
    used_bytes: int
    available_bytes: int
    total_bytes: int
    usage_percent: float = Field(
        description="used_bytes / total_bytes * 100, rounded to 2 decimals; 0 if total_bytes is 0."
    )
    captured_at: datetime = Field(description="When the Collector last measured this datastore's usage.")


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

    pbs_instance_id: str = Field(alias="{#PBS_INSTANCE_ID}")
    pbs_instance_name: str = Field(alias="{#PBS_INSTANCE_NAME}")
    task_type: str = Field(
        alias="{#TASK_TYPE}",
        description="backup | verify | prune | garbage_collection | sync | unknown (this app's own normalized TaskType vocabulary).",
    )


class ZabbixJobDiscoveryOut(BaseModel):
    data: list[ZabbixJobDiscoveryEntry]


class ZabbixJobStatusValue(BaseModel):
    pbs_instance_id: str = Field(description="Matches {#PBS_INSTANCE_ID} from GET /zabbix/discovery/jobs.")
    task_type: str = Field(description="Matches {#TASK_TYPE} from GET /zabbix/discovery/jobs.")
    status: str = Field(description="The most recent task's status: ok | error | running | unknown.")
    last_started_at: datetime
    last_ended_at: datetime | None = Field(description="Null while the most recent task is still running.")
    age_seconds: float = Field(
        description="Seconds since the most recent task of this type started on this instance -- "
        "the basis for a Zabbix trigger like 'no backup task on this PBS instance in 26h'.",
    )


class ZabbixJobStatusOut(BaseModel):
    """One flat payload covering every discovered (instance, task type)
    pair's most recent task -- meant as a single Zabbix master item."""

    jobs: list[ZabbixJobStatusValue]
