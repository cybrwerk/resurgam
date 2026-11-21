import { useTranslation } from "react-i18next";

import type { BackupSnapshotOut } from "../api/types";
import { DiskTree } from "./DiskTree";
import { EmptyState } from "./EmptyState";
import { FilesearchAssistant } from "./FilesearchAssistant";
import { Modal } from "./Modal";
import { SnapshotFileBrowser } from "./SnapshotFileBrowser";
import styles from "./FileBrowserWindow.module.css";

interface FileBrowserWindowProps {
  open: boolean;
  onClose: () => void;
  title: string;
  customerId: string;
  groupId: string;
  snapshotId: string;
  path: string;
  onPathChange: (path: string) => void;
  backupType: "vm" | "ct" | "host";
  isIndexed: boolean;
  indexingEnabled: boolean;
  snapshots: BackupSnapshotOut[];
  onResultSelect: (snapshotId: string, path: string) => void;
  mountConfirmed: boolean;
  onConfirmMount: () => void;
}

/** The actual browsing experience (tree + search + content), detached
 * into its own window -- raised directly by the user after the Files
 * page felt overloaded with the calendar, the tree/browser/search, and
 * the indexing job history all stacked on one page at once. Opened once
 * a snapshot is picked on the calendar; the page itself goes back to
 * just picking a customer/group/restore point and tracking indexing
 * jobs, both lighter-weight concerns than active file browsing.
 */
export function FileBrowserWindow({
  open,
  onClose,
  title,
  customerId,
  groupId,
  snapshotId,
  path,
  onPathChange,
  backupType,
  isIndexed,
  indexingEnabled,
  snapshots,
  onResultSelect,
  mountConfirmed,
  onConfirmMount,
}: FileBrowserWindowProps) {
  const { t } = useTranslation();
  // Only a VM backup has to be mounted to be browsed live
  // (proxmox-file-restore boots a micro-VM). Containers and hosts are read
  // straight from the PBS catalog -- fast, no mount, no index needed -- so
  // gating them behind "Run" was a regression (user report, 2026-10-07).
  const needsMountConfirmation = backupType === "vm" && !isIndexed && !mountConfirmed;

  return (
    <Modal open={open} onClose={onClose} title={title} size="xlarge">
      {needsMountConfirmation ? (
        <div className={styles.mountPrompt}>
          <EmptyState
            title={t("fileWorkspace.mountPromptTitle")}
            description={t("fileWorkspace.mountPromptDescription")}
          />
          <button type="button" className="button buttonPrimary" onClick={onConfirmMount}>
            {t("fileWorkspace.runMount")}
          </button>
        </div>
      ) : (
        <div className={styles.workspace}>
          <div className={styles.treePanel}>
            <DiskTree
              groupId={groupId}
              snapshotId={snapshotId}
              backupType={backupType}
              isIndexed={isIndexed}
              selectedPath={path}
              onSelectPath={onPathChange}
            />
          </div>
          <div className={styles.browserPanel}>
            <FilesearchAssistant
              customerId={customerId}
              groupId={groupId}
              snapshots={snapshots}
              onResultSelect={onResultSelect}
            />
            <SnapshotFileBrowser
              customerId={customerId}
              groupId={groupId}
              snapshotId={snapshotId}
              path={path}
              onPathChange={onPathChange}
              isIndexed={isIndexed}
              indexingEnabled={indexingEnabled}
            />
          </div>
        </div>
      )}
    </Modal>
  );
}
