from pydantic import BaseModel, Field


class InstanceSettingsOut(BaseModel):
    min_report_retention_days: int = Field(
        description="RESURGAM-wide floor for report retention, in days. A customer's own report_retention_days can extend beyond this but never go below it."
    )
    collector_poll_interval_minutes: int = Field(
        description="How often the Collector scheduler re-syncs each non-disabled PBS instance, in minutes. Defaults to 15."
    )


class UpdateInstanceSettingsRequest(BaseModel):
    min_report_retention_days: int = Field(
        ge=1, description="New platform-wide floor for report retention, in days. Must be at least 1."
    )
    collector_poll_interval_minutes: int = Field(
        ge=1, description="New Collector scheduler poll interval, in minutes. Must be at least 1."
    )
