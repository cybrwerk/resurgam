import { FileBrowserPanel } from "./FileBrowserPanel";
import { Modal } from "./Modal";

interface FileBrowserModalProps {
  open: boolean;
  onClose: () => void;
  groupId: string;
  snapshotId: string;
  title: string;
}

/** Opens `FileBrowserPanel` (manifest §13) in a modal from a snapshot row
 * in the Datastores tree. `FileRecoveryPage` renders the same panel
 * inline instead, after its own customer/group/snapshot pickers. */
export function FileBrowserModal({ open, onClose, groupId, snapshotId, title }: FileBrowserModalProps) {
  return (
    <Modal open={open} title={title} onClose={onClose}>
      {open && <FileBrowserPanel groupId={groupId} snapshotId={snapshotId} resetKey={snapshotId} />}
    </Modal>
  );
}
