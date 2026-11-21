import { useEffect, useRef, useState } from "react";
import { useMutation, useQueries } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import { fileRestoreApi } from "../api/resources";
import type { FileRestoreJobOut } from "../api/types";

const POLL_INTERVAL_MS = 1500;
/** Polling a job that can't be read backs off (3 s, 6 s, 12 s, …) and gives up after this many failures. */
const MAX_POLL_ERRORS = 5;
const MAX_BACKOFF_MS = 30_000;

type PollState = { data?: FileRestoreJobOut; error: unknown; errorUpdateCount: number };

/** Exported for tests. */
export function archivePollInterval(state: PollState): number | false {
  if (state.error) {
    if (state.errorUpdateCount >= MAX_POLL_ERRORS) return false;
    return Math.min(POLL_INTERVAL_MS * 2 ** state.errorUpdateCount, MAX_BACKOFF_MS);
  }
  return state.data === undefined || isInFlight(state.data.status) ? POLL_INTERVAL_MS : false;
}

function isInFlight(status: FileRestoreJobOut["status"] | undefined): boolean {
  return status === "pending" || status === "running";
}

export interface ArchiveDownload {
  jobId: string;
  /** What the operator picked, for the list: a folder name or "3 entries". */
  label: string;
  job: FileRestoreJobOut | undefined;
  /** The browser save failed after the ZIP was ready (e.g. the content expired meanwhile). */
  saveFailed: boolean;
  /** The job's state could not be read (last poll failed); polling backs off and stops after a few tries. */
  loadError: string | null;
}

/** Folder / multi-selection ZIP downloads (`archive` jobs) out of one snapshot: creates the job,
 * polls it while it runs (progress, then size and skipped entries), and hands the finished ZIP
 * to the browser exactly once -- via a download-ticket link, so it streams to disk. Unlike
 * single-file downloads (`useSnapshotFileDownloads`) a finished archive stays in the list until
 * dismissed: its report (what was skipped or renamed) is worth reading, and "Download again"
 * works for as long as the server keeps the content. */
function errorMessage(error: unknown): string | null {
  if (!error) return null;
  return error instanceof ApiError || error instanceof Error ? error.message : String(error);
}

export function useSnapshotArchiveDownloads(groupId: string, snapshotId: string) {
  const [archives, setArchives] = useState<{ jobId: string; label: string }[]>([]);
  const [saveFailed, setSaveFailed] = useState<Record<string, boolean>>({});
  const saved = useRef(new Set<string>());

  const createMutation = useMutation({
    mutationFn: ({ paths }: { paths: string[]; label: string }) =>
      fileRestoreApi.createArchiveJob(groupId, snapshotId, paths),
    onSuccess: (job, { label }) => setArchives((prev) => [{ jobId: job.id, label }, ...prev]),
  });

  const queries = useQueries({
    queries: archives.map(({ jobId }) => ({
      queryKey: ["file-restore-archive-job", jobId],
      queryFn: () => fileRestoreApi.getJob(jobId),
      retry: false,
      refetchInterval: (query: { state: PollState }) => archivePollInterval(query.state),
    })),
  });

  const save = (jobId: string) => {
    setSaveFailed((prev) => ({ ...prev, [jobId]: false }));
    fileRestoreApi.saveJobContent(jobId).catch(() => setSaveFailed((prev) => ({ ...prev, [jobId]: true })));
  };

  const statuses = queries.map((q) => q.data?.status ?? "").join(",");
  useEffect(() => {
    queries.forEach((query) => {
      const job = query.data;
      if (job?.status === "completed" && job.content_available !== false && !saved.current.has(job.id)) {
        saved.current.add(job.id);
        save(job.id);
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statuses]);

  const downloads: ArchiveDownload[] = archives.map(({ jobId, label }, index) => ({
    jobId,
    label,
    job: queries[index]?.data,
    saveFailed: saveFailed[jobId] ?? false,
    loadError: errorMessage(queries[index]?.error),
  }));

  const createError = createMutation.error;
  return {
    downloads,
    startArchive: (paths: string[], label: string) => createMutation.mutate({ paths, label }),
    isCreating: createMutation.isPending,
    /** The job could not even be created -- e.g. 507 not enough disk space, 422 invalid selection. */
    createErrorMessage: createError ? (createError instanceof ApiError ? createError.message : String(createError)) : null,
    saveAgain: save,
    dismiss: (jobId: string) => setArchives((prev) => prev.filter((a) => a.jobId !== jobId)),
  };
}
