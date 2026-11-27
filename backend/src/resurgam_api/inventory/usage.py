import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum

from sqlalchemy import func
from sqlalchemy.orm import Session

from resurgam_api.models.usage import UsageMeasurement

_DAY = timedelta(hours=24)
_MONTH = timedelta(days=30)


def _closest_before(history: list[UsageMeasurement], cutoff: datetime) -> UsageMeasurement | None:
    """The most recent measurement at or before `cutoff`, or None if the
    history doesn't reach back that far yet. `history` must be sorted
    ascending by `captured_at`.
    """
    candidate: UsageMeasurement | None = None
    for measurement in history:
        if measurement.captured_at <= cutoff:
            candidate = measurement
        else:
            break
    return candidate


def compute_usage_changes(
    history: list[UsageMeasurement], now: datetime, attr: str = "used_bytes"
) -> tuple[int | None, int | None]:
    """Manifest §9.3's "tägliche und monatliche Veränderung" in used bytes.

    Computed from RESURGAM's own stored measurements rather than PBS's own
    history array (which also exists — see `pbs_api.list_datastore_usage`
    — but parsing it would mean two different sources of truth for the
    same number, and sync being on-demand-only, ADR 0006, already means
    this reads exactly as sparse as everything else the Collector tracks).
    Returns (None, None) for either figure once the history doesn't yet
    reach back that far — not zero, which would misleadingly claim "no
    change" instead of "not enough history yet".

    `history` must be sorted ascending by `captured_at` and non-empty for
    a change to be computable; the latest entry is always `history[-1]`.

    `attr` picks the figure: `used_bytes`, or `s3_data_bytes` for an S3
    datastore (ADR 0033) -- rows where it is null (no GC yet) are skipped.
    """
    history = [m for m in history if getattr(m, attr) is not None]
    if not history:
        return None, None

    latest = history[-1]
    daily_baseline = _closest_before(history[:-1], now - _DAY)
    monthly_baseline = _closest_before(history[:-1], now - _MONTH)

    value = getattr(latest, attr)
    daily_change = value - getattr(daily_baseline, attr) if daily_baseline is not None else None
    monthly_change = value - getattr(monthly_baseline, attr) if monthly_baseline is not None else None
    return daily_change, monthly_change


# --- S3 traffic (ADR 0033) ----------------------------------------------------

S3_COUNTERS = (
    "s3_get_requests",
    "s3_put_requests",
    "s3_post_requests",
    "s3_head_requests",
    "s3_delete_requests",
    "s3_uploaded_bytes",
    "s3_downloaded_bytes",
)


def compute_s3_traffic(history: list[UsageMeasurement], since: datetime) -> dict[str, int] | None:
    """S3 requests and bytes per counter between `since` and the latest
    measurement, summed from consecutive measurements. PBS's counters run
    since the datastore's last counter reset (its optional threshold reset
    schedule), so a counter lower than in the row before means a reset: that
    step then counts the new value, as everything since the reset is new.
    Traffic between the last row before a reset and the reset itself is lost
    -- the figure is a lower bound. None without two measurements carrying
    counters in the window. `history` must be sorted ascending."""
    rows = [m for m in history if m.s3_put_requests is not None]
    start = None
    for index, row in enumerate(rows):
        if row.captured_at >= since:
            # The row just before the window is the baseline, if there is one.
            start = max(index - 1, 0)
            break
    if start is None or len(rows) - start < 2:
        return None
    totals = dict.fromkeys(S3_COUNTERS, 0)
    for previous, current in zip(rows[start:], rows[start + 1 :], strict=False):
        for counter in S3_COUNTERS:
            before, after = getattr(previous, counter) or 0, getattr(current, counter) or 0
            totals[counter] += after - before if after >= before else after
    return totals


# --- Capacity forecast ------------------------------------------------------

FORECAST_WINDOW_DAYS = 30
# Fewer than this many days of history and a straight line through the points
# says more about one big initial backup than about real growth.
FORECAST_MIN_DAYS = 7
FORECAST_CRITICAL_DAYS = 14
FORECAST_WARNING_DAYS = 30


class ForecastStatus(str, Enum):
    INSUFFICIENT_HISTORY = "insufficient_history"
    GROWING = "growing"
    NOT_GROWING = "not_growing"
    # ADR 0033: an S3 datastore. Its used/avail is the local cache
    # filesystem, and a bucket has no fixed size to run out of.
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class CapacityForecast:
    status: ForecastStatus
    basis_days: int
    growth_bytes_per_day: int | None
    days_until_full: float | None
    full_at: datetime | None


NOT_APPLICABLE_FORECAST = CapacityForecast(ForecastStatus.NOT_APPLICABLE, 0, None, None, None)


def load_daily_peaks(db: Session, datastore_id: uuid.UUID, now: datetime) -> list[tuple[date, int]]:
    """Highest `used_bytes` per UTC day over the last `FORECAST_WINDOW_DAYS`
    days, oldest first. One point per day, so a sync cadence of 15 minutes
    and one of 6 hours weigh the same, and the dip a garbage collection
    causes inside a day does not pull the line down.
    """
    day = func.date_trunc("day", func.timezone("UTC", UsageMeasurement.captured_at))
    rows = (
        db.query(day.label("day"), func.max(UsageMeasurement.used_bytes))
        .filter(
            UsageMeasurement.datastore_id == datastore_id,
            UsageMeasurement.captured_at >= now - timedelta(days=FORECAST_WINDOW_DAYS),
        )
        .group_by(day)
        .order_by(day)
        .all()
    )
    return [(row[0].date(), int(row[1])) for row in rows]


def compute_capacity_forecast(
    daily_peaks: list[tuple[date, int]], available_bytes: int | None, now: datetime
) -> CapacityForecast:
    """When will the datastore be full if it keeps growing like the last 30
    days? A least-squares line through the daily peaks. Prune and garbage
    collection free space inside that window, so the slope is the *net*
    growth -- what is written minus what retention frees -- not just the
    rate new backups arrive at.

    `available_bytes` is PBS's own "avail" from the latest measurement
    (what can still be written), not `total - used`: a filesystem's
    reserved blocks would make the latter optimistic.
    """
    if len(daily_peaks) < 2:
        return CapacityForecast(ForecastStatus.INSUFFICIENT_HISTORY, len(daily_peaks), None, None, None)
    first_day = daily_peaks[0][0]
    basis_days = (daily_peaks[-1][0] - first_day).days + 1
    if len(daily_peaks) < FORECAST_MIN_DAYS or basis_days < FORECAST_MIN_DAYS:
        return CapacityForecast(ForecastStatus.INSUFFICIENT_HISTORY, basis_days, None, None, None)

    xs = [float((d - first_day).days) for d, _ in daily_peaks]
    ys = [float(used) for _, used in daily_peaks]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    spread = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / spread

    growth = round(slope)
    if growth <= 0 or available_bytes is None:
        return CapacityForecast(ForecastStatus.NOT_GROWING, basis_days, growth, None, None)
    days = max(available_bytes, 0) / slope
    return CapacityForecast(ForecastStatus.GROWING, basis_days, growth, round(days, 1), now + timedelta(days=days))
