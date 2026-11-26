from uuid import UUID

from pydantic import BaseModel, Field


class AlarmOut(BaseModel):
    type: str = Field(
        description=(
            "What is wrong: `protection_critical` / `protection_warning` (a backup group is overdue), "
            "`datastore_usage_critical` / `datastore_usage_warning` (datastore >= 95 % / 80 % full), "
            "`datastore_full_soon_critical` / `datastore_full_soon_warning` (at the last 30 days' net growth the "
            "datastore is full in <= 14 / <= 30 days; only raised when more severe than the usage alarm), "
            "`instant_recovery_running_long` (a VM runs from its backup longer than the cluster's alarm hours), "
            "`instant_recovery_overlay_storage` (the overlay storage of a VM running from its backup is >= 80 % full), "
            "`instant_recovery_failed` (a failed Instant Recovery left its VM or backup mapping behind), "
            "`backup_key_changed` (a backup group's newest backup uses a different encryption key, or none, than "
            "the backup before it -- older and newer backups need different keys to restore), "
            "`encryption_key_missing` (backups in a datastore are encrypted with a key RESURGAM does not have -- "
            "they cannot be indexed, browsed or restored here), "
            "`encryption_key_copy_unconfirmed` (no one confirmed yet that a copy of a stored key is kept outside "
            "RESURGAM; platform admins only), "
            "`guest_not_backed_up` (PVE guests that no enabled backup job covers, one alarm per cluster; platform "
            "admins only), `offsite_copy_missing` (a sync job should copy these workloads offsite but no copy arrived "
            "within the warning threshold), `offsite_copy_lagging_critical` / `offsite_copy_lagging_warning` (the "
            "oldest snapshot not copied offsite yet is older than the critical / warning threshold), "
            "`offsite_copy_gap` (no checked offsite copy: a copy on the same PBS only, a target PBS not connected to "
            "RESURGAM, or no sync job although the protection policy requires Sync/DR). The offsite alarms are one per source datastore and customer; see "
            "GET /backup-gaps for each workload."
        )
    )
    severity: str = Field(description="`critical` or `warning`.")
    customer_id: UUID | None = Field(description="The affected customer; null when the entity has none.")
    customer_name: str | None = Field(default=None, description="Display name of `customer_id`.")
    customer_match_code: str | None = Field(default=None, description="Three-letter short code of `customer_id`.")
    message: str = Field(description="Human-readable description, ready to show.")
    entity_type: str = Field(
        description="`backup_group`, `datastore`, `restore_plan`, `encryption_key` or `pve_cluster`."
    )
    entity_id: UUID = Field(description="ID of the affected entity (of `entity_type`).")
