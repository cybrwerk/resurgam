from datetime import UTC, datetime, timedelta

from resurgam_api.models.inventory import BackupGroup, ProtectionPolicy, ProtectionStatus, VerifyStatus


def evaluate_protection_status(
    policy: ProtectionPolicy | None,
    group: BackupGroup,
    snapshot_count: int,
    *,
    now: datetime | None = None,
) -> ProtectionStatus:
    """Manifest §9.4/§9.5: OK/Warning/Critical/Unknown for one backup group.

    No policy assigned means there is nothing to evaluate against, so the
    status is UNKNOWN rather than a default pass — an unmonitored system
    must not look identical to a healthy one. `sync_dr_required` on the
    policy is not evaluated here: it decides whether a group without any
    sync job is an offsite gap (`inventory/offsite.py`, ADR 0032).
    """
    if policy is None:
        return ProtectionStatus.UNKNOWN

    if group.last_backup_at is None:
        return ProtectionStatus.CRITICAL

    reference_time = now if now is not None else datetime.now(UTC)
    age = reference_time - group.last_backup_at

    if age > timedelta(hours=policy.max_time_since_last_backup_hours):
        return ProtectionStatus.CRITICAL

    if group.last_verify_status == VerifyStatus.FAILED.value:
        return ProtectionStatus.CRITICAL

    if age > timedelta(hours=policy.expected_interval_hours):
        return ProtectionStatus.WARNING

    if snapshot_count < policy.min_snapshot_count:
        return ProtectionStatus.WARNING

    if policy.expected_verify_deadline_hours is not None:
        verify_pending = group.last_verify_status in (VerifyStatus.NONE.value, VerifyStatus.UNKNOWN.value)
        if verify_pending and age > timedelta(hours=policy.expected_verify_deadline_hours):
            return ProtectionStatus.WARNING

    return ProtectionStatus.OK
