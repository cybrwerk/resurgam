import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { fileRestoreApi } from "../api/resources";
import type { FileEntryOut, FileRestoreJobOut } from "../api/types";
import { useSnapshotArchiveDownloads } from "../hooks/useSnapshotArchiveDownloads";
import { useSnapshotFileDownloads } from "../hooks/useSnapshotFileDownloads";
import { pathSegments } from "../lib/pathSegments";
import { ArchiveDownloadList } from "./ArchiveDownloadList";
import { EmptyState } from "./EmptyState";
import { InlineAlert } from "./InlineAlert";
import { SnapshotEntryList, type SnapshotEntryRow } from "./SnapshotEntryList";
import styles from "./FileBrowserPanel.module.css";

interface FileBrowserPanelProps {
  groupId: string;
  snapshotId: string;
  /** Re-mounts the browser at "/" whenever this changes — the modal use
   * passes its own `open` flag (reset on every re-open, even for the
   * same snapshot), the page use just passes `snapshotId` itself (reset
   * only when the operator actually picks a different snapshot). */
  resetKey: string;
  /** Optionally controlled -- the Unified File Workspace's DiskTree needs
   * to drive this panel to a specific directory too, so it passes both.
   * `FileBrowserModal` (Datastores tree → snapshot row popup) omits
   * both and keeps this panel's own internal path state, unchanged. */
  path?: string;
  onPathChange?: (path: string) => void;
}

const POLL_INTERVAL_MS = 1500;
const IN_FLIGHT_STATUSES: FileRestoreJobOut["status"][] = ["pending", "running"];

function isInFlight(status: FileRestoreJobOut["status"] | undefined): boolean {
  return status !== undefined && IN_FLIGHT_STATUSES.includes(status);
}

/** Browses and downloads individual files out of a backup snapshot
 * (manifest §13). Dispatches to whichever backend (CT/host catalog vs.
 * VM qemu file-restore) the snapshot's own backup type needs; this
 * component doesn't need to know which one answered.
 *
 * Listing and downloading both run as async jobs (ADR 0013) rather than a
 * plain synchronous request — a `list`/`download` job is created, then
 * polled via `refetchInterval` (this app's first use of TanStack Query
 * polling) until it reaches a terminal status. This is necessary, not
 * just faster: CT/host downloads can take minutes regardless of file
 * size, and VM file-restore beyond the top-level disk listing requires a
 * privileged out-of-process worker, not the API request handler itself.
 *
 * `file_restore_unavailable` gets its own explicit message rather than
 * the generic load-error state, since it means "not available in this
 * environment", not "the request failed".
 *
 * Folders and multi-selections download as ONE ZIP (`archive` job): every row has a checkbox,
 * a folder row also its own "ZIP" action; the selection belongs to the directory shown and is
 * cleared when navigating away. Running/finished ZIPs are listed above the entries with their
 * progress, size and skipped entries (`ArchiveDownloadList`).
 *
 * Pure content, no Modal/page chrome of its own — `FileBrowserModal`
 * wraps this in a `Modal` (opened from a snapshot row in the Datastores
 * tree); `FileRecoveryPage` renders it directly inline after its own
 * customer/group/snapshot pickers. */
