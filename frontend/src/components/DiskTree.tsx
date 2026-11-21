import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { fileIndexApi, fileRestoreApi } from "../api/resources";
import type { FileRestoreJobOut } from "../api/types";
import { formatArchiveLabel } from "../lib/formatArchiveLabel";
import { ChevronDownIcon, FolderIcon, FolderOpenIcon } from "./icons";
import styles from "./DiskTree.module.css";

interface DirectoryChild {
  path: string;
  name: string;
}

const LIVE_POLL_INTERVAL_MS = 1000;
const IN_FLIGHT_STATUSES: FileRestoreJobOut["status"][] = ["pending", "running"];

/** Polls a "list" file-restore job to completion and returns its
 * directories -- the live-browse equivalent of `fileIndexApi.browse`,
 * inlined here (not `FileBrowserPanel`'s own job-polling, which is a
 * `useQuery`+`useEffect` pair driven by component state) since a tree
 * node just needs one straight async answer, not incremental status.
 */
async function fetchLiveDirectories(groupId: string, snapshotId: string, path: string): Promise<DirectoryChild[]> {
  let job = await fileRestoreApi.createJob(groupId, snapshotId, "list", path);
  while (IN_FLIGHT_STATUSES.includes(job.status)) {
    await new Promise((resolve) => setTimeout(resolve, LIVE_POLL_INTERVAL_MS));
    job = await fileRestoreApi.getJob(job.id);
  }
  if (job.status !== "completed") {
    throw new Error(job.error_message ?? "listing failed");
  }
  return (job.result ?? []).filter((entry) => entry.type === "directory").map((entry) => ({ path: entry.path, name: entry.name }));
}

/** One-level directory listing for the tree, from whichever backend the
 * snapshot's index status calls for. Uses the *exact same* query-key
 * shape as `FileIndexBrowserPanel`'s own green-mode browse query
 * (`["file-index-browse", groupId, snapshotId, path]`) so TanStack
 * Query's cache serves both from one fetch when they browse the same
 * path -- no explicit cache-sharing code, just a matching key.
 */
function useDirectoryChildren(
  groupId: string,
  snapshotId: string,
  path: string,
  isIndexed: boolean,
  enabled: boolean,
): { children: DirectoryChild[]; isLoading: boolean; isError: boolean } {
  const indexQuery = useQuery({
    queryKey: ["file-index-browse", groupId, snapshotId, path],
    queryFn: () => fileIndexApi.browse({ backupGroupId: groupId, backupSnapshotId: snapshotId, path }),
    enabled: isIndexed && enabled,
  });
  const liveQuery = useQuery({
    queryKey: ["disk-tree-live", groupId, snapshotId, path],
    queryFn: () => fetchLiveDirectories(groupId, snapshotId, path),
    enabled: !isIndexed && enabled,
  });

  if (isIndexed) {
    const children = (indexQuery.data ?? [])
      .filter((entry) => entry.type === "directory")
      .map((entry) => ({ path: entry.path, name: entry.filename }));
    return { children, isLoading: indexQuery.isLoading, isError: indexQuery.isError };
  }
  return { children: liveQuery.data ?? [], isLoading: liveQuery.isLoading, isError: liveQuery.isError };
}

interface TreeNodeProps {
  groupId: string;
  snapshotId: string;
  isIndexed: boolean;
  path: string;
  label: string;
  depth: number;
  selectedPath: string;
  onSelectPath: (path: string) => void;
}

