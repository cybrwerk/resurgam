import { useId } from "react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import type { JobCapabilitiesOut } from "../api/types";
import { InlineAlert } from "./InlineAlert";
import styles from "./JobRowActions.module.css";

/** Why write actions are unavailable, in one short sentence naming the PBS
 * privilege(s) the token lacks (from the capabilities endpoint's
 * `missing_write_privileges`). Null while capabilities are still loading
 * or when writing is allowed, so nothing flickers in. */
export function writeBlockedReason(capabilities: JobCapabilitiesOut | undefined, t: TFunction): string | null {
  if (!capabilities || capabilities.can_write) return null;
  const missing = capabilities.missing_write_privileges ?? [];
  return missing.length > 0
    ? t("pbsJobs.requiresPrivilege", { privileges: missing.join(", ") })
    : t("common.capabilitiesReadOnly");
}

export function useWriteBlockedReason(capabilities: JobCapabilitiesOut | undefined): string | null {
  const { t } = useTranslation();
  return writeBlockedReason(capabilities, t);
}

/** Edit + Delete row actions for a PBS job (prune / verify / sync / GC
 * schedule). Both buttons are always rendered so the actions stay
 * discoverable; when the token can't write, they're disabled and a short
 * muted hint underneath says which privilege to grant on PBS. */
export function JobRowActions({
  canWrite,
  blockedReason,
  onEdit,
  onDelete,
  deleteLabel,
  deleteDisabled = false,
}: {
  canWrite: boolean;
  blockedReason: string | null;
  onEdit: () => void;
  onDelete: () => void;
  deleteLabel?: string;
  deleteDisabled?: boolean;
}) {
  const { t } = useTranslation();
  const hintId = useId();
  const describedBy = !canWrite && blockedReason ? hintId : undefined;

  return (
    <span className={styles.actions}>
      <span className={styles.buttons}>
        <button type="button" className="button" onClick={onEdit} disabled={!canWrite} aria-describedby={describedBy}>
          {t("common.edit")}
        </button>
        <button
          type="button"
          className="button buttonDanger"
          onClick={onDelete}
          disabled={!canWrite || deleteDisabled}
          aria-describedby={describedBy}
        >
          {deleteLabel ?? t("common.delete")}
        </button>
      </span>
      {describedBy && (
        <span id={hintId} className={`mutedText ${styles.hint}`}>
          {blockedReason}
        </span>
      )}
    </span>
  );
}

export interface JobFeedback {
  tone: "info" | "danger";
  message: string;
}

/** Result line for a save/delete on a Jobs page: success as an info note,
 * failure as an alert carrying PBS's own error text. */
export function JobFeedbackAlert({ feedback }: { feedback: JobFeedback | null }) {
  if (!feedback) return null;
  return (
    <div className={styles.feedback}>
      <InlineAlert tone={feedback.tone}>{feedback.message}</InlineAlert>
    </div>
  );
}

export function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}
