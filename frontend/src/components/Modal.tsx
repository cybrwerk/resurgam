import { useEffect, useId, useRef, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { XCircleIcon } from "./icons";
import styles from "./Modal.module.css";

interface ModalProps {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  /** "large" for content-heavy forms (a wide two-column grid, several
   * sections) that feel cramped at the default width -- raised directly
   * by the user after a real form needed both vertical *and* horizontal
   * scrolling despite plenty of free screen space. "xlarge" is for a
   * whole detached sub-workspace opened in a window (e.g. the File
   * Browser's tree + search + content panes) rather than a form.
   * Defaults to the standard width for everything else. */
  size?: "default" | "large" | "xlarge";
}

/** A general-purpose "window" for viewing/editing a single object's
 * details, opened by clicking its row — distinct from
 * ConfirmActionDialog, which is fixed to a title+body confirmation
 * shape. Uses the native <dialog> element for the same reasons
 * (built-in focus trapping, Escape-to-close, backdrop). */
export function Modal({ open, title, onClose, children, size = "default" }: ModalProps) {
  const { t } = useTranslation();
  const ref = useRef<HTMLDialogElement>(null);
  // A hardcoded id broke `aria-labelledby` the moment two Modals were
  // open at once (`RestorePointsModal` now layers a `FileBrowserWindow`
  // on top of itself) -- every `<dialog>` shared the same
  // `id="modal-title"`, so the second one's accessible name resolved to
  // the first one's title instead of its own.
  const titleId = useId();

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.showModal();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  return (
    <dialog
      ref={ref}
      className={
        size === "large"
          ? `${styles.dialog} ${styles.dialogLarge}`
          : size === "xlarge"
            ? `${styles.dialog} ${styles.dialogXLarge}`
            : styles.dialog
      }
      onCancel={onClose}
      aria-labelledby={titleId}
    >
      <div className={styles.header}>
        <h2 id={titleId} className={styles.title}>
          {title}
        </h2>
        <button type="button" className="button buttonGhost" onClick={onClose} aria-label={t("common.close")}>
          <XCircleIcon />
        </button>
      </div>
      <div className={styles.body}>{children}</div>
    </dialog>
  );
}
