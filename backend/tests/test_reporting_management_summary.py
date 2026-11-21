"""Pure unit tests for `compute_management_summary`'s traffic-light rule
and `compute_protection_level`'s tier derivation -- no DB needed, both
functions take already-computed section data directly.
"""

import uuid
from datetime import UTC, date, datetime

from resurgam_api.reporting.recovery_evidence import compute_management_summary, compute_protection_level
from resurgam_api.reporting.timeline_svg import render_period_timeline_svg
from resurgam_api.schemas.reporting import (
    BackupHistoryEntryOut,
    PolicyAdherenceEntryOut,
    RestoreTestEvidenceEntryOut,
    VerificationResultsOut,
    VerificationSummaryOut,
)

_DATE_FROM = date(2026, 1, 1)
_DATE_TO = date(2026, 1, 31)


def _snapshot(group_id=None, verify_status="ok", when=None):
    return BackupHistoryEntryOut(
        backup_group_id=group_id or uuid.uuid4(),
        workload_label="vm-100",
        backup_type="vm",
        namespace="",
        snapshot_time=when or datetime(2026, 1, 15, tzinfo=UTC),
        size_bytes=1000,
        verify_status=verify_status,
    )


def _policy(status="ok"):
    return PolicyAdherenceEntryOut(
        backup_group_id=uuid.uuid4(),
        workload_label="vm-100",
        policy_name="Daily",
        status=status,
        last_backup_at=None,
        last_verify_status="ok",
    )


def _restore_test(group_id=None, member_status="passed"):
    return RestoreTestEvidenceEntryOut(
        application_group_name="App 1",
        backup_group_id=group_id or uuid.uuid4(),
        workload_label="vm-100",
        run_status="completed",
        member_status=member_status,
        tested_at=datetime(2026, 1, 20, tzinfo=UTC),
    )


def _empty_verification(ok=0, failed=0):
    return VerificationResultsOut(summary=VerificationSummaryOut(ok=ok, failed=failed, none=0, unknown=0), entries=[])


def _summary(**overrides):
    kwargs = {
        "date_from": _DATE_FROM,
        "date_to": _DATE_TO,
        "backup_history": [],
        "policy_adherence": [],
        "verification_results": _empty_verification(),
        "failed_jobs": [],
        "restore_test_evidence": [],
        "restore_plans_in_period": [],
        "has_application_groups": False,
    }
    kwargs.update(overrides)
    return compute_management_summary(**kwargs)


def test_traffic_light_is_green_when_nothing_is_wrong():
    result = _summary(backup_history=[_snapshot()], verification_results=_empty_verification(ok=1))
    assert result.traffic_light == "green"


def test_traffic_light_is_red_when_a_policy_is_critical():
    result = _summary(policy_adherence=[_policy(status="critical")])
    assert result.traffic_light == "red"


def test_traffic_light_is_red_when_a_restore_test_failed():
    result = _summary(restore_test_evidence=[_restore_test(member_status="failed")])
    assert result.traffic_light == "red"


def test_traffic_light_is_amber_when_a_policy_is_warning():
    result = _summary(policy_adherence=[_policy(status="warning")])
    assert result.traffic_light == "amber"


def test_traffic_light_is_amber_when_a_verification_failed():
    result = _summary(verification_results=_empty_verification(ok=1, failed=1))
    assert result.traffic_light == "amber"


def test_traffic_light_is_amber_when_no_restore_test_ran_but_application_groups_exist():
    result = _summary(has_application_groups=True, restore_test_evidence=[])
    assert result.traffic_light == "amber"


def test_traffic_light_is_green_when_no_restore_test_ran_and_no_application_groups_exist():
    result = _summary(has_application_groups=False, restore_test_evidence=[])
    assert result.traffic_light == "green"


def test_red_takes_priority_over_amber():
    result = _summary(policy_adherence=[_policy(status="critical"), _policy(status="warning")])
    assert result.traffic_light == "red"


def test_restore_test_pass_rate_is_none_when_no_restore_tests_ran():
    result = _summary(restore_test_evidence=[])
    assert result.restore_test_pass_rate is None


def test_restore_test_pass_rate_reflects_pass_fraction():
    result = _summary(
        restore_test_evidence=[_restore_test(member_status="passed"), _restore_test(member_status="failed")]
    )
    assert result.restore_test_pass_rate == 0.5


