from datetime import UTC, datetime, timedelta

from resurgam_api.inventory.protection import evaluate_protection_status
from resurgam_api.models.inventory import BackupGroup, ProtectionPolicy, ProtectionStatus, VerifyStatus

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _policy(**overrides) -> ProtectionPolicy:
    defaults = {
        "expected_interval_hours": 24,
        "max_time_since_last_backup_hours": 48,
        "expected_verify_deadline_hours": None,
        "min_snapshot_count": 1,
        "sync_dr_required": False,
    }
    defaults.update(overrides)
    return ProtectionPolicy(**defaults)


def _group(**overrides) -> BackupGroup:
    defaults = {"last_backup_at": NOW - timedelta(hours=1), "last_verify_status": VerifyStatus.OK.value}
    defaults.update(overrides)
    return BackupGroup(**defaults)


def test_no_policy_is_unknown() -> None:
    group = _group()
    assert evaluate_protection_status(None, group, snapshot_count=5, now=NOW) == ProtectionStatus.UNKNOWN


def test_never_backed_up_is_critical() -> None:
    group = _group(last_backup_at=None)
    assert evaluate_protection_status(_policy(), group, snapshot_count=0, now=NOW) == ProtectionStatus.CRITICAL


def test_within_interval_and_verified_is_ok() -> None:
    group = _group(last_backup_at=NOW - timedelta(hours=2))
    status = evaluate_protection_status(_policy(), group, snapshot_count=3, now=NOW)
    assert status == ProtectionStatus.OK


def test_older_than_max_time_is_critical() -> None:
    group = _group(last_backup_at=NOW - timedelta(hours=49))
    status = evaluate_protection_status(_policy(), group, snapshot_count=3, now=NOW)
    assert status == ProtectionStatus.CRITICAL


def test_older_than_interval_but_within_max_time_is_warning() -> None:
    group = _group(last_backup_at=NOW - timedelta(hours=30))
    status = evaluate_protection_status(_policy(), group, snapshot_count=3, now=NOW)
    assert status == ProtectionStatus.WARNING


def test_failed_verify_is_critical_even_within_interval() -> None:
    group = _group(last_backup_at=NOW - timedelta(hours=1), last_verify_status=VerifyStatus.FAILED.value)
    status = evaluate_protection_status(_policy(), group, snapshot_count=3, now=NOW)
    assert status == ProtectionStatus.CRITICAL


def test_below_min_snapshot_count_is_warning() -> None:
    group = _group(last_backup_at=NOW - timedelta(hours=1))
    status = evaluate_protection_status(_policy(min_snapshot_count=5), group, snapshot_count=1, now=NOW)
    assert status == ProtectionStatus.WARNING


def test_verify_pending_past_deadline_is_warning() -> None:
    group = _group(last_backup_at=NOW - timedelta(hours=5), last_verify_status=VerifyStatus.NONE.value)
    policy = _policy(expected_verify_deadline_hours=2)
    status = evaluate_protection_status(policy, group, snapshot_count=3, now=NOW)
    assert status == ProtectionStatus.WARNING


def test_verify_pending_within_deadline_is_ok() -> None:
    group = _group(last_backup_at=NOW - timedelta(hours=1), last_verify_status=VerifyStatus.NONE.value)
    policy = _policy(expected_verify_deadline_hours=2)
    status = evaluate_protection_status(policy, group, snapshot_count=3, now=NOW)
    assert status == ProtectionStatus.OK
