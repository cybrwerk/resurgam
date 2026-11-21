import { type ReactNode, useEffect, useId, useRef } from "react";
import { useTranslation } from "react-i18next";

import styles from "./ConfirmActionDialog.module.css";

interface ConfirmActionDialogProps {
  open: boolean;
  title: string;
  body: string;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  /** Extra controls shown under the body (e.g. an option for the action). */
  children?: ReactNode;
  /** Disables the confirm button, e.g. while an extra control is invalid. */
  confirmDisabled?: boolean;
}

/** Every destructive/privileged action confirms with the object's name and
 * effect spelled out — manifest §26.5: "Destructive Actions verwenden
 * Bestätigungsdialoge mit Objektnamen und Auswirkung." Uses the native
 * <dialog> element for built-in focus trapping and Escape-to-close. */
export function ConfirmActionDialog({
  open,
  title,
  body,
  confirmLabel,
  danger = false,
  onConfirm,
  onCancel,
  children,
  confirmDisabled = false,
}: ConfirmActionDialogProps) {
  const { t } = useTranslation();
  const ref = useRef<HTMLDialogElement>(null);
  // Unique per dialog: several can be mounted on one page, and a shared id
  // would give them all the first one's title as their accessible name.
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
    <dialog ref={ref} className={styles.dialog} onCancel={onCancel} aria-labelledby={titleId}>
      <h2 id={titleId} className={styles.dialogTitle}>
        {title}
      </h2>
      <p className={styles.dialogBody}>{body}</p>
      {children && <div className={styles.dialogExtra}>{children}</div>}
      <div className={styles.dialogActions}>
        <button type="button" className="button" onClick={onCancel}>
          {t("common.cancel")}
        </button>
        <button
          type="button"
          className={danger ? "button buttonDanger" : "button buttonPrimary"}
          onClick={onConfirm}
          disabled={confirmDisabled}
        >
          {confirmLabel ?? t("common.confirm")}
        </button>
      </div>
    </dialog>
  );
}