function TreeNode({ groupId, snapshotId, isIndexed, path, label, depth, selectedPath, onSelectPath }: TreeNodeProps) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const { children, isLoading, isError } = useDirectoryChildren(groupId, snapshotId, path, isIndexed, expanded);
  const isSelected = path === selectedPath;

  return (
    <li>
      <button
        type="button"
        className={`${styles.node} ${isSelected ? styles.nodeSelected : ""}`}
        style={{ paddingLeft: `${0.5 + depth * 1}rem` }}
        onClick={() => {
          setExpanded((v) => !v);
          onSelectPath(path);
        }}
      >
        <ChevronDownIcon className={`${styles.chevron} ${expanded ? styles.chevronExpanded : ""}`} />
        {expanded ? <FolderOpenIcon className={styles.nodeIcon} /> : <FolderIcon className={styles.nodeIcon} />}
        <span className={styles.nodeLabel}>{label}</span>
      </button>
      {expanded && (
        <ul className={styles.children}>
          {isLoading && (
            <li className={styles.leafMessage} style={{ paddingLeft: `${0.5 + (depth + 1) * 1}rem` }}>
              {t("common.loading")}
            </li>
          )}
          {isError && (
            <li className={`${styles.leafMessage} ${styles.leafError}`} style={{ paddingLeft: `${0.5 + (depth + 1) * 1}rem` }}>
              {t("common.loadError")}
            </li>
          )}
          {!isLoading && !isError && children.length === 0 && (
            <li className={styles.leafMessage} style={{ paddingLeft: `${0.5 + (depth + 1) * 1}rem` }}>
              {t("fileWorkspace.noSubfolders")}
            </li>
          )}
          {children.map((child) => (
            <TreeNode
              key={child.path}
              groupId={groupId}
              snapshotId={snapshotId}
              isIndexed={isIndexed}
              path={child.path}
              label={child.name}
              depth={depth + 1}
              selectedPath={selectedPath}
              onSelectPath={onSelectPath}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

interface DiskTreeProps {
  groupId: string;
  snapshotId: string;
  backupType: "vm" | "ct" | "host";
  isIndexed: boolean;
  selectedPath: string;
  onSelectPath: (path: string) => void;
}

/** Left-rail lazy-expand tree over a snapshot's disks/partitions/folders
 * (VM) or root filesystem (CT/host) -- raised directly by the user.
 * Neither backend exposes a single "whole tree" call (PBS's own
 * `proxmox-file-restore` walks one level per subprocess call, and the
 * persistent index has no schema tag distinguishing a disk-archive level
 * from a partition level from a real folder), so this is built the way
 * real file-tree UIs work: each node fetches its own children only once
 * expanded. For a VM, the root level is genuinely the disk archives
 * (`drive-scsi0.img.fidx`, etc.) and one level under each is the
 * partition selector -- both render identically (deeper indentation is
 * the only visual distinction, since there's no schema tag to render
 * differently). For CT/host there's no disk/partition concept at all --
 * the root level *is* the real filesystem root, with zero special-casing
 * needed here beyond the cosmetic heading below.
 */
export function DiskTree({ groupId, snapshotId, backupType, isIndexed, selectedPath, onSelectPath }: DiskTreeProps) {
  const { t } = useTranslation();
  const { children: roots, isLoading, isError } = useDirectoryChildren(groupId, snapshotId, "/", isIndexed, true);

  return (
    <div className={styles.panel}>
      <p className="label">{backupType === "vm" ? t("fileWorkspace.disksHeading") : t("fileWorkspace.rootHeading")}</p>
      {isLoading && <p className="mutedText">{t("common.loading")}</p>}
      {isError && <p className="mutedText">{t("common.loadError")}</p>}
      {!isLoading && !isError && roots.length === 0 && <p className="mutedText">{t("fileWorkspace.noSubfolders")}</p>}
      <ul className={styles.root}>
        {roots.map((root) => (
          <TreeNode
            key={root.path}
            groupId={groupId}
            snapshotId={snapshotId}
            isIndexed={isIndexed}
            path={root.path}
            label={backupType === "vm" ? formatArchiveLabel(root.name) : root.name}
            depth={0}
            selectedPath={selectedPath}
            onSelectPath={onSelectPath}
          />
        ))}
      </ul>
    </div>
  );
}
