import { useTranslation } from "react-i18next";

import type { FileRestoreJobOut } from "../api/types";
import type { ArchiveDownload } from "../hooks/useSnapshotArchiveDownloads";
import { formatBytes } from "../lib/formatBytes";
import { StatusBadge, type StatusTone } from "./StatusBadge";
import styles from "./ArchiveDownloadList.module.css";

const TONE: Record<FileRestoreJobOut["status"], StatusTone> = {
  pending: "info",
  running: "info",
  completed: "ok",
  failed: "danger",
  expired: "warning",
  cancelled: "unknown",
};

interface ArchiveDownloadListProps {
  downloads: ArchiveDownload[];
  onSaveAgain: (jobId: string) => void;
  onDismiss: (jobId: string) => void;
}

/** The folder / multi-selection ZIP downloads started from a file browser: state, progress while
 * the worker runs, then the ZIP's size and -- the part worth reading -- what was skipped or
 * renamed. `aria-live` so a screen reader hears when a ZIP is ready or failed. */
export function ArchiveDownloadList({ downloads, onSaveAgain, onDismiss }: ArchiveDownloadListProps) {
  const { t } = useTranslation();
  if (downloads.length === 0) return null;

  return (
    <ul className={styles.list} aria-label={t("fileRestore.archive.listLabel")} aria-live="polite">
      {downloads.map(({ jobId, label, job, saveFailed, loadError }) => {
        const status = job?.status ?? "pending";
        const report = job?.archive_report ?? null;
        const progress = job?.progress ?? null;
        // A job that can't be read any more (e.g. 404 after a restart) can still be dismissed.
        const terminal = (status !== "pending" && status !== "running") || (loadError !== null && !job);
        return (
          <li key={jobId} className={styles.item}>
            <div className={styles.header}>
              <StatusBadge tone={TONE[status]} label={t(`fileRestore.archive.status.${status}`)} />
              <span className={styles.label}>{job?.result_filename ?? label}</span>
              <span className={styles.actions}>
                {status === "completed" && job?.content_available && (
                  <button type="button" className="button buttonGhost" onClick={() => onSaveAgain(jobId)}>
                    {t("fileRestore.archive.downloadAgain")}
                  </button>
                )}
                {terminal && (
                  <button
                    type="button"
                    className="button buttonGhost"
                    onClick={() => onDismiss(jobId)}
                    aria-label={t("fileRestore.archive.dismissLabel", { name: job?.result_filename ?? label })}
                  >
                    {t("fileRestore.archive.dismiss")}
                  </button>
                )}
              </span>
            </div>

            {status === "running" && progress && (
              <p className={`mutedText ${styles.detail}`}>
                {t(`fileRestore.archive.progress.${progress.phase}`, {
                  done: progress.entries_done,
                  total: progress.entries_total,
                  size: formatBytes(progress.bytes_done),
                })}
              </p>
            )}
            {status === "pending" && !loadError && (
              <p className={`mutedText ${styles.detail}`}>{t("fileRestore.archive.queued")}</p>
            )}
            {loadError && (
              <p className={`${styles.detail} ${styles.error}`}>{t("fileRestore.archive.loadError", { error: loadError })}</p>
            )}

            {status === "completed" && (
              <p className={styles.detail}>
                <span title={job?.result_size_bytes != null ? `${job.result_size_bytes} B` : undefined}>
                  {t("fileRestore.archive.summary", {
                    size: formatBytes(job?.result_size_bytes ?? null),
                    files: report?.file_count ?? 0,
                    total: formatBytes(report?.total_bytes ?? null),
                  })}
                </span>
                {!job?.content_available && <> {t("fileRestore.archive.contentGone")}</>}
              </p>
            )}
            {saveFailed && <p className={`${styles.detail} ${styles.error}`}>{t("fileRestore.archive.saveFailed")}</p>}

            {(status === "failed" || status === "expired") && (
              <p className={`${styles.detail} ${styles.error}`}>
                {job?.error_message ?? t("fileRestore.archive.expired")}
              </p>
            )}

            {report && report.skipped_count > 0 && (
              <details className={styles.details}>
                <summary>{t("fileRestore.archive.skipped", { count: report.skipped_count })}</summary>
                <ul className={styles.entryList}>
                  {report.skipped.map((entry) => (
                    <li key={entry.path}>
                      <code>{entry.path}</code> — {entry.reason}
                    </li>
                  ))}
                </ul>
                {report.skipped_count > report.skipped.length && (
                  <p className="mutedText">
                    {t("fileRestore.archive.moreNotListed", { count: report.skipped_count - report.skipped.length })}
                  </p>
                )}
              </details>
            )}
            {report && report.renamed_count > 0 && (
              <details className={styles.details}>
                <summary>{t("fileRestore.archive.renamed", { count: report.renamed_count })}</summary>
                <p className="mutedText">{t("fileRestore.archive.renamedHint")}</p>
                <ul className={styles.entryList}>
                  {report.renamed.map((entry) => (
                    <li key={entry.path}>
                      <code>{entry.path}</code> → <code>{entry.archive_name}</code>
                    </li>
                  ))}
                </ul>
                {report.renamed_count > report.renamed.length && (
                  <p className="mutedText">
                    {t("fileRestore.archive.moreNotListed", { count: report.renamed_count - report.renamed.length })}
                  </p>
                )}
              </details>
            )}
          </li>
        );
      })}
    </ul>
  );
}
