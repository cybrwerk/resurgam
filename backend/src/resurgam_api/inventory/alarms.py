import uuid
from datetime import UTC, datetime, timedelta
from enum import Enum

from sqlalchemy import func
from sqlalchemy.orm import Session

from resurgam_api.api.v1.datastores import get_datastore_assignment
from resurgam_api.auth.scope import effective_group_customer_id
from resurgam_api.instant_recovery import lifecycle as ir_lifecycle
from resurgam_api.inventory.protection import evaluate_protection_status
from resurgam_api.models.datastore import Datastore
from resurgam_api.models.inventory import BackupGroup, BackupSnapshot, ProtectionPolicy, ProtectionStatus
from resurgam_api.models.pve import PveCluster
from resurgam_api.models.restore import RestorePlan, RestorePlanExecutionStatus, RestorePlanMethod
from resurgam_api.models.tenancy import Customer
from resurgam_api.models.usage import UsageMeasurement
from resurgam_api.schemas.alarm import AlarmOut

# Same thresholds as the frontend's Capacity & Trends page
# (usagePercentTone) — kept in sync deliberately, not derived from a
# shared constant, since there is no shared config layer between the two
# yet.
_USAGE_WARNING_PERCENT = 80.0
_USAGE_CRITICAL_PERCENT = 95.0


class AlarmType(str, Enum):
    """Not from the manifest by name, but the underlying signals are —
    manifest §9.2's Dashboard names "überfällige Backups" (protection
    alarms) and "Datastore-Auslastung" (usage alarms) explicitly.
    Instance-unreachable and task-failure signals are deliberately not
    included here: both are already visible inline (PBS Instances page's
    health badge; task history's own status column), so a duplicate
    alarm would just restate what's already shown, not add a signal.
    """

    PROTECTION_CRITICAL = "protection_critical"
    PROTECTION_WARNING = "protection_warning"
    DATASTORE_USAGE_CRITICAL = "datastore_usage_critical"
    DATASTORE_USAGE_WARNING = "datastore_usage_warning"
    # Instant Recovery (ADR 0030 §6): a VM still runs from its backup after the
    # cluster's `instant_recovery_alarm_hours`; the overlay storage is 80 % full;
    # a failed Instant Recovery left a VM, overlays or mappings behind.
    INSTANT_RECOVERY_RUNNING_LONG = "instant_recovery_running_long"
    INSTANT_RECOVERY_OVERLAY_STORAGE = "instant_recovery_overlay_storage"
    INSTANT_RECOVERY_FAILED = "instant_recovery_failed"
    # Backup encryption (ADR 0031): a group's newest backup uses a different key
    # than the one before it (or switched between encrypted and unencrypted).
    BACKUP_KEY_CHANGED = "backup_key_changed"


class AlarmSeverity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"


def _protection_alarms(db: Session, visible: set[uuid.UUID] | None) -> list[AlarmOut]:
    alarms: list[AlarmOut] = []
    # A superseded generation (a reused VMID's frozen prior identity)
    # keeps whatever protection_policy_id it had before it closed --
    # without this filter it would raise a permanent, never-resolvable
    # "overdue backup" alarm forever.
    groups = (
        db.query(BackupGroup)
        .filter(BackupGroup.protection_policy_id.isnot(None), BackupGroup.superseded_at.is_(None))
        .all()
    )

    for group in groups:
        policy = db.get(ProtectionPolicy, group.protection_policy_id)
        snapshot_count = db.query(BackupSnapshot).filter_by(backup_group_id=group.id).count()
        status = evaluate_protection_status(policy, group, snapshot_count)
        if status not in (ProtectionStatus.CRITICAL, ProtectionStatus.WARNING):
            continue

        assignment = get_datastore_assignment(db, group.datastore_id)
        customer_id = effective_group_customer_id(group, assignment)
        if visible is not None and customer_id not in visible:
            continue

        alarm_type = (
            AlarmType.PROTECTION_CRITICAL if status == ProtectionStatus.CRITICAL else AlarmType.PROTECTION_WARNING
        )
        severity = AlarmSeverity.CRITICAL if status == ProtectionStatus.CRITICAL else AlarmSeverity.WARNING
        label = group.resolved_name or f"{group.backup_type}/{group.backup_id}"
        alarms.append(
            AlarmOut(
                type=alarm_type.value,
                severity=severity.value,
                customer_id=customer_id,
                message=f"{label}: protection status {status.value}",
                entity_type="backup_group",
                entity_id=group.id,
            )
        )
    return alarms


