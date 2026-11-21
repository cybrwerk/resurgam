"""Audit detail for file-restore download/archive jobs (shared by the worker and the API)."""

from resurgam_api.models.filerestore_job import FileRestoreJob


def download_audit_detail(job: FileRestoreJob) -> dict | None:
    """What a download/archive job delivered -- paths, counts, sizes; never content (item 4 of
    the folder-ZIP work: an auditor must be able to tell what left the backup)."""
    if job.job_type == "list":
        return None
    detail: dict = {
        "job_type": job.job_type,
        "paths": job.paths if job.job_type == "archive" else [job.path],
        "filename": job.result_filename,
        "size_bytes": job.result_size_bytes,
    }
    if job.archive_report:
        report = job.archive_report
        detail.update(
            file_count=report.get("file_count"),
            directory_count=report.get("directory_count"),
            total_bytes=report.get("total_bytes"),
            skipped_count=report.get("skipped_count"),
            skipped_paths=[s["path"] for s in report.get("skipped", [])][:50],
        )
    elif job.job_type == "download" and job.result_size_bytes is not None:
        detail.update(file_count=1, total_bytes=job.result_size_bytes)
    return detail
