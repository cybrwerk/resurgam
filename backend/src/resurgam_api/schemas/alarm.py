from uuid import UUID

from pydantic import BaseModel, Field


class AlarmOut(BaseModel):
    type: str = Field(
        description=(
            "What is wrong: `protection_critical` / `protection_warning` (a backup group is overdue), "
            "`datastore_usage_critical` / `datastore_usage_warning` (datastore >= 95 % / 80 % full), "
            "`instant_recovery_running_long` (a VM runs from its backup longer than the cluster's alarm hours), "
            "`instant_recovery_overlay_storage` (the overlay storage of a VM running from its backup is >= 80 % full), "
            "`instant_recovery_failed` (a failed Instant Recovery left its VM or backup mapping behind), "
            "`backup_key_changed` (a backup group's newest backup uses a different encryption key, or none, than "
            "the backup before it -- older and newer backups need different keys to restore)."
        )
    )
    severity: str = Field(description="`critical` or `warning`.")
    customer_id: UUID | None = Field(description="The affected customer; null when the entity has none.")
    customer_name: str | None = Field(default=None, description="Display name of `customer_id`.")
    customer_match_code: str | None = Field(default=None, description="Three-letter short code of `customer_id`.")
    message: str = Field(description="Human-readable description, ready to show.")
    entity_type: str = Field(description="`backup_group`, `datastore` or `restore_plan`.")
    entity_id: UUID = Field(description="ID of the affected entity (of `entity_type`).")