def _usage_alarms(db: Session, visible: set[uuid.UUID] | None) -> list[AlarmOut]:
    alarms: list[AlarmOut] = []
    datastores = db.query(Datastore).all()

    for datastore in datastores:
        assignment = get_datastore_assignment(db, datastore.id)
        customer_id = assignment.customer_id if assignment else None
        if visible is not None and customer_id not in visible:
            continue

        latest = (
            db.query(UsageMeasurement)
            .filter_by(datastore_id=datastore.id)
            .order_by(UsageMeasurement.captured_at.desc())
            .first()
        )
        if latest is None or not latest.total_bytes:
            continue
        percent = latest.used_bytes / latest.total_bytes * 100

        if percent >= _USAGE_CRITICAL_PERCENT:
            alarm_type, severity = AlarmType.DATASTORE_USAGE_CRITICAL, AlarmSeverity.CRITICAL
        elif percent >= _USAGE_WARNING_PERCENT:
            alarm_type, severity = AlarmType.DATASTORE_USAGE_WARNING, AlarmSeverity.WARNING
        else:
            continue

        alarms.append(
            AlarmOut(
                type=alarm_type.value,
                severity=severity.value,
                customer_id=customer_id,
                message=f"{datastore.name}: {round(percent, 1)}% used",
                entity_type="datastore",
                entity_id=datastore.id,
            )
        )
    return alarms


