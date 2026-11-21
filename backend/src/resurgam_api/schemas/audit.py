from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    occurred_at: datetime
    actor_user_id: UUID | None
    actor_label: str | None = Field(
        default=None,
        description="Actor's display name and email, snapshotted at write time -- stays correct even after the "
        "user account is deleted (actor_user_id alone would go null). Null only for events recorded before this "
        "field existed, or with no actor at all (a system-initiated event).",
    )
    actor_is_breakglass: bool = Field(
        default=False,
        description="Whether this action was taken via the emergency break-glass credential, snapshotted at write "
        "time for the same reason as actor_label.",
    )
    action: str
    target_type: str | None
    target_id: str | None
    correlation_id: str | None
    detail: dict | None
    customer_id: UUID | None
