import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { fileIndexApi, fileIndexSettingsApi, inventoryApi } from "../api/resources";
import { EmptyState } from "./EmptyState";
import { FileBrowserWindow } from "./FileBrowserWindow";
import { FileIcon, FolderOpenIcon } from "./icons";
import { InlineAlert } from "./InlineAlert";
import { Modal } from "./Modal";
import { formatBytes } from "../lib/formatBytes";
import { verifyStatusPresentation } from "../lib/statusPresentation";
import { StatusBadge } from "./StatusBadge";
import styles from "./RestorePointsModal.module.css";

interface RestorePointsModalProps {
  open: boolean;
  onClose: () => void;
  groupId: string;
  customerId: string;
  backupType: "vm" | "ct" | "host";
  groupFileIndexEnabled: boolean;
  /** Display label for the workload (e.g. "VM 100 — tenant-a"), used as the
   * modal title on the restore-points list. */
  workloadLabel: string;
}

/** Opened from a Protected Workloads row (manifest §9.4): lists that
 * workload's restore points (snapshots), each opening the same unified
 * `FileBrowserWindow` (tree + search + green/yellow browsing) the Files
 * workspace uses -- raised directly by the user after this modal still
 * opened the older always-live `FileBrowserPanel` on its own, predating
 * that redesign, with no catalog/search/mount-confirmation of its own.
 */
export function RestorePointsModal({
  open,
  onClose,
  groupId,
  customerId,
  backupType,
  groupFileIndexEnabled,
  workloadLabel,
}: RestorePointsModalProps) {
  const { t } = useTranslation();
  const [browsingSnapshotId, setBrowsingSnapshotId] = useState<string | null>(null);
  const [path, setPath] = useState("/");
  const [mountConfirmed, setMountConfirmed] = useState(false);

  useEffect(() => {
    if (open) {
      setBrowsingSnapshotId(null);
      setPath("/");
      setMountConfirmed(false);
    }
  }, [open, groupId]);

  const snapshotsQuery = useQuery({
    queryKey: ["snapshots", groupId],
    queryFn: () => inventoryApi.snapshots(groupId),
    enabled: open,
  });

  const statusQuery = useQuery({
    queryKey: ["file-index-snapshot-status", groupId],
    queryFn: () => fileIndexApi.snapshotStatus(groupId),
    enabled: open,
  });
  const indexedIds = new Set(statusQuery.data?.indexed_snapshot_ids ?? []);

  // A 404 means "never configured" -- treated as disabled, not an error,
  // same as the Files workspace's own customer-level check.
  const fileIndexSettingsQuery = useQuery({
    queryKey: ["file-index-settings", customerId],
    queryFn: () => fileIndexSettingsApi.get(customerId),
    enabled: open && customerId !== "",
    retry: false,
  });
  const indexingEnabled =
    fileIndexSettingsQuery.isSuccess && fileIndexSettingsQuery.data.enabled && groupFileIndexEnabled;

  const handleBrowse = (snapshotId: string) => {
    setBrowsingSnapshotId(snapshotId);
    setPath("/");
    setMountConfirmed(false);
  };

  const handleResultSelect = (resultSnapshotId: string, resultPath: string) => {
    setBrowsingSnapshotId(resultSnapshotId);
    setPath(resultPath);
    setMountConfirmed(false);
  };

  return (
    <>
      <Modal open={open} title={t("restorePoints.title", { workload: workloadLabel })} onClose={onClose}>
        {snapshotsQuery.isLoading && <p className="mutedText">{t("common.loading")}</p>}

        {snapshotsQuery.isError && <InlineAlert tone="danger">{t("common.loadError")}</InlineAlert>}

        {snapshotsQuery.data && snapshotsQuery.data.length === 0 && <EmptyState title={t("restorePoints.empty")} />}

        {snapshotsQuery.data && snapshotsQuery.data.length > 0 && (
          <ul className={styles.list}>
            {snapshotsQuery.data.map((snapshot) => {
              const { tone, label } = verifyStatusPresentation(snapshot.verify_status, t);
              return (
                <li key={snapshot.id}>
                  <button type="button" className={styles.row} onClick={() => handleBrowse(snapshot.id)}>
                    <FileIcon className={styles.rowIcon} />
                    <span className={styles.rowLabel}>{new Date(snapshot.snapshot_time).toLocaleString()}</span>
                    <span className={styles.rowMeta}>
                      <span className="mutedText">{formatBytes(snapshot.size_bytes)}</span>
                      <StatusBadge tone={tone} label={label} />
                      <FolderOpenIcon className={styles.rowIcon} />
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </Modal>

      {browsingSnapshotId && (
        <FileBrowserWindow
          open
          onClose={() => setBrowsingSnapshotId(null)}
          title={workloadLabel}
          customerId={customerId}
          groupId={groupId}
          snapshotId={browsingSnapshotId}
          path={path}
          onPathChange={setPath}
          backupType={backupType}
          isIndexed={indexedIds.has(browsingSnapshotId)}
          indexingEnabled={indexingEnabled}
          snapshots={snapshotsQuery.data ?? []}
          onResultSelect={handleResultSelect}
          mountConfirmed={mountConfirmed}
          onConfirmMount={() => setMountConfirmed(true)}
        />
      )}
    </>
  );
}
