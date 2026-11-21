from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


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


class DatastoreUsageOut(BaseModel):
    latest: UsageMeasurementOut | None
    history: list[UsageMeasurementOut]
    daily_change_bytes: int | None
    monthly_change_bytes: int | None
