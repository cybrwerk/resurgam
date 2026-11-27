from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class UsageMeasurementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Id of this measurement (one is stored per Collector sync, never changed).")
    datastore_id: UUID = Field(description="The datastore measured.")
    captured_at: datetime = Field(description="When the Collector took the measurement (UTC).")
    used_bytes: int = Field(
        description="Bytes in use as PBS reports it: deduplicated and compressed data on disk. For an S3 datastore "
        "this is the local cache filesystem, not the bucket (see `s3_data_bytes`)."
    )
    available_bytes: int = Field(description="Bytes PBS can still write (S3: on the local cache filesystem).")
    total_bytes: int = Field(description="Size of the filesystem in bytes (S3: of the local cache filesystem).")
    logical_bytes: int = Field(
        description="Sum of the sizes of every current backup before deduplication, from RESURGAM's inventory."
    )
    usage_percent: float = Field(
        description="`used_bytes / total_bytes * 100`, rounded to 2 decimals; 0 if total is 0."
    )
    group_count_vm: int = Field(description="Backup groups of type `vm` on the datastore.")
    group_count_ct: int = Field(description="Backup groups of type `ct` on the datastore.")
    group_count_host: int = Field(description="Backup groups of type `host` on the datastore.")
    snapshot_count: int = Field(description="Backups (snapshots) on the datastore.")
    oldest_snapshot_at: datetime | None = Field(description="Time of the oldest backup; null without backups.")
    newest_snapshot_at: datetime | None = Field(description="Time of the newest backup; null without backups.")
    last_successful_gc_at: datetime | None = Field(
        description="When the last successful garbage collection on this datastore ended; null if none is known."
    )
    s3_data_bytes: int | None = Field(
        default=None,
        description=(
            "S3-backed datastores only (ADR 0033): chunk data in the bucket as found by the last garbage collection "
            "(PBS `gc-status.disk-bytes`). Null for filesystem datastores and until a GC has run on the datastore. "
            "For S3, `used_bytes`/`available_bytes`/`total_bytes` describe the local cache filesystem, not the bucket."
        ),
    )
    s3_get_requests: int | None = Field(
        default=None,
        description="S3 datastores only: GET requests PBS sent to the endpoint since the datastore's last counter "
        "reset (PBS `s3-statistics`; restore, verify, GC, S3 refresh). Null for filesystem datastores.",
    )
    s3_put_requests: int | None = Field(
        default=None,
        description="S3 datastores only: PUT requests since the last counter reset (backup, sync, metadata changes).",
    )
    s3_post_requests: int | None = Field(
        default=None,
        description="S3 datastores only: POST requests since the last counter reset (bulk deletion during GC).",
    )
    s3_head_requests: int | None = Field(
        default=None,
        description="S3 datastores only: HEAD requests since the last counter reset (access and existence checks).",
    )
    s3_delete_requests: int | None = Field(
        default=None,
        description="S3 datastores only: DELETE requests since the last counter reset (GC, content moves).",
    )
    s3_uploaded_bytes: int | None = Field(
        default=None, description="S3 datastores only: bytes uploaded to the endpoint since the last counter reset."
    )
    s3_downloaded_bytes: int | None = Field(
        default=None,
        description="S3 datastores only: bytes downloaded from the endpoint since the last counter reset (egress).",
    )


class CapacityForecastOut(BaseModel):
    status: str = Field(
        description=(
            "`growing` (used space grows; `days_until_full` is set), `not_growing` (flat or shrinking over the "
            "window -- no full date), `insufficient_history` (fewer than 7 days of measurements yet), or "
            "`not_applicable` (an S3-backed datastore: a bucket has no fixed size, ADR 0033)."
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


class S3TrafficOut(BaseModel):
    """Requests and bytes over a window, summed from RESURGAM's measurements."""

    s3_get_requests: int = Field(description="GET requests in the window.")
    s3_put_requests: int = Field(description="PUT requests in the window.")
    s3_post_requests: int = Field(description="POST requests in the window.")
    s3_head_requests: int = Field(description="HEAD requests in the window.")
    s3_delete_requests: int = Field(description="DELETE requests in the window.")
    s3_uploaded_bytes: int = Field(description="Bytes uploaded to the S3 endpoint in the window.")
    s3_downloaded_bytes: int = Field(description="Bytes downloaded from the S3 endpoint in the window (egress).")


class DatastoreUsageOut(BaseModel):
    latest: UsageMeasurementOut | None = Field(description="The newest measurement, or null before the first sync.")
    history: list[UsageMeasurementOut] = Field(
        description="The newest 500 measurements (one per Collector sync), oldest first."
    )
    daily_change_bytes: int | None = Field(
        description=(
            "Change of `used_bytes` (for an S3 datastore: of `s3_data_bytes`) against the measurement closest to "
            "24 h ago; null until one exists."
        )
    )
    monthly_change_bytes: int | None = Field(
        description=(
            "Change of `used_bytes` (for an S3 datastore: of `s3_data_bytes`) against the measurement closest to "
            "30 days ago; null until one exists."
        )
    )
    forecast: CapacityForecastOut = Field(description="When the datastore will be full at its current net growth.")
    backend_type: str = Field(
        default="filesystem",
        description=(
            "`filesystem` or `s3` (PBS 4.x S3-backed datastore, ADR 0033). For `s3` the used/available/total figures "
            "are the local cache filesystem, and the forecast is `not_applicable`."
        ),
    )
    s3_traffic_24h: S3TrafficOut | None = Field(
        default=None,
        description=(
            "S3 datastores only: S3 requests and bytes in the last 24 h, from consecutive measurements. A counter "
            "reset on PBS (its threshold reset schedule) is handled; traffic between the last measurement and a reset "
            "is not seen, so this is a lower bound. Null for filesystem datastores or with fewer than two measurements."
        ),
    )
    s3_traffic_30d: S3TrafficOut | None = Field(
        default=None, description="Like `s3_traffic_24h`, over the last 30 days."
    )
