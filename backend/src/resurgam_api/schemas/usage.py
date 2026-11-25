from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class UsageMeasurementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    datastore_id: UUID
    captured_at: datetime
    used_bytes: int
    available_bytes: int
    total_bytes: int
    logical_bytes: int
    usage_percent: float
    group_count_vm: int
    group_count_ct: int
    group_count_host: int
    snapshot_count: int
    oldest_snapshot_at: datetime | None
    newest_snapshot_at: datetime | None
    last_successful_gc_at: datetime | None


class CapacityForecastOut(BaseModel):
    status: str = Field(
        description=(
            "`growing` (used space grows; `days_until_full` is set), `not_growing` (flat or shrinking over the "
            "window -- no full date), or `insufficient_history` (fewer than 7 days of measurements yet)."
        )
    )
    basis_days: int = Field(description="How many days of measurements the forecast is based on (at most 30).")
    growth_bytes_per_day: int | None = Field(
        description=(
            "Net growth of used space per day: a least-squares line through each day's highest `used_bytes` "
            "over the last 30 days. Already net of what prune and garbage collection freed in that window. "
            "Negative when the datastore shrinks; null with `insufficient_history`."
        )
    )
    days_until_full: float | None = Field(
        description=(
            "Days until `available_bytes` of the latest measurement is used up at `growth_bytes_per_day`. "
            "Null unless `status` is `growing`. A warning alarm is raised at <= 30 days, a critical one at <= 14."
        )
    )
    full_at: datetime | None = Field(description="Point in time `days_until_full` lands on (UTC); null like it.")


class DatastoreUsageOut(BaseModel):
    latest: UsageMeasurementOut | None = Field(description="The newest measurement, or null before the first sync.")
    history: list[UsageMeasurementOut] = Field(
        description="The newest 500 measurements (one per Collector sync), oldest first."
    )
    daily_change_bytes: int | None = Field(
        description="Change of `used_bytes` against the measurement closest to 24 h ago; null until one exists."
    )
    monthly_change_bytes: int | None = Field(
        description="Change of `used_bytes` against the measurement closest to 30 days ago; null until one exists."
    )
    forecast: CapacityForecastOut = Field(description="When the datastore will be full at its current net growth.")