def _instant_recovery_alarms(db: Session, visible: set[uuid.UUID] | None, now: datetime) -> list[AlarmOut]:
    alarms: list[AlarmOut] = []
    plans = (
        db.query(RestorePlan)
        .filter(
            RestorePlan.method == RestorePlanMethod.INSTANT.value,
            RestorePlan.execution_status.in_(
                [*ir_lifecycle.RUNNING_FROM_BACKUP_STATUSES, RestorePlanExecutionStatus.FAILED.value]
            ),
        )
        .all()
    )
    for plan in plans:
        if visible is not None and plan.customer_id not in visible:
            continue
        label = f"{plan.temp_name} (VM {plan.target_vmid})"

        def add(alarm_type: AlarmType, severity: AlarmSeverity, message: str, plan=plan) -> None:
            alarms.append(
                AlarmOut(
                    type=alarm_type.value,
                    severity=severity.value,
                    customer_id=plan.customer_id,
                    message=message,
                    entity_type="restore_plan",
                    entity_id=plan.id,
                )
            )

        if plan.execution_status == RestorePlanExecutionStatus.FAILED.value:
            if plan.ir_vm_created or plan.ir_mapped_devices:
                add(
                    AlarmType.INSTANT_RECOVERY_FAILED,
                    AlarmSeverity.CRITICAL,
                    f"{label}: Instant Recovery failed and left the VM or its backup mapping behind -- "
                    "check it, then Discard.",
                )
            continue

        cluster = db.get(PveCluster, plan.pve_cluster_id)
        hours = cluster.instant_recovery_alarm_hours if cluster else 24
        if plan.ir_running_since is not None and plan.ir_running_since <= now - timedelta(hours=hours):
            running_for = int((now - plan.ir_running_since).total_seconds() // 3600)
            add(
                AlarmType.INSTANT_RECOVERY_RUNNING_LONG,
                AlarmSeverity.WARNING,
                f"{label}: runs from its backup for {running_for} h -- move it to storage or discard it.",
            )
        percent = ir_lifecycle.overlay_storage_percent(plan)
        if percent is not None and percent >= ir_lifecycle.OVERLAY_STORAGE_ALARM_PERCENT:
            add(
                AlarmType.INSTANT_RECOVERY_OVERLAY_STORAGE,
                AlarmSeverity.CRITICAL,
                f"{label}: the overlay storage on {plan.target_node} is {round(percent, 1)}% full -- when it is "
                "full the VM can't write any more. Move it to storage now.",
            )
    return alarms


def _short_fp(fingerprint: str | None) -> str:
    return ":".join(fingerprint.split(":")[:4]) + ":…" if fingerprint else "no key"


def _encryption_alarms(db: Session, visible: set[uuid.UUID] | None) -> list[AlarmOut]:
    """ADR 0031: the newest backup of a group was made with a different key
    (or without one) than the backup before it -- restoring older and newer
    backups then needs different keys. Only snapshots whose crypt mode is
    known (synced since migration 0071) take part."""
    ranked = (
        db.query(
            BackupSnapshot.backup_group_id.label("group_id"),
            BackupSnapshot.crypt_mode.label("crypt_mode"),
            BackupSnapshot.key_fingerprint.label("fingerprint"),
            func.row_number()
            .over(partition_by=BackupSnapshot.backup_group_id, order_by=BackupSnapshot.snapshot_time.desc())
            .label("rank"),
        )
        .join(BackupGroup, BackupGroup.id == BackupSnapshot.backup_group_id)
        .filter(BackupGroup.superseded_at.is_(None), BackupGroup.removed_from_pbs_at.is_(None))
        .subquery()
    )
    rows = db.query(ranked).filter(ranked.c.rank <= 2).all()
    newest: dict[uuid.UUID, dict[int, tuple[str | None, str | None]]] = {}
    for row in rows:
        newest.setdefault(row.group_id, {})[row.rank] = (row.crypt_mode, row.fingerprint)

    alarms: list[AlarmOut] = []
    for group_id, pair in newest.items():
        if 1 not in pair or 2 not in pair:
            continue
        (mode_new, fp_new), (mode_old, fp_old) = pair[1], pair[2]
        if mode_new is None or mode_old is None or fp_new == fp_old:
            continue
        group = db.get(BackupGroup, group_id)
        customer_id = effective_group_customer_id(group, get_datastore_assignment(db, group.datastore_id))
        if visible is not None and customer_id not in visible:
            continue
        label = group.resolved_name or f"{group.backup_type}/{group.backup_id}"
        alarms.append(
            AlarmOut(
                type=AlarmType.BACKUP_KEY_CHANGED.value,
                severity=AlarmSeverity.WARNING.value,
                customer_id=customer_id,
                message=(
                    f"{label}: the newest backup uses {_short_fp(fp_new)}, the one before used {_short_fp(fp_old)} "
                    "-- older and newer backups now need different keys to restore. Keep both keys."
                ),
                entity_type="backup_group",
                entity_id=group.id,
            )
        )
    return alarms


def _attach_customer_names(db: Session, alarms: list[AlarmOut]) -> None:
    """Every alarm carries a bare `customer_id` from the two collectors
    above; this resolves each to a display name + the three-letter
    match_code (the project's existing short reference for a customer,
    manifest-adjacent — see `Customer.match_code`) in one bulk query
    rather than one lookup per alarm.
    """
    customer_ids = {a.customer_id for a in alarms if a.customer_id is not None}
    if not customer_ids:
        return
    customers = {c.id: c for c in db.query(Customer).filter(Customer.id.in_(customer_ids)).all()}
    for alarm in alarms:
        customer = customers.get(alarm.customer_id) if alarm.customer_id else None
        if customer is not None:
            alarm.customer_name = customer.name
            alarm.customer_match_code = customer.match_code


def compute_alarms(db: Session, visible: set[uuid.UUID] | None) -> list[AlarmOut]:
    """Manifest §9.2's Dashboard alarm states: overdue/critical backups
    and datastore capacity — computed fresh on every call from already
    -synced data, never stored (same "computed on read" pattern as
    `evaluate_protection_status`/`compute_usage_changes`). `visible=None`
    means platform_admin (no filtering); otherwise each alarm's
    *effective* customer must be in `visible`, same scope rule as every
    other customer-scoped endpoint.
    """
    alarms = (
        _protection_alarms(db, visible)
        + _usage_alarms(db, visible)
        + _instant_recovery_alarms(db, visible, datetime.now(UTC))
        + _encryption_alarms(db, visible)
    )
    _attach_customer_names(db, alarms)
    return alarms
