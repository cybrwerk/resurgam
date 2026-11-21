from datetime import UTC, datetime, timedelta

from resurgam_api.inventory.usage import compute_usage_changes
from resurgam_api.models.usage import UsageMeasurement

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def _measurement(captured_at: datetime, used_bytes: int) -> UsageMeasurement:
    return UsageMeasurement(captured_at=captured_at, used_bytes=used_bytes, available_bytes=0, total_bytes=100)


def test_empty_history_returns_no_changes() -> None:
    assert compute_usage_changes([], NOW) == (None, None)


def test_single_measurement_returns_no_changes() -> None:
    history = [_measurement(NOW, 1000)]
    assert compute_usage_changes(history, NOW) == (None, None)


def test_daily_change_computed_from_closest_measurement_a_day_ago() -> None:
    history = [
        _measurement(NOW - timedelta(hours=25), 800),
        _measurement(NOW - timedelta(hours=23), 900),  # too recent for the daily baseline
        _measurement(NOW, 1000),
    ]
    daily, monthly = compute_usage_changes(history, NOW)
    assert daily == 200  # 1000 - 800, the 25h-old point, not the 23h-old one
    assert monthly is None


def test_monthly_change_computed_when_history_reaches_back_far_enough() -> None:
    history = [
        _measurement(NOW - timedelta(days=31), 500),
        _measurement(NOW - timedelta(hours=25), 800),
        _measurement(NOW, 1000),
    ]
    daily, monthly = compute_usage_changes(history, NOW)
    assert daily == 200
    assert monthly == 500


def test_negative_change_when_usage_shrank() -> None:
    history = [_measurement(NOW - timedelta(hours=25), 1000), _measurement(NOW, 700)]
    daily, _ = compute_usage_changes(history, NOW)
    assert daily == -300
