import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { fileIndexApi } from "../api/resources";
import type { FileIndexEntryOut } from "../api/types";
import { useSnapshotArchiveDownloads } from "../hooks/useSnapshotArchiveDownloads";
import { useSnapshotFileDownloads } from "../hooks/useSnapshotFileDownloads";
import { pathSegments } from "../lib/pathSegments";
import { ArchiveDownloadList } from "./ArchiveDownloadList";
import { EmptyState } from "./EmptyState";
import { InlineAlert } from "./InlineAlert";
import { SnapshotEntryList } from "./SnapshotEntryList";
import styles from "./FileIndexBrowserPanel.module.css";

interface FileIndexBrowserPanelProps {
  customerId: string;
  groupId: string;
  snapshotId: string;
  indexingEnabled: boolean;
  /** Fully controlled -- the Unified File Workspace's DiskTree and
   * FilesearchAssistant both need to be able to drive this panel to a
   * specific directory too, so the page owns `path`, not this panel. */
  path: string;
  onPathChange: (path: string) => void;
}

/** An Explorer-style browser over the persistent File Index (manifest
 * §13, ADR 0016), raised directly by the user after the search-only
 * version shipped: a directory tree instead of a flat results table,
 * "point in time" snapshot switching that keeps the current path (the
 * parent page re-renders this with a new `snapshotId` on switch, `path`
 * itself is only reset when the *group* changes), and recursive search
 * scoped to wherever you're currently browsing rather than a separate,
 * always-global search page.
 *
 * File rows are downloadable (via the same on-demand file-restore job
 * queue `FileBrowserPanel` uses, ADR 0013) even though the index itself
 * only ever stores metadata — the index tells you *what* exists and
 * *where*, the actual bytes still come from a live PBS call.
 */
export function FileIndexBrowserPanel({
  customerId,
  groupId,
  snapshotId,
  indexingEnabled,
  path,
  onPathChange,
}: FileIndexBrowserPanelProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const setPath = onPathChange;
  const [searchInput, setSearchInput] = useState("");
  const [searchTerm, setSearchTerm] = useState("");
  const { downloadError, isDownloading, startDownload } = useSnapshotFileDownloads(groupId, snapshotId);
  const archives = useSnapshotArchiveDownloads(groupId, snapshotId);
  const lastGroupId = useRef(groupId);

  useEffect(() => {
    if (lastGroupId.current !== groupId) {
      lastGroupId.current = groupId;
      setSearchInput("");
      setSearchTerm("");
    }
  }, [groupId]);

  const isSearching = searchTerm.trim() !== "";

  const browseQuery = useQuery({
    queryKey: ["file-index-browse", groupId, snapshotId, path],
    queryFn: () => fileIndexApi.browse({ backupGroupId: groupId, backupSnapshotId: snapshotId, path }),
    enabled: !isSearching,
  });

  const searchQuery = useQuery({
    queryKey: ["file-index-browse-search", customerId, groupId, snapshotId, path, searchTerm],
    queryFn: () =>
      fileIndexApi.search({
        customerId,
        backupGroupId: groupId,
        backupSnapshotId: snapshotId,
        path: path === "/" ? undefined : `${path}/`,
        filename: searchTerm,
        limit: 200,
      }),
    enabled: isSearching,
  });

  const indexJobMutation = useMutation({
    mutationFn: () => fileIndexApi.createJob(groupId, snapshotId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["file-index-jobs", groupId] }),
  });

  const handleEntryClick = (entry: FileIndexEntryOut) => {
    if (entry.type === "directory") {
      setPath(entry.path);
      setSearchInput("");
      setSearchTerm("");
    } else {
      startDownload({ path: entry.path, name: entry.filename });
    }
  };

  const crumbs = pathSegments(path);
  const isRoot = path === "/";
  const isLoading = isSearching ? searchQuery.isLoading : browseQuery.isLoading;
  const isError = isSearching ? searchQuery.isError : browseQuery.isError;
  const entries = isSearching ? searchQuery.data?.items ?? null : browseQuery.data ?? null;
  const searchHasMore = isSearching && (searchQuery.data?.has_more ?? false);
  // A snapshot that was never indexed (or an already-indexed one whose
  // entries were later purged, e.g. PBS pruned it) looks identical from
  // here: an empty root listing. Real snapshots always have at least a
  // few top-level entries, so this is a reliable enough signal to offer
  // the same "index it" CTA the trigger buttons elsewhere already use.
  const isNotIndexed = !isSearching && isRoot && browseQuery.isSuccess && (browseQuery.data?.length ?? 0) === 0;

  return (
    <div>
      <div className={styles.toolbar}>
        <div className={`field ${styles.searchField}`}>
          <label className="label" htmlFor="file-index-browser-search">
            {t("fileIndex.searchFromHere")}
          </label>
          <input
            id="file-index-browser-search"
            type="text"
            className="input"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") setSearchTerm(searchInput.trim());
            }}
            onBlur={() => setSearchTerm(searchInput.trim())}
            placeholder={t("fileIndex.filenamePlaceholder")}
          />
        </div>
        {isSearching && (
          <button
            type="button"
            className="button"
            onClick={() => {
              setSearchInput("");
              setSearchTerm("");
            }}
          >
            {t("fileIndex.clearSearch")}
          </button>
        )}
      </div>

      {!isSearching && (
        <div className={styles.breadcrumbs}>
          <button type="button" className={styles.crumb} onClick={() => setPath("/")} disabled={isRoot}>
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
      )}

      {isSearching && (
        <p className="mutedText">{t("fileIndex.searchScope", { path: path === "/" ? "/" : `${path}/` })}</p>
      )}

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

      {isLoading && <p className="mutedText">{t("common.loading")}</p>}
      {isError && <InlineAlert tone="danger">{t("common.loadError")}</InlineAlert>}

      {isNotIndexed && (
        <div className={styles.notIndexed}>
          <EmptyState title={t("fileIndex.notIndexedTitle")} description={t("fileIndex.notIndexedDescription")} />
          {indexingEnabled && (
            <button
              type="button"
              className="button buttonPrimary"
              style={{ marginTop: "0.75rem" }}
              onClick={() => indexJobMutation.mutate()}
              disabled={indexJobMutation.isPending}
            >
              {t("fileRecovery.indexThisSnapshot")}
            </button>
          )}
        </div>
      )}

      {!isNotIndexed && entries && entries.length === 0 && (
        <EmptyState title={isSearching ? t("fileIndex.noResults") : t("fileRestore.emptyDirectory")} />
      )}

      {entries && entries.length > 0 && (
        <>
          <SnapshotEntryList
            rows={entries.map((entry) => ({
              key: entry.id,
              path: entry.path,
              name: entry.filename,
              isDirectory: entry.type === "directory",
              size: entry.size_bytes,
              secondary: isSearching ? entry.path : undefined,
            }))}
            selectionKey={`${snapshotId}:${path}:${searchTerm}`}
            isDownloading={isDownloading}
            onOpen={(row) => handleEntryClick(entries.find((entry) => entry.id === row.key) as FileIndexEntryOut)}
            onDownloadZip={(selection) =>
              archives.startArchive(
                selection.map((row) => row.path),
                selection.length === 1
                  ? selection[0].name
                  : t("fileRestore.archive.entriesLabel", { count: selection.length }),
              )
            }
            zipDisabled={archives.isCreating}
          />
          {searchHasMore && <p className="mutedText">{t("fileIndex.searchTruncated")}</p>}
        </>
      )}
    </div>
  );
}
