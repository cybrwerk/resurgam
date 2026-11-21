from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProtectionPolicyCreateRequest(BaseModel):
    customer_id: UUID
    name: str = Field(min_length=1, max_length=100)
    expected_interval_hours: int = Field(gt=0)
    max_time_since_last_backup_hours: int = Field(gt=0)
    expected_verify_deadline_hours: int | None = Field(default=None, gt=0)
    min_snapshot_count: int = Field(default=1, ge=0)
    sync_dr_required: bool = False

    @model_validator(mode="after")
    def _max_time_covers_interval(self) -> "ProtectionPolicyCreateRequest":
        if self.max_time_since_last_backup_hours < self.expected_interval_hours:
            raise ValueError(
                "max_time_since_last_backup_hours must be greater than or equal to expected_interval_hours"
            )
        return self


class ProtectionPolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    customer_id: UUID
    name: str
    expected_interval_hours: int
    max_time_since_last_backup_hours: int
    expected_verify_deadline_hours: int | None
    min_snapshot_count: int
    sync_dr_required: bool
    created_at: datetime


class AssignProtectionPolicyRequest(BaseModel):
    protection_policy_id: UUID
