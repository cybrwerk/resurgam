from resurgam_api.models.inventory import BackupGroup


def workload_label(group: BackupGroup) -> str:
    """A human-readable identity for a `BackupGroup` -- `resolved_name`
    (PBS's own notes, or a PVE guest-name fallback, see `collector.sync`)
    plus its VMID/CTID for disambiguation, or just the bare id when no
    name has ever resolved. Shared by Recovery Evidence reporting and
    every job-listing endpoint that needs to show *which workload* a job
    belongs to instead of just a `backup_group_id` UUID.
    """
    return f"{group.resolved_name} ({group.backup_id})" if group.resolved_name else group.backup_id