export function FileBrowserPanel({
  groupId,
  snapshotId,
  resetKey,
  path: controlledPath,
  onPathChange,
}: FileBrowserPanelProps) {
  const { t } = useTranslation();
  const [internalPath, setInternalPath] = useState("/");
  const path = controlledPath ?? internalPath;
  const setPath = onPathChange ?? setInternalPath;
  const [listJobId, setListJobId] = useState<string | null>(null);
  const { downloadError, isDownloading, startDownload } = useSnapshotFileDownloads(groupId, snapshotId);
  const archives = useSnapshotArchiveDownloads(groupId, snapshotId);
  const lastResetKey = useRef(resetKey);

  useEffect(() => {
    if (lastResetKey.current !== resetKey) {
      lastResetKey.current = resetKey;
      setPath("/");
    }
  }, [resetKey]);

  const createListJobMutation = useMutation({
    mutationFn: (p: string) => fileRestoreApi.createJob(groupId, snapshotId, "list", p),
    onSuccess: (job) => setListJobId(job.id),
  });

  useEffect(() => {
    setListJobId(null);
    createListJobMutation.mutate(path);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groupId, snapshotId, path]);

  const listJobQuery = useQuery({
    queryKey: ["file-restore-list-job", listJobId],
    queryFn: () => fileRestoreApi.getJob(listJobId as string),
    enabled: listJobId !== null,
    refetchInterval: (query) => (isInFlight(query.state.data?.status) ? POLL_INTERVAL_MS : false),
  });

  const handleRowClick = (entry: FileEntryOut) => {
    if (entry.type === "directory") {
      setPath(entry.path);
    } else {
      startDownload(entry);
    }
  };

  const crumbs = pathSegments(path);
  const listJob = listJobQuery.data;
  const entries = listJob?.status === "completed" ? listJob.result ?? [] : null;
  const isListLoading = createListJobMutation.isPending || listJobQuery.isLoading || isInFlight(listJob?.status);
  const isListUnavailable =
    (createListJobMutation.isError &&
      createListJobMutation.error instanceof ApiError &&
      createListJobMutation.error.code === "file_restore_unavailable") ||
    (listJob?.status === "failed" && listJob.error_code === "file_restore_unavailable");
  // The worker's own message says what is missing and how to install it (e.g. "proxmox-file-restore is
  // not installed on the host running the file-restore worker. ... apply the latest update ...").
  const unavailableDetail = isListUnavailable
    ? createListJobMutation.error instanceof ApiError
      ? createListJobMutation.error.message
      : listJob?.error_message ?? null
    : null;
  const isListError =
    !isListUnavailable && (createListJobMutation.isError || listJobQuery.isError || listJob?.status === "failed");
  const hasEntries = (entries?.length ?? 0) > 0;
  const rows: SnapshotEntryRow[] = (entries ?? []).map((entry) => ({
    key: entry.path,
    path: entry.path,
    name: entry.name,
    isDirectory: entry.type === "directory",
    size: entry.size,
  }));
  const startZip = (selection: SnapshotEntryRow[]) =>
    archives.startArchive(
      selection.map((row) => row.path),
      selection.length === 1 ? selection[0].name : t("fileRestore.archive.entriesLabel", { count: selection.length }),
    );

  return (
    <div>
      <div className={styles.breadcrumbs}>
        <button type="button" className={styles.crumb} onClick={() => setPath("/")} disabled={path === "/"}>
          {t("fileRestore.root")}
        </button>
        {crumbs.map((crumb) => (
          <span key={crumb.path}>
            <span className={styles.crumbSeparator}>/</span>
            <button
              type="button"
              className={styles.crumb}
              onClick={() => setPath(crumb.path)}
              disabled={crumb.path === path}
            >
              {crumb.label}
            </button>
          </span>
        ))}
      </div>

      {downloadError && (
        <InlineAlert tone="danger">{t("fileRestore.downloadFailed", { name: downloadError.name })}</InlineAlert>
      )}

      {archives.createErrorMessage && (
        <InlineAlert tone="danger">
          {t("fileRestore.archive.createFailed")} {archives.createErrorMessage}
        </InlineAlert>
      )}

      <ArchiveDownloadList
        downloads={archives.downloads}
        onSaveAgain={archives.saveAgain}
        onDismiss={archives.dismiss}
      />

      {hasEntries && !downloadError && <p className={`mutedText ${styles.hint}`}>{t("fileRestore.downloadHint")}</p>}

      {isListLoading && <p className="mutedText">{t("common.loading")}</p>}

      {isListUnavailable && (
        <EmptyState
          title={t("fileRestore.unavailableTitle")}
          description={t("fileRestore.unavailableDescription")}
          action={unavailableDetail ? <p className="mutedText">{unavailableDetail}</p> : undefined}
        />
      )}

      {isListError && (
        <InlineAlert tone="danger">
          {t("common.loadError")}
          {" — "}
          <button
            type="button"
            className="button buttonGhost"
            onClick={() => {
              setListJobId(null);
              createListJobMutation.mutate(path);
            }}
          >
            {t("common.retry")}
          </button>
        </InlineAlert>
      )}

      {entries && entries.length === 0 && <EmptyState title={t("fileRestore.emptyDirectory")} />}

      {entries && entries.length > 0 && (
        <SnapshotEntryList
          rows={rows}
          selectionKey={`${snapshotId}:${path}`}
          isDownloading={isDownloading}
          onOpen={(row) => handleRowClick(entries.find((entry) => entry.path === row.path) as FileEntryOut)}
          onDownloadZip={startZip}
          zipDisabled={archives.isCreating}
        />
      )}
    </div>
  );
}
