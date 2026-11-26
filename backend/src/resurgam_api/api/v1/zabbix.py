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

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from resurgam_api import monitoring
from resurgam_api.api.v1.backup_gaps import build_backup_gaps
from resurgam_api.auth.dependency import require_role
from resurgam_api.config import Settings, get_settings
from resurgam_api.db import get_db
from resurgam_api.errors import ResurgamApiError
from resurgam_api.inventory.usage import compute_capacity_forecast, load_daily_peaks
from resurgam_api.lab_setup.pairing import read_ca_certificate
from resurgam_api.models.datastore import Datastore
from resurgam_api.models.identity import RoleKey
from resurgam_api.models.inventory import TaskRun
from resurgam_api.models.pbs import PbsInstance
from resurgam_api.models.usage import UsageMeasurement
from resurgam_api.schemas.zabbix import (
    ZabbixBackupGapsOut,
    ZabbixDatastoreDiscoveryEntry,
    ZabbixDatastoreDiscoveryOut,
    ZabbixDatastoreUsageOut,
    ZabbixDatastoreUsageValue,
    ZabbixJobDiscoveryEntry,
    ZabbixJobDiscoveryOut,
    ZabbixJobStatusOut,
    ZabbixJobStatusValue,
    ZabbixSetupOut,
)

router = APIRouter(prefix="/zabbix", tags=["zabbix"])
_require_platform_admin = require_role(RoleKey.PLATFORM_ADMIN)

# Tasks PBS reports that RESURGAM cannot classify; the template's discovery
# filter ({$RESURGAM.JOB.TYPE.NOT_MATCHES}) leaves them out by default.
_UNMONITORED_TASK_TYPE = "unknown"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _appliance_url(settings: Settings) -> str:
    return settings.frontend_origin.rstrip("/")


@router.get("/setup", response_model=ZabbixSetupOut)
def zabbix_setup(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    _user=Depends(_require_platform_admin),
) -> ZabbixSetupOut:
    """For the Zabbix monitoring wizard (Administration -> Zabbix monitoring):
    the address Zabbix has to use, the template, and how much Zabbix will
    discover right now."""
    datastore_ids = [row[0] for row in db.query(Datastore.id).all()]
    with_usage = (
        db.query(UsageMeasurement.datastore_id)
        .filter(UsageMeasurement.datastore_id.in_(datastore_ids))
        .distinct()
        .count()
        if datastore_ids
        else 0
    )
    instances = {row[0] for row in db.query(PbsInstance.id).all()}
    job_types = [
        pair for pair in _distinct_task_type_pairs(db) if pair[0] in instances and pair[1] != _UNMONITORED_TASK_TYPE
    ]
    return ZabbixSetupOut(
        appliance_url=_appliance_url(settings),
        template_name=monitoring.TEMPLATE_NAME,
        template_version=monitoring.TEMPLATE_REVISION,
        min_zabbix_version=monitoring.MIN_ZABBIX_VERSION,
        ca_certificate_available=read_ca_certificate(settings.appliance_tls_ca_file) is not None,
        datastores=len(datastore_ids),
        datastores_with_usage=with_usage,
        job_types=len(job_types),
    )


@router.get(
    "/template",
    response_class=Response,
    responses={
        200: {
            "content": {"application/yaml": {}},
            "description": "The Zabbix 7.0 template (YAML) with {$RESURGAM.URL} preset to this appliance's address.",
        }
    },
)
def zabbix_template(settings: Settings = Depends(get_settings), _user=Depends(_require_platform_admin)) -> Response:
    """The "RESURGAM by HTTP" template to import in Zabbix (Data collection
    -> Templates -> Import). Its {$RESURGAM.URL} macro already holds this
    appliance's address; only {$RESURGAM.TOKEN} is left to set on the host."""
    return Response(
        content=monitoring.render_template(_appliance_url(settings)),
        media_type="application/yaml",
        headers={"Content-Disposition": f'attachment; filename="{monitoring.TEMPLATE_FILENAME}"'},
    )


@router.get(
    "/ca-certificate",
    response_class=Response,
    responses={
        200: {"content": {"application/x-pem-file": {}}, "description": "The appliance's root CA certificate (PEM)."},
        404: {"description": "`ca_certificate_unavailable`: the appliance uses a certificate from a public CA."},
    },
)
def zabbix_ca_certificate(
    settings: Settings = Depends(get_settings), _user=Depends(_require_platform_admin)
) -> Response:
    """The root certificate of the appliance's own CA (Caddy `tls internal`).
    Only needed to switch on TLS verification in Zabbix: add it to the Zabbix
    server's CA store, then set "SSL verify peer" on the template's HTTP items."""
    pem = read_ca_certificate(settings.appliance_tls_ca_file)
    if pem is None:
        raise ResurgamApiError(
            code="ca_certificate_unavailable",
            message="This appliance has no own CA certificate to download -- its certificate comes from a public CA.",
            status_code=404,
        )
    return Response(
        content=pem,
        media_type="application/x-pem-file",
        headers={"Content-Disposition": 'attachment; filename="resurgam-appliance-ca.crt"'},
    )


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
    now = datetime.now(UTC)
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
        forecast = compute_capacity_forecast(load_daily_peaks(db, datastore.id, now), latest.available_bytes, now)
        values.append(
            ZabbixDatastoreUsageValue(
                datastore_id=str(datastore.id),
                used_bytes=latest.used_bytes,
                available_bytes=latest.available_bytes,
                total_bytes=latest.total_bytes,
                usage_percent=round(usage_percent, 2),
                captured_at=latest.captured_at,
                age_seconds=max(0.0, (now - _aware(latest.captured_at)).total_seconds()),
                growth_bytes_per_day=forecast.growth_bytes_per_day,
                days_until_full=forecast.days_until_full if forecast.days_until_full is not None else -1.0,
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


@router.get("/backup-gaps", response_model=ZabbixBackupGapsOut)
def zabbix_backup_gaps(db: Session = Depends(get_db), _user=Depends(_require_platform_admin)) -> ZabbixBackupGapsOut:
    """3-2-1 gap counts for the template's "3-2-1 gaps" items (ADR 0032), from
    the same computation as GET /backup-gaps."""
    summary = build_backup_gaps(db, None).summary
    return ZabbixBackupGapsOut(
        unprotected_guests=summary.unprotected_guests,
        offsite_gaps=summary.offsite_gaps,
        offsite_critical=summary.offsite_critical,
        offsite_max_lag_hours=summary.offsite_max_lag_hours if summary.offsite_max_lag_hours is not None else -1,
        excepted=summary.excepted,
    )
