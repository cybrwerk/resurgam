import type { TaskRunOut, TaskStatus } from "../api/types";

export interface LastRun {
  status: TaskStatus;
  startedAt: string;
}

function mostRecent(rows: TaskRunOut[]): LastRun | null {
  if (rows.length === 0) return null;
  const latest = rows.reduce((a, b) => (a.started_at > b.started_at ? a : b));
  return { status: latest.status, startedAt: latest.started_at };
}

/** Correlates a job *definition* to its own recent runs from PBS's
 * already-ingested task history (`TaskRun`, reused via `tasksApi.list`) --
 * no new Collector/PVE ingestion needed (docs/adr/0023). Each category's
 * `worker_id` convention was checked live against the real PBS instance
 * (2026-09-27) and turned out to genuinely differ between categories --
 * prune keys by namespace, verify by the job's own id: */

/** `task_type == "prunejob"`, `worker_id == "{store}:{ns}"` (bare `store`
 * when `ns` is root) -- confirmed live. */
export function lastPruneRun(tasks: TaskRunOut[], store: string, ns: string | null): LastRun | null {
  const worker = ns ? `${store}:${ns}` : store;
  return mostRecent(tasks.filter((t) => t.task_type === "prunejob" && t.worker_id === worker));
}

/** `task_type == "verify"`, `worker_id == "{store}:{job_id}"` -- the
 * verify job's own id, confirmed live. Deliberately *not* the same
 * `{store}:{ns}` shape prune uses -- checked empirically, not assumed
 * symmetric between the two categories. */
export function lastVerifyRun(tasks: TaskRunOut[], store: string, jobId: string): LastRun | null {
  const worker = `${store}:${jobId}`;
  return mostRecent(tasks.filter((t) => t.task_type === "verify" && t.worker_id === worker));
}

/** `task_type == "sync"`, `worker_id == "{store}:{job_id}"` -- **not**
 * live-confirmed (no sync job has ever run on the real environment this
 * session verified against). Mirrors verify's confirmed `{store}:{job_id}`
 * shape as the best-grounded guess; re-check against a real sync run
 * before relying on this for anything beyond a UI nicety. */
export function lastSyncRun(tasks: TaskRunOut[], store: string, jobId: string): LastRun | null {
  const worker = `${store}:${jobId}`;
  return mostRecent(tasks.filter((t) => t.task_type === "sync" && t.worker_id === worker));
}

/** `task_type == "garbage_collection"`, `worker_id == store` alone --
 * confirmed live. Datastore-scoped, not job-scoped (GC isn't a job list). */
export function lastGcRun(tasks: TaskRunOut[], store: string): LastRun | null {
  return mostRecent(tasks.filter((t) => t.task_type === "garbage_collection" && t.worker_id === store));
}

/** `task_type == "backup"`, `worker_id == "{pbs_datastore}:{guest_type}/{vmid}"`
 * -- confirmed live. Only meaningful for a job with an explicit `vmid`
 * list (the common case); `all_guests`/`pool` jobs would need the live
 * guest list to know which vmids to check and are left unmatched here
 * (render "—") rather than guessed at. */
export function lastBackupRun(tasks: TaskRunOut[], pbsDatastore: string, vmids: string[]): LastRun | null {
  const workers = new Set(vmids.flatMap((vmid) => [`${pbsDatastore}:vm/${vmid}`, `${pbsDatastore}:ct/${vmid}`]));
  return mostRecent(tasks.filter((t) => t.task_type === "backup" && t.worker_id != null && workers.has(t.worker_id)));
}