def test_unresolved_failed_jobs_counts_distinct_identities_once():
    from resurgam_api.schemas.reporting import FailedJobEntryOut

    entries = [
        FailedJobEntryOut(
            source="file_index_job",
            id=uuid.uuid4(),
            label="File index (manual)",
            status="failed",
            occurred_at=datetime(2026, 1, 5, tzinfo=UTC),
        ),
        FailedJobEntryOut(
            source="file_index_job",
            id=uuid.uuid4(),
            label="File index (manual)",
            status="failed",
            occurred_at=datetime(2026, 1, 10, tzinfo=UTC),
        ),
        FailedJobEntryOut(
            source="file_restore_job",
            id=uuid.uuid4(),
            label="File restore (files)",
            status="failed",
            occurred_at=datetime(2026, 1, 8, tzinfo=UTC),
        ),
    ]
    result = _summary(failed_jobs=entries)
    assert result.unresolved_failed_jobs == 2  # two distinct (source, label) identities
    assert result.traffic_light == "red"


def test_period_timeline_svg_renders_without_crashing_on_empty_data():
    svg = render_period_timeline_svg(_DATE_FROM, _DATE_TO, [], [])
    assert svg.startswith("<svg")
    assert svg.endswith("</svg>")


def test_period_timeline_svg_renders_with_data():
    svg = render_period_timeline_svg(_DATE_FROM, _DATE_TO, [_snapshot()], [_restore_test()])
    assert "<circle" in svg
    assert "<polygon" in svg


def test_protection_level_tier_backup_only():
    group_id = uuid.uuid4()
    groups = [_FakeGroup(group_id, "vm-100")]
    result = compute_protection_level(groups, [_snapshot(group_id=group_id, verify_status="failed")], [], [])
    assert result.tier == "backup_only"
    assert result.workloads[0].tier == "backup_only"


def test_protection_level_tier_backup_verified():
    group_id = uuid.uuid4()
    groups = [_FakeGroup(group_id, "vm-100")]
    result = compute_protection_level(groups, [_snapshot(group_id=group_id, verify_status="ok")], [], [])
    assert result.tier == "backup_verified"


def test_protection_level_tier_restore_tested():
    group_id = uuid.uuid4()
    groups = [_FakeGroup(group_id, "vm-100")]
    restore_plan = _FakeRestorePlan(group_id, "completed")
    result = compute_protection_level(groups, [_snapshot(group_id=group_id, verify_status="ok")], [restore_plan], [])
    assert result.tier == "backup_verified_restore_tested"


def test_protection_level_tier_lab_tested():
    group_id = uuid.uuid4()
    groups = [_FakeGroup(group_id, "vm-100")]
    result = compute_protection_level(
        groups,
        [_snapshot(group_id=group_id, verify_status="ok")],
        [],
        [_restore_test(group_id=group_id, member_status="passed")],
    )
    assert result.tier == "backup_verified_lab_tested"


def test_protection_level_customer_tier_is_the_minimum_not_the_average_or_best():
    tested_id, untested_id = uuid.uuid4(), uuid.uuid4()
    groups = [_FakeGroup(tested_id, "vm-100"), _FakeGroup(untested_id, "vm-200")]
    history = [
        _snapshot(group_id=tested_id, verify_status="ok"),
        _snapshot(group_id=untested_id, verify_status="failed"),
    ]
    result = compute_protection_level(groups, history, [], [_restore_test(group_id=tested_id, member_status="passed")])
    tiers = {w.workload_label: w.tier for w in result.workloads}
    assert tiers["vm-100"] == "backup_verified_lab_tested"
    assert tiers["vm-200"] == "backup_only"
    assert result.tier == "backup_only"  # the minimum, not the best or an average


def test_protection_level_defaults_to_backup_only_when_no_workloads_have_data():
    result = compute_protection_level([_FakeGroup(uuid.uuid4(), "vm-100")], [], [], [])
    assert result.tier == "backup_only"
    assert result.workloads == []


class _FakeGroup:
    def __init__(self, group_id, backup_id):
        self.id = group_id
        self.resolved_name = None
        self.backup_id = backup_id


class _FakeRestorePlan:
    def __init__(self, backup_group_id, execution_status):
        self.backup_group_id = backup_group_id
        self.execution_status = execution_status
        self.approval_status = None
