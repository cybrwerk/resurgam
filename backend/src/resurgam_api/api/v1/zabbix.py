"""Manifest §16 Observability: "Zabbix-kompatible Checks" -- raised
directly by the user ("ex zabbix could get the Jobs States from there
and do LLD with the values, and also the capacity and usage of
datastores"). Read-only, platform-admin-scoped (same access level
`pbs_instances.list_pbs_instances` already uses -- PBS instances/
datastores are global infra, not customer-owned data).

Auth: a Zabbix HTTP agent item sends a static `Authorization: Bearer`
header with a personal API token (docs/adr/0029) -- read-only, carrying
`platform_admin` globally, ideally limited to the Zabbix server's address.
Session tokens would expire after 15 minutes.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from resurgam_api.auth.dependency import require_role
from resurgam_api.db import get_db
from resurgam_api.models.datastore import Datastore
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.inventory import TaskRun
from resurgam_api.models.pbs import PbsInstance
from resurgam_api.models.usage import UsageMeasurement
from resurgam_api.schemas.zabbix import (
    ZabbixDatastoreDiscoveryEntry,
    ZabbixDatastoreDiscoveryOut,
    ZabbixDatastoreUsageOut,
    ZabbixDatastoreUsageValue,
    ZabbixJobDiscoveryEntry,
    ZabbixJobDiscoveryOut,
    ZabbixJobStatusOut,
    ZabbixJobStatusValue,
)

router = APIRouter(prefix="/zabbix", tags=["zabbix"])
_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)


@router.get("/discovery/datastores", response_model=ZabbixDatastoreDiscoveryOut)
def zabbix_discover_datastores(
    db: Session = Depends(get_db), _user=Depends(_require_platform_admin)
) -> ZabbixDatastoreDiscoveryOut:
    """A Zabbix Low-Level Discovery rule: point a discovery rule's HTTP
    agent item at this URL to auto-create a usage item prototype set per
    real RESURGAM datastore, added/removed automatically as datastores
    are imported/removed.
    """
    rows = (
        db.query(Datastore, PbsInstance)
        .join(PbsInstance, Datastore.pbs_instance_id == PbsInstance.id)
        .order_by(Datastore.name)
        .all()
    )
    return ZabbixDatastoreDiscoveryOut(
        data=[
            ZabbixDatastoreDiscoveryEntry(
                datastore_id=str(datastore.id),
                datastore_name=datastore.name,
                pbs_instance_id=str(instance.id),
                pbs_instance_name=instance.display_name,
            )
            for datastore, instance in rows
        ]
    )


@router.get("/datastores/usage", response_model=ZabbixDatastoreUsageOut)
def zabbix_datastore_usage(
    db: Session = Depends(get_db), _user=Depends(_require_platform_admin)
) -> ZabbixDatastoreUsageOut:
    """Every datastore's latest known capacity/usage, in one payload --
    intended as a single Zabbix master item, with per-datastore dependent
    items (keyed by {#DATASTORE_ID} from the discovery rule above)
    extracting one value each via JSONPath preprocessing.
    """
    values: list[ZabbixDatastoreUsageValue] = []
    for datastore in db.query(Datastore).all():
        latest = (
            db.query(UsageMeasurement)
            .filter_by(datastore_id=datastore.id)
            .order_by(UsageMeasurement.captured_at.desc())
            .first()
        )
        if latest is None:
            continue
        usage_percent = (latest.used_bytes / latest.total_bytes * 100) if latest.total_bytes else 0.0
        values.append(
            ZabbixDatastoreUsageValue(
                datastore_id=str(datastore.id),
                used_bytes=latest.used_bytes,
                available_bytes=latest.available_bytes,
                total_bytes=latest.total_bytes,
                usage_percent=round(usage_percent, 2),
                captured_at=latest.captured_at,
            )
        )
    return ZabbixDatastoreUsageOut(datastores=values)


def _distinct_task_type_pairs(db: Session) -> list[tuple[uuid.UUID, str]]:
    return db.query(TaskRun.pbs_instance_id, TaskRun.task_type).distinct().all()


@router.get("/discovery/jobs", response_model=ZabbixJobDiscoveryOut)
def zabbix_discover_jobs(
    db: Session = Depends(get_db), _user=Depends(_require_platform_admin)
) -> ZabbixJobDiscoveryOut:
    """A Zabbix Low-Level Discovery rule for job states: one entry per
    (PBS instance, task type) pair with at least one recorded task --
    e.g. "backup on pbs-main", "prune on site-b".
    """
    instances = {instance.id: instance for instance in db.query(PbsInstance).all()}
    entries = [
        ZabbixJobDiscoveryEntry(
            pbs_instance_id=str(instance_id), pbs_instance_name=instances[instance_id].display_name, task_type=task_type
        )
        for instance_id, task_type in _distinct_task_type_pairs(db)
        if instance_id in instances
    ]
    entries.sort(key=lambda entry: (entry.pbs_instance_name, entry.task_type))
    return ZabbixJobDiscoveryOut(data=entries)


@router.get("/jobs/status", response_model=ZabbixJobStatusOut)
def zabbix_job_status(db: Session = Depends(get_db), _user=Depends(_require_platform_admin)) -> ZabbixJobStatusOut:
    """Every discovered (instance, task type) pair's most recent task, in
    one payload -- a single Zabbix master item, same pairing as
    `/zabbix/datastores/usage` for capacity.
    """
    now = datetime.now(UTC)
    values: list[ZabbixJobStatusValue] = []
    for instance_id, task_type in _distinct_task_type_pairs(db):
        latest = (
            db.query(TaskRun)
            .filter_by(pbs_instance_id=instance_id, task_type=task_type)
            .order_by(TaskRun.started_at.desc())
            .first()
        )
        if latest is None:
            continue
        values.append(
            ZabbixJobStatusValue(
                pbs_instance_id=str(instance_id),
                task_type=task_type,
                status=latest.status,
                last_started_at=latest.started_at,
                last_ended_at=latest.ended_at,
                age_seconds=(now - latest.started_at).total_seconds(),
            )
        )
    values.sort(key=lambda value: (value.pbs_instance_id, value.task_type))
    return ZabbixJobStatusOut(jobs=values)
