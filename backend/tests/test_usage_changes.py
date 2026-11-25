from datetime import UTC, date, datetime, timedelta

from resurgam_api.inventory.usage import ForecastStatus, compute_capacity_forecast, compute_usage_changes
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


# --- Capacity forecast ------------------------------------------------------

GB = 1024**3
DAY0 = date(2026, 9, 1)


def _peaks(values: list[int]) -> list[tuple[date, int]]:
    return [(DAY0 + timedelta(days=i), v) for i, v in enumerate(values)]


def test_forecast_needs_a_week_of_history() -> None:
    forecast = compute_capacity_forecast(_peaks([GB * i for i in range(6)]), 100 * GB, NOW)
    assert forecast.status == ForecastStatus.INSUFFICIENT_HISTORY
    assert forecast.days_until_full is None
    assert forecast.growth_bytes_per_day is None


def test_forecast_steady_growth() -> None:
    forecast = compute_capacity_forecast(_peaks([GB * i for i in range(10)]), 50 * GB, NOW)
    assert forecast.status == ForecastStatus.GROWING
    assert forecast.basis_days == 10
    assert forecast.growth_bytes_per_day == GB
    assert forecast.days_until_full == 50.0
    assert forecast.full_at == NOW + timedelta(days=50)


def test_forecast_is_net_of_prune_and_gc() -> None:
    # +10 GB per day, GC frees 50 GB every 7 days: net growth is well below 10 GB/day.
    used, values = 0, []
    for day in range(28):
        used += 10 * GB
        if day % 7 == 6:
            used -= 50 * GB
        values.append(used)
    forecast = compute_capacity_forecast(_peaks(values), 100 * GB, NOW)
    assert forecast.status == ForecastStatus.GROWING
    assert 2 * GB < forecast.growth_bytes_per_day < 4 * GB


def test_forecast_flat_or_shrinking_has_no_full_date() -> None:
    forecast = compute_capacity_forecast(_peaks([100 * GB - GB * i for i in range(10)]), 50 * GB, NOW)
    assert forecast.status == ForecastStatus.NOT_GROWING
    assert forecast.growth_bytes_per_day == -GB
    assert forecast.days_until_full is None
    assert forecast.full_at is None


def test_forecast_gaps_between_days_count_by_date_not_by_index() -> None:
    # Synced only every other day: 2 GB between points is 1 GB per day.
    peaks = [(DAY0 + timedelta(days=2 * i), 2 * GB * i) for i in range(7)]
    forecast = compute_capacity_forecast(peaks, 10 * GB, NOW)
    assert forecast.growth_bytes_per_day == GB
    assert forecast.days_until_full == 10.0
