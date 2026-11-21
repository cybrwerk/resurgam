import { useEffect, useState } from "react";
import { useMutation, useQueries } from "@tanstack/react-query";

import { fileRestoreApi } from "../api/resources";
import type { FileRestoreJobOut } from "../api/types";

const POLL_INTERVAL_MS = 1500;
const IN_FLIGHT_STATUSES: FileRestoreJobOut["status"][] = ["pending", "running"];

function isInFlight(status: FileRestoreJobOut["status"] | undefined): boolean {
  return status !== undefined && IN_FLIGHT_STATUSES.includes(status);
}

interface DownloadableEntry {
  path: string;
  name: string;
}

/** Downloads files out of a backup snapshot via the async file-restore
 * job queue (ADR 0013): creates a "download" job, polls it until
 * terminal, then triggers the browser save. Multiple downloads can run
 * at once — each is an independent job, nothing stops starting a second
 * one while the first is still running.
 *
 * Shared by `FileBrowserPanel` (on-demand PBS browsing) and
 * `FileIndexBrowserPanel` (browsing the persistent index, ADR 0016) —
 * both let an operator click a file row to download it the same way,
 * regardless of where the directory listing itself came from.
 */
export function useSnapshotFileDownloads(groupId: string, snapshotId: string) {
  const [downloadJobs, setDownloadJobs] = useState<Record<string, DownloadableEntry>>({});
  const [downloadError, setDownloadError] = useState<{ name: string } | null>(null);

  const downloadMutation = useMutation({
    mutationFn: (entry: DownloadableEntry) => fileRestoreApi.createJob(groupId, snapshotId, "download", entry.path),
    onMutate: () => setDownloadError(null),
    onSuccess: (job, entry) => setDownloadJobs((prev) => ({ ...prev, [job.id]: entry })),
    onError: (_error, entry) => setDownloadError({ name: entry.name }),
  });

  const downloadJobIds = Object.keys(downloadJobs);
  const downloadJobQueries = useQueries({
    queries: downloadJobIds.map((jobId) => ({
      queryKey: ["file-restore-download-job", jobId],
      queryFn: () => fileRestoreApi.getJob(jobId),
      refetchInterval: (query: { state: { data?: FileRestoreJobOut } }) =>
        isInFlight(query.state.data?.status) ? POLL_INTERVAL_MS : false,
    })),
  });

  const downloadStatuses = downloadJobQueries.map((q) => q.data?.status ?? "").join(",");

  useEffect(() => {
    downloadJobQueries.forEach((query, index) => {
      const jobId = downloadJobIds[index];
      const entry = downloadJobs[jobId];
      if (!jobId || !entry || !query.data) return;

      if (query.data.status === "completed") {
        setDownloadJobs((prev) => {
          const { [jobId]: _removed, ...rest } = prev;
          return rest;
        });
        // The result may have expired server-side, or the network may
        // blip, between the job completing and this save fetch running
        // -- without a catch, that failure vanished silently (no error
        // shown, no file saved).
        fileRestoreApi.saveJobContent(jobId).catch(() => setDownloadError({ name: entry.name }));
      } else if (["failed", "expired", "cancelled"].includes(query.data.status)) {
        setDownloadJobs((prev) => {
          const { [jobId]: _removed, ...rest } = prev;
          return rest;
        });
        setDownloadError({ name: entry.name });
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [downloadStatuses]);

  const isDownloading = (path: string) => Object.values(downloadJobs).some((downloading) => downloading.path === path);
  const startDownload = (entry: DownloadableEntry) => {
    if (!isDownloading(entry.path)) downloadMutation.mutate(entry);
  };

  return { downloadError, isDownloading, startDownload };
}
