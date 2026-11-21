from datetime import datetime, timedelta

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


def compute_usage_changes(history: list[UsageMeasurement], now: datetime) -> tuple[int | None, int | None]:
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
    """
    if not history:
        return None, None

    latest = history[-1]
    daily_baseline = _closest_before(history[:-1], now - _DAY)
    monthly_baseline = _closest_before(history[:-1], now - _MONTH)

    daily_change = latest.used_bytes - daily_baseline.used_bytes if daily_baseline is not None else None
    monthly_change = latest.used_bytes - monthly_baseline.used_bytes if monthly_baseline is not None else None
    return daily_change, monthly_change
