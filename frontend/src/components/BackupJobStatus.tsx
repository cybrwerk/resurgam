import { useTranslation } from "react-i18next";

import type { VzdumpJobOut } from "../api/types";
import { StatusBadge } from "./StatusBadge";
import styles from "./BackupJobStatus.module.css";

interface BackupJobStatusProps {
  job: VzdumpJobOut;
  canWrite: boolean;
  onCancelTimer: () => void;
  /** Opens the "change timer" dialog; the button is hidden when omitted. */
  onChangeTimer?: () => void;
  cancelPending?: boolean;
}

/** Status cell for a PVE backup job: active/disabled, plus -- for a job
 * disabled "for N hours" -- when RESURGAM will enable it again, a failed
 * attempt if any, and ways to change the timer or cancel it (the job then
 * stays disabled). */
export function BackupJobStatus({
  job,
  canWrite,
  onCancelTimer,
  onChangeTimer,
  cancelPending = false,
}: BackupJobStatusProps) {
  const { t } = useTranslation();
  if (job.enabled) return <StatusBadge tone="ok" label={t("common.statusActive")} />;
  if (!job.reenable_at) return <StatusBadge tone="unknown" label={t("common.statusDisabled")} />;

  const at = new Date(job.reenable_at);
  return (
    <span className={styles.stack}>
      <StatusBadge tone={job.reenable_last_error ? "warning" : "info"} label={t("backupJobReenable.badge")} />
      <span className="mutedText" title={at.toLocaleString()}>
        {t("backupJobReenable.reenablesAt", { time: at.toLocaleString() })}
      </span>
      {job.reenable_last_error && (
        <span className={styles.error} title={job.reenable_last_error}>
          {t("backupJobReenable.lastAttemptFailed", { error: job.reenable_last_error })}
        </span>
      )}
      {canWrite && (
        <span className={styles.timerActions}>
          {onChangeTimer && (
            <button type="button" className={`button ${styles.cancel}`} onClick={onChangeTimer} disabled={cancelPending}>
              {t("backupJobReenable.changeTimer")}
            </button>
          )}
          <button type="button" className={`button ${styles.cancel}`} onClick={onCancelTimer} disabled={cancelPending}>
            {t("backupJobReenable.cancelTimer")}
          </button>
        </span>
      )}
    </span>
  );
}
