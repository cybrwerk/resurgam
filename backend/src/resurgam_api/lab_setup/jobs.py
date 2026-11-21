"""In-process jobs for the guided SSH flows (agent install, template build).

Why an in-memory job with polling, not a request kept open and not the
Postgres job queue (ADR 0002):

- The credentials are one-time and must never be stored. The DB queue is
  consumed by separate worker processes, which would need the credentials
  persisted somewhere to pick them up -- ruled out.
- A template build takes minutes (image download, first boot, apt). A single
  long request would show nothing until the end; the wizard needs a live
  step log, and a reloaded tab must be able to re-attach. A job id that the
  wizard polls (the same `refetchInterval` pattern it already uses while
  waiting for the agent) gives both.

So each job runs in a background thread of the API process. The credentials
exist only as local variables of that thread and are dropped when it ends;
the job record (status, steps, result) holds no secret -- every message and
command output passes `redact()` against the job's secrets first. Finished
jobs are kept for `Settings.lab_ssh_job_retention_seconds` and are lost on
an API restart (the wizard then says so). This relies on the API running as
one process, which is how the appliance runs it (one uvicorn process).
"""

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from resurgam_worker.lab_build.errors import StepFailed

logger = logging.getLogger("resurgam_api.lab_setup.jobs")

REDACTED = "***"
# The tail of a command's output kept per step (the UI shows it on demand).
MAX_STEP_OUTPUT = 8000


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class JobStep:
    key: str
    status: str  # running | ok | warning | failed | skipped
    message: str
    output: str | None = None
    started_at: datetime = field(default_factory=_now)
    finished_at: datetime | None = None


@dataclass
class LabSetupJob:
    id: uuid.UUID
    cluster_id: uuid.UUID
    kind: str  # agent_install | template_build
    node: str
    status: str = "running"  # running | succeeded | failed
    started_at: datetime = field(default_factory=_now)
    finished_at: datetime | None = None
    steps: list[JobStep] = field(default_factory=list)
    result: dict = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


class JobLog:
    """What an orchestrator writes to: steps with redacted messages/output."""

    def __init__(self, job: LabSetupJob, secrets: list[str], lock: threading.Lock):
        self._job = job
        self._secrets = [s for s in secrets if s and len(s) >= 4]
        self._lock = lock

    def redact(self, text: str | None) -> str | None:
        if text is None:
            return None
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
            # A multi-line secret (a private key) may appear line by line.
            for line in secret.splitlines():
                if len(line.strip()) >= 16:
                    text = text.replace(line.strip(), REDACTED)
        return text

    def add_secret(self, secret: str | None) -> None:
        if secret and len(secret) >= 4:
            self._secrets.append(secret)

    def start(self, key: str, message: str) -> JobStep:
        step = JobStep(key=key, status="running", message=self.redact(message) or "")
        with self._lock:
            self._job.steps.append(step)
        return step

    def finish(self, step: JobStep, status: str, message: str | None = None, output: str | None = None) -> None:
        with self._lock:
            step.status = status
            if message is not None:
                step.message = self.redact(message) or ""
            if output:
                step.output = (self.redact(output) or "")[-MAX_STEP_OUTPUT:]
            step.finished_at = _now()

    def note(self, key: str, status: str, message: str, output: str | None = None) -> None:
        step = self.start(key, message)
        self.finish(step, status, output=output)

    def set_result(self, **values) -> None:
        with self._lock:
            self._job.result.update(values)

    def wipe(self) -> None:
        self._secrets = []


def _default_executor(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="resurgam-lab-setup-job", daemon=True).start()


class JobRegistry:
    def __init__(self):
        self._jobs: dict[uuid.UUID, LabSetupJob] = {}
        self._lock = threading.Lock()
        # Tests swap this for a synchronous runner.
        self.executor: Callable[[Callable[[], None]], None] = _default_executor

    def _prune(self, retention: timedelta) -> None:
        cutoff = _now() - retention
        for job_id in [j.id for j in self._jobs.values() if j.finished_at is not None and j.finished_at < cutoff]:
            del self._jobs[job_id]

    def running_for_cluster(self, cluster_id: uuid.UUID) -> LabSetupJob | None:
        with self._lock:
            return next((j for j in self._jobs.values() if j.cluster_id == cluster_id and j.status == "running"), None)

    def get(self, job_id: uuid.UUID) -> LabSetupJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def snapshot(self, job: LabSetupJob) -> LabSetupJob:
        """A consistent copy for serialization while the thread keeps writing."""
        with self._lock:
            return LabSetupJob(
                id=job.id,
                cluster_id=job.cluster_id,
                kind=job.kind,
                node=job.node,
                status=job.status,
                started_at=job.started_at,
                finished_at=job.finished_at,
                steps=[JobStep(**vars(s)) for s in job.steps],
                result=dict(job.result),
                error_code=job.error_code,
                error_message=job.error_message,
            )

    def start(
        self,
        *,
        cluster_id: uuid.UUID,
        kind: str,
        node: str,
        secrets: list[str],
        work: Callable[[JobLog], None],
        on_finish: Callable[[LabSetupJob], None] | None = None,
        retention: timedelta = timedelta(hours=1),
    ) -> LabSetupJob:
        """Registers the job and hands `work(log)` to the executor. `work`
        raises `StepFailed` (or anything else) to fail the job; `on_finish`
        runs afterwards either way (audit, releasing resources)."""
        job = LabSetupJob(id=uuid.uuid4(), cluster_id=cluster_id, kind=kind, node=node)
        with self._lock:
            self._prune(retention)
            self._jobs[job.id] = job
        log = JobLog(job, secrets, self._lock)

        def run() -> None:
            try:
                work(log)
                with self._lock:
                    job.status = "succeeded"
            except StepFailed as exc:
                self._fail(job, log, exc.code, exc.message, exc.output)
            except Exception as exc:  # noqa: BLE001 -- any failure ends the job with a visible reason
                logger.warning("lab setup job %s (%s) failed: %s", job.id, kind, log.redact(str(exc)))
                self._fail(job, log, "internal_error", f"Unexpected error: {exc}", None)
            finally:
                with self._lock:
                    job.finished_at = _now()
                if on_finish is not None:
                    try:
                        on_finish(self.snapshot(job))
                    except Exception as exc:  # noqa: BLE001 -- bookkeeping must not mask the job's own outcome
                        logger.warning("lab setup job %s: finishing step failed: %s", job.id, log.redact(str(exc)))
                log.wipe()

        self.executor(run)
        return job

    def _fail(self, job: LabSetupJob, log: JobLog, code: str, message: str, output: str | None) -> None:
        message = log.redact(message) or ""
        with self._lock:
            running = next((s for s in reversed(job.steps) if s.status == "running"), None)
        if running is not None:
            log.finish(running, "failed", message, output)
        with self._lock:
            job.status = "failed"
            job.error_code = code
            job.error_message = message


registry = JobRegistry()
