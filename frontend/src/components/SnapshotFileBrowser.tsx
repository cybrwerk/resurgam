import { FileBrowserPanel } from "./FileBrowserPanel";
import { FileIndexBrowserPanel } from "./FileIndexBrowserPanel";

interface SnapshotFileBrowserProps {
  customerId: string;
  groupId: string;
  snapshotId: string;
  path: string;
  onPathChange: (path: string) => void;
  isIndexed: boolean;
  indexingEnabled: boolean;
}

/** The Unified File Workspace's content pane: a thin switch between the
 * two existing, unchanged browsing mechanisms, both driven by the same
 * lifted `path` -- green (`isIndexed`) reads the persistent catalog
 * (instant, searchable); yellow falls back to live on-demand PBS
 * browsing (mount-on-demand), exactly matching the calendar's own
 * green/yellow meaning. Neither panel needs to know about the other or
 * about this switch -- each is just handed a controlled path.
 */
export function SnapshotFileBrowser({
  customerId,
  groupId,
  snapshotId,
  path,
  onPathChange,
  isIndexed,
  indexingEnabled,
}: SnapshotFileBrowserProps) {
  if (isIndexed) {
    return (
      <FileIndexBrowserPanel
        customerId={customerId}
        groupId={groupId}
        snapshotId={snapshotId}
        indexingEnabled={indexingEnabled}
        path={path}
        onPathChange={onPathChange}
      />
    );
  }
  return (
    <FileBrowserPanel
      groupId={groupId}
      snapshotId={snapshotId}
      resetKey={snapshotId}
      path={path}
      onPathChange={onPathChange}
    />
  );
}
