import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { pveClustersApi } from "../api/resources";
import type { BackupRunOut, BackupRunTaskOut } from "../api/types";
import { CopyButton } from "./CopyButton";
import { InlineAlert } from "./InlineAlert";
import { Modal } from "./Modal";
import { Spinner } from "./Spinner";
import { StatusBadge, type StatusTone } from "./StatusBadge";
import styles from "./BackupRunViewer.module.css";

const POLL_MS = 2000;
const MAX_LINES = 2000;

function outcome(status: string | null, exitstatus: string | null): { tone: StatusTone; key: string } {
  if (status !== "stopped") return { tone: "info", key: "running" };
  if (exitstatus === "OK") return { tone: "ok", key: "ok" };
  if (exitstatus?.startsWith("WARNINGS")) return { tone: "warning", key: "warnings" };
  return { tone: "danger", key: "failed" };
}

/** One vzdump task: status badge and its log, appended live. */
function TaskProgress({ clusterId, task }: { clusterId: string; task: BackupRunTaskOut }) {
  const { t } = useTranslation();
  const [lines, setLines] = useState<{ n: number; t: string }[]>([]);
  const [status, setStatus] = useState<string | null>("running");
  const [exitstatus, setExitstatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const logRef = useRef<HTMLPreElement>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    let start = 0;
    const tick = async () => {
      try {
        const progress = await pveClustersApi.taskProgress(clusterId, task.node, task.upid, start);
        if (cancelled) return;
        setError(null);
        if (progress.lines.length > 0) {
          setLines((previous) => [...previous, ...progress.lines].slice(-MAX_LINES));
        }
        start = progress.next_start;
        setStatus(progress.status);
        setExitstatus(progress.exitstatus);
        // A full page means more lines are waiting: fetch them right away,
        // and keep going after `stopped` until the log is drained.
        if (progress.more) {
          timer = window.setTimeout(tick, 0);
          return;
        }
        if (progress.status === "stopped") return;
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : t("common.loadError"));
      }
      timer = window.setTimeout(tick, POLL_MS);
    };
    void tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [clusterId, task.node, task.upid, t]);

  // Follow the end of the log while it grows.
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines.length]);

  const result = outcome(status, exitstatus);
  return (
    <section className={styles.task} data-testid={`backup-task-${task.node}`}>
      <header className={styles.taskHeader}>
        <strong>{t("backupRun.node", { node: task.node })}</strong>
        {task.vmids && <span className="mutedText">{t("backupRun.guests", { vmids: task.vmids.join(", ") })}</span>}
        {/* Only the status is a live region -- announcing every log line would flood screen readers. */}
        <span className={styles.badge} aria-live="polite">
          {result.key === "running" && <Spinner />}
          <StatusBadge tone={result.tone} label={t(`backupRun.state.${result.key}`)} />
        </span>
      </header>
      {exitstatus && result.key !== "ok" && <p className="mutedText">{exitstatus}</p>}
      {error && <InlineAlert>{error}</InlineAlert>}
      <pre ref={logRef} className={styles.log}>
        {lines.length > 0 ? lines.map((line) => line.t).join("\n") : t("backupRun.waitingForLog")}
      </pre>
      <div className={styles.upidRow}>
        <span className={`mutedText ${styles.upid}`}>
          {t("backupRun.upidLabel")}: <code>{task.upid}</code>
        </span>
        <CopyButton text={task.upid} label={t("backupRun.copyUpid")} />
      </div>
    </section>
  );
}

/** Backup Jobs -> Run now: follows every started vzdump task live. */
export function BackupRunViewer({
  clusterId,
  run,
  jobName,
  onClose,
}: {
  clusterId: string;
  run: BackupRunOut;
  /** The job's comment, else its id. */
  jobName?: string;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const missing = run.skipped.filter((s) => s.reason === "missing").map((s) => s.vmid);
  const otherNode = run.skipped.filter((s) => s.reason === "other_node").map((s) => s.vmid);
  return (
    <Modal open size="large" title={t("backupRun.title", { job: jobName ?? run.job_id })} onClose={onClose}>
      <p className="mutedText">{t("backupRun.intro")}</p>
      {missing.length > 0 && <InlineAlert tone="info">{t("backupRun.skipped", { vmids: missing.join(", ") })}</InlineAlert>}
      {otherNode.length > 0 && (
        <InlineAlert tone="info">{t("backupRun.skippedOtherNode", { vmids: otherNode.join(", ") })}</InlineAlert>
      )}
      <div className={styles.tasks}>
        {run.tasks.map((task) => (
          <TaskProgress key={task.upid} clusterId={clusterId} task={task} />
        ))}
      </div>
    </Modal>
  );
}
