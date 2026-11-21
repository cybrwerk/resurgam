from typing import Literal

from pydantic import BaseModel, Field


class BackupRunTaskOut(BaseModel):
    node: str = Field(description="The node the backup task runs on.")
    upid: str = Field(description="PVE task id; follow it with GET /pve-clusters/{id}/tasks/{node}/{upid}.")
    vmids: list[int] | None = Field(
        default=None, description="The guests backed up by this task, for a job with a VMID list; null for all/pool."
    )


class BackupRunSkippedOut(BaseModel):
    vmid: int = Field(description="A VMID listed in the job that this run does not back up.")
    reason: Literal["missing", "other_node"] = Field(
        description="`missing`: the guest no longer exists on the cluster. `other_node`: the job is bound to one "
        "node (`node`) and the guest lives on another -- a scheduled run skips it the same way."
    )


class BackupRunOut(BaseModel):
    job_id: str = Field(description="The backup job that was started.")
    tasks: list[BackupRunTaskOut] = Field(description="One vzdump task per node holding guests of the job.")
    skipped: list[BackupRunSkippedOut] = Field(
        default_factory=list, description="VMIDs in the job's list that this run does not back up, with the reason."
    )


class PveTaskLogLine(BaseModel):
    n: int = Field(description="1-based line number in the task log.")
    t: str = Field(description="The line's text.")


class PveTaskProgressOut(BaseModel):
    node: str = Field(description="The node.")
    upid: str = Field(description="PVE task id.")
    status: str | None = Field(default=None, description="`running` or `stopped` (PVE's own values).")
    exitstatus: str | None = Field(
        default=None, description="Once stopped: `OK` on success, `WARNINGS: n`, or the error text."
    )
    lines: list[PveTaskLogLine] = Field(
        description="Log lines after the first `start` lines -- at most 500 per call (see `more`). Empty while the "
        "log is empty."
    )
    more: bool = Field(
        default=False,
        description="True when this call returned a full page of 500 lines, so more may be waiting: poll again "
        "right away with `next_start`, even when `status` is already `stopped`.",
    )
    next_start: int = Field(
        description="Number of log lines read so far (the last line's `n`). Pass as `start` on the next poll to get "
        "only new lines."
    )
