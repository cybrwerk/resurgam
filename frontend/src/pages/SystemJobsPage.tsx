import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { fileIndexApi, fileRestoreApi, restorePlansApi } from "../api/resources";
import type {
  FileIndexJobOut,
  FileIndexJobStatus,
  FileRestoreJobOut,
  FileRestoreJobStatus,
  RestorePlanExecutionStatus,
  RestorePlanOut,
} from "../api/types";
import { ConfirmActionDialog } from "../components/ConfirmActionDialog";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { InlineAlert } from "../components/InlineAlert";
import { Modal } from "../components/Modal";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { QueryState } from "../components/QueryState";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { TabPanel, Tabs, useTabParam } from "../components/Tabs";
import { formatBytes } from "../lib/formatBytes";
import styles from "./SystemJobsPage.module.css";

const POLL_INTERVAL_MS = 5000;

const FILE_RESTORE_STATUS_TONE: Record<FileRestoreJobStatus, StatusTone> = {
  pending: "info",
  running: "info",
  completed: "ok",
  failed: "danger",
  expired: "unknown",
  cancelled: "unknown",
};

const FILE_INDEX_STATUS_TONE: Record<FileIndexJobStatus, StatusTone> = {
  pending: "info",
  running: "info",
  paused: "warning",
  completed: "ok",
  failed: "danger",
  cancelled: "unknown",
};

const EXECUTION_STATUS_TONE: Record<RestorePlanExecutionStatus, StatusTone> = {
  pending_approval: "warning",
  approved: "info",
  rejected: "danger",
  restore_requested: "info",
  restore_running: "info",
  restore_failed: "danger",
  finalizing: "info",
  completed: "ok",
  finalize_failed: "danger",
  // Instant Recovery (ADR 0030)
  queued: "info",
  mapping: "info",
  starting: "info",
  running_from_backup: "warning",
  moving_disks: "warning",
  finishing: "info",
  discarding: "info",
  discarded: "unknown",
  failed: "danger",
};

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

// --- File Restore jobs -----------------------------------------------------

function FileRestoreJobDetailModal({ job, onClose }: { job: FileRestoreJobOut; onClose: () => void }) {
  const { t } = useTranslation();
  return (
    <Modal open title={t("systemJobs.fileRestoreJobDetailTitle")} onClose={onClose}>
      <dl className={`detailGrid ${styles.modalDetails}`}>
        <dt>{t("systemJobs.workload")}</dt>
        <dd>{job.workload_label}</dd>
        <dt>{t("systemJobs.jobType")}</dt>
        <dd>{job.job_type}</dd>
        <dt>{t("systemJobs.path")}</dt>
        <dd>{job.path}</dd>
        <dt>{t("common.status")}</dt>
        <dd>
          <StatusBadge tone={FILE_RESTORE_STATUS_TONE[job.status]} label={t(`systemJobs.fileRestoreStatus.${job.status}`)} />
        </dd>
        {job.result_filename && (
          <>
            <dt>{t("systemJobs.resultFilename")}</dt>
            <dd>{job.result_filename}</dd>
          </>
        )}
        {job.paths && job.paths.length > 0 && (
          <>
            <dt>{t("systemJobs.selectedPaths")}</dt>
            <dd>
              <ul className={styles.pathList}>
                {job.paths.map((p) => (
                  <li key={p}>
                    <code>{p}</code>
                  </li>
                ))}
              </ul>
            </dd>
          </>
        )}
        {job.result_size_bytes != null && (
          <>
            <dt>{t("systemJobs.resultSize")}</dt>
            <dd title={`${job.result_size_bytes} B`}>{formatBytes(job.result_size_bytes)}</dd>
          </>
        )}
        {job.archive_report && (
          <>
            <dt>{t("systemJobs.archiveContents")}</dt>
            <dd>
              {t("systemJobs.archiveContentsValue", {
                files: job.archive_report.file_count,
                skipped: job.archive_report.skipped_count,
              })}
            </dd>
          </>
        )}
        {job.result && (
          <>
            <dt>{t("systemJobs.resultEntries")}</dt>
            <dd>{job.result.length}</dd>
          </>
        )}
        {job.error_code && (
          <>
            <dt>{t("systemJobs.errorCode")}</dt>
            <dd>{job.error_code}</dd>
          </>
        )}
        {job.error_message && (
          <>
            <dt>{t("systemJobs.errorMessage")}</dt>
            <dd>{job.error_message}</dd>
          </>
        )}
      </dl>
    </Modal>
  );
}

function FileRestoreJobsSection() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [detailJob, setDetailJob] = useState<FileRestoreJobOut | null>(null);

  const jobsQuery = useQuery({
    queryKey: ["system-jobs", "file-restore"],
    queryFn: () => fileRestoreApi.listJobs({ limit: 200 }),
    refetchInterval: POLL_INTERVAL_MS,
  });

  const cancelMutation = useMutation({
    mutationFn: (jobId: string) => fileRestoreApi.cancelJob(jobId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["system-jobs", "file-restore"] }),
  });

  const columns: DataTableColumn<FileRestoreJobOut>[] = [
    {
      key: "status",
      header: t("common.status"),
      render: (row) => <StatusBadge tone={FILE_RESTORE_STATUS_TONE[row.status]} label={t(`systemJobs.fileRestoreStatus.${row.status}`)} />,
    },
    { key: "workload", header: t("systemJobs.workload"), render: (row) => row.workload_label },
    { key: "type", header: t("systemJobs.jobType"), render: (row) => row.job_type },
    { key: "path", header: t("systemJobs.path"), render: (row) => row.path },
    {
      key: "created",
      header: t("common.createdAt"),
      render: (row) => new Date(row.created_at).toLocaleString(),
      sortValue: (row) => row.created_at,
    },
    {
      key: "actions",
      header: t("common.actions"),
      render: (row) => (
        <div className={styles.rowActions}>
          <button type="button" className="button buttonGhost" onClick={() => setDetailJob(row)}>
            {t("systemJobs.details")}
          </button>
          {row.status === "pending" || row.status === "running" ? (
            <button
              type="button"
              className="button buttonDanger"
              onClick={() => cancelMutation.mutate(row.id)}
              disabled={cancelMutation.isPending}
            >
              {t("systemJobs.cancel")}
            </button>
          ) : null}
        </div>
      ),
    },
  ];

  return (
    <Panel testId="file-restore-jobs" padded={false}>
      <QueryState query={jobsQuery} isEmpty={(data) => data.length === 0}>
        {(jobs) => <DataTable columns={columns} rows={jobs} rowKey={(row) => row.id} />}
      </QueryState>
      {detailJob && <FileRestoreJobDetailModal job={detailJob} onClose={() => setDetailJob(null)} />}
    </Panel>
  );
}

// --- File Index jobs ---------------------------------------------------------

function FileIndexJobDetailModal({ job, onClose }: { job: FileIndexJobOut; onClose: () => void }) {
  const { t } = useTranslation();
  return (
    <Modal open title={t("systemJobs.fileIndexJobDetailTitle")} onClose={onClose}>
      <dl className={`detailGrid ${styles.modalDetails}`}>
        <dt>{t("systemJobs.workload")}</dt>
        <dd>{job.workload_label}</dd>
        <dt>{t("common.status")}</dt>
        <dd>
          <StatusBadge tone={FILE_INDEX_STATUS_TONE[job.status]} label={t(`fileIndex.jobStatus.${job.status}`)} />
        </dd>
        <dt>{t("fileIndex.trigger")}</dt>
        <dd>{t(`fileIndex.triggerType.${job.trigger}`)}</dd>
        <dt>{t("fileIndex.entriesIndexed")}</dt>
        <dd>{job.entries_indexed}</dd>
        {job.current_path && (
          <>
            <dt>{t("fileIndex.currentPath")}</dt>
            <dd>{job.current_path}</dd>
          </>
        )}
        {job.error_code && (
          <>
            <dt>{t("systemJobs.errorCode")}</dt>
            <dd>{job.error_code}</dd>
          </>
        )}
        {job.error_message && (
          <>
            <dt>{t("systemJobs.errorMessage")}</dt>
            <dd>{job.error_message}</dd>
          </>
        )}
      </dl>
    </Modal>
  );
}

function FileIndexJobsSection() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [detailJob, setDetailJob] = useState<FileIndexJobOut | null>(null);
  const [pendingCancel, setPendingCancel] = useState<FileIndexJobOut | null>(null);

  const jobsQuery = useQuery({
    queryKey: ["system-jobs", "file-index"],
    queryFn: () => fileIndexApi.listJobs({ limit: 200 }),
    refetchInterval: POLL_INTERVAL_MS,
  });

  const cancelMutation = useMutation({
    mutationFn: (jobId: string) => fileIndexApi.cancelJob(jobId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["system-jobs", "file-index"] }),
  });

  const columns: DataTableColumn<FileIndexJobOut>[] = [
    {
      key: "status",
      header: t("common.status"),
      render: (row) => <StatusBadge tone={FILE_INDEX_STATUS_TONE[row.status]} label={t(`fileIndex.jobStatus.${row.status}`)} />,
    },
    { key: "workload", header: t("systemJobs.workload"), render: (row) => row.workload_label },
    { key: "trigger", header: t("fileIndex.trigger"), render: (row) => t(`fileIndex.triggerType.${row.trigger}`) },
    { key: "entries", header: t("fileIndex.entriesIndexed"), render: (row) => row.entries_indexed },
    {
      key: "created",
      header: t("common.createdAt"),
      render: (row) => new Date(row.created_at).toLocaleString(),
      sortValue: (row) => row.created_at,
    },
    {
      key: "actions",
      header: t("common.actions"),
      render: (row) => (
        <div className={styles.rowActions}>
          <button type="button" className="button buttonGhost" onClick={() => setDetailJob(row)}>
            {t("systemJobs.details")}
          </button>
          {["pending", "running", "paused"].includes(row.status) && (
            <button type="button" className="button buttonDanger" onClick={() => setPendingCancel(row)}>
              {t("systemJobs.cancel")}
            </button>
          )}
        </div>
      ),
    },
  ];

  return (
    <Panel testId="file-index-jobs" padded={false}>
      <QueryState query={jobsQuery} isEmpty={(data) => data.length === 0}>
        {(jobs) => <DataTable columns={columns} rows={jobs} rowKey={(row) => row.id} />}
      </QueryState>
      {detailJob && <FileIndexJobDetailModal job={detailJob} onClose={() => setDetailJob(null)} />}
      <ConfirmActionDialog
        open={pendingCancel !== null}
        title={t("fileIndex.confirmCancelTitle")}
        body={t("fileIndex.confirmCancelBody")}
        danger
        confirmLabel={t("systemJobs.cancel")}
        onCancel={() => setPendingCancel(null)}
        onConfirm={() => {
          if (pendingCancel) cancelMutation.mutate(pendingCancel.id);
          setPendingCancel(null);
        }}
      />
    </Panel>
  );
}

// --- Restore Plan executions -------------------------------------------------

const ABORTABLE_STATUSES: RestorePlanExecutionStatus[] = ["restore_requested", "restore_running"];

function RestorePlanDetailModal({ plan, onClose }: { plan: RestorePlanOut; onClose: () => void }) {
  const { t } = useTranslation();
  return (
    <Modal open title={t("systemJobs.restorePlanDetailTitle")} onClose={onClose}>
      <dl className={`detailGrid ${styles.modalDetails}`}>
        <dt>{t("restoreCenter.targetVmid")}</dt>
        <dd>{plan.target_vmid}</dd>
        <dt>{t("restoreCenter.node")}</dt>
        <dd>{plan.target_node}</dd>
        {plan.upid && (
          <>
            <dt>UPID</dt>
            <dd className="resurgam-mono">{plan.upid}</dd>
          </>
        )}
        {plan.execution_error && (
          <>
            <dt>{t("systemJobs.errorMessage")}</dt>
            <dd>{plan.execution_error}</dd>
          </>
        )}
      </dl>
      <p className="label">{t("restoreCenter.generatedCommand")}</p>
      <pre className={styles.commandBlock}>{plan.generated_command}</pre>
      <p className="label">{t("systemJobs.checks")}</p>
      <ul className={styles.checksList}>
        {plan.checks_result.map((check) => (
          <li key={check.check}>
            {check.check}: {check.status} — {check.message}
          </li>
        ))}
      </ul>
    </Modal>
  );
}

function AbortExecutionDialog({ plan, onClose }: { plan: RestorePlanOut; onClose: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [confirmVmid, setConfirmVmid] = useState("");
  const [error, setError] = useState<string | null>(null);

  const abortMutation = useMutation({
    mutationFn: () => restorePlansApi.abortExecution(plan.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["system-jobs", "restore-plans"] });
      onClose();
    },
    onError: (err) => setError(errorMessage(err, t("common.loadError"))),
  });

  return (
    <Modal open title={t("systemJobs.abortExecutionTitle")} onClose={onClose}>
      <InlineAlert tone="danger">{t("systemJobs.abortExecutionWarning")}</InlineAlert>
      {error && <InlineAlert tone="danger">{error}</InlineAlert>}
      <div className="field">
        <label className="label" htmlFor="abort-confirm-vmid">
          {t("systemJobs.abortConfirmLabel", { vmid: plan.target_vmid })}
        </label>
        <input
          id="abort-confirm-vmid"
          className="input"
          value={confirmVmid}
          onChange={(e) => setConfirmVmid(e.target.value)}
        />
      </div>
      <div className={styles.rowActions}>
        <button type="button" className="button" onClick={onClose}>
          {t("common.cancel")}
        </button>
        <button
          type="button"
          className="button buttonDanger"
          disabled={confirmVmid !== String(plan.target_vmid) || abortMutation.isPending}
          onClick={() => abortMutation.mutate()}
        >
          {t("systemJobs.abortExecution")}
        </button>
      </div>
    </Modal>
  );
}

function RestorePlanExecutionsSection() {
  const { t } = useTranslation();
  const [detailPlan, setDetailPlan] = useState<RestorePlanOut | null>(null);
  const [abortPlan, setAbortPlan] = useState<RestorePlanOut | null>(null);

  const plansQuery = useQuery({
    queryKey: ["system-jobs", "restore-plans"],
    queryFn: restorePlansApi.list,
    refetchInterval: POLL_INTERVAL_MS,
  });

  const executedPlans = (plansQuery.data ?? []).filter((plan) => plan.execution_status !== null);

  const columns: DataTableColumn<RestorePlanOut>[] = [
    {
      key: "status",
      header: t("common.status"),
      render: (row) =>
        row.execution_status ? (
          <StatusBadge tone={EXECUTION_STATUS_TONE[row.execution_status]} label={t(`restoreCenter.executionStatus_${row.execution_status}`)} />
        ) : (
          "—"
        ),
    },
    { key: "vmid", header: t("restoreCenter.targetVmid"), render: (row) => row.target_vmid },
    { key: "node", header: t("restoreCenter.node"), render: (row) => row.target_node },
    {
      key: "executedAt",
      header: t("systemJobs.executedAt"),
      render: (row) => (row.executed_at ? new Date(row.executed_at).toLocaleString() : "—"),
      sortValue: (row) => row.executed_at ?? "",
    },
    {
      key: "actions",
      header: t("common.actions"),
      render: (row) => (
        <div className={styles.rowActions}>
          <button type="button" className="button buttonGhost" onClick={() => setDetailPlan(row)}>
            {t("systemJobs.details")}
          </button>
          {row.execution_status && ABORTABLE_STATUSES.includes(row.execution_status) && (
            <button type="button" className="button buttonDanger" onClick={() => setAbortPlan(row)}>
              {t("systemJobs.abortExecution")}
            </button>
          )}
        </div>
      ),
    },
  ];

  return (
    <Panel testId="restore-plan-executions" padded={false}>
      <QueryState query={plansQuery} isEmpty={() => executedPlans.length === 0}>
        {() => <DataTable columns={columns} rows={executedPlans} rowKey={(row) => row.id} />}
      </QueryState>
      {detailPlan && <RestorePlanDetailModal plan={detailPlan} onClose={() => setDetailPlan(null)} />}
      {abortPlan && <AbortExecutionDialog plan={abortPlan} onClose={() => setAbortPlan(null)} />}
    </Panel>
  );
}

/** Admin-only view of every RESURGAM-internal background job across its
 * three separate systems (raised directly by the user): File Restore
 * jobs (ADR 0013), File Index jobs (ADR 0016), and Restore Plan
 * executions (ADR 0014's PVE-side qmrestore/pct restore). Three tabs
 * rather than one forced-unified table -- the three job shapes are
 * genuinely different (different status vocabularies, different
 * fields), and each needs its own action: File Restore jobs can only be
 * cancelled while still `pending` (the worker's blocking, checkpoint-
 * free execution model has no way to interrupt a running one without a
 * real rework); File Index jobs support real cooperative cancellation
 * (checkpointed between directories/batches); Restore Plan executions
 * can be aborted via a real PVE task-stop call, gated behind a typed-
 * VMID confirmation given the real risk of interrupting a live disk
 * write partway through.
 */
export function SystemJobsPage() {
  const { t } = useTranslation();
  const [tab, setTab] = useTabParam(["file-restore", "file-index", "restore-plans"]);
  return (
    <div>
      <PageHeader title={t("systemJobs.title")} description={t("systemJobs.description")} />
      <Tabs
        label={t("systemJobs.title")}
        tabs={[
          { id: "file-restore", label: t("systemJobs.fileRestoreJobsTitle") },
          { id: "file-index", label: t("systemJobs.fileIndexJobsTitle") },
          { id: "restore-plans", label: t("systemJobs.restorePlanExecutionsTitle") },
        ]}
        active={tab}
        onChange={setTab}
      />
      {tab === "file-restore" && (
        <TabPanel id="file-restore">
          <FileRestoreJobsSection />
        </TabPanel>
      )}
      {tab === "file-index" && (
        <TabPanel id="file-index">
          <FileIndexJobsSection />
        </TabPanel>
      )}
      {tab === "restore-plans" && (
        <TabPanel id="restore-plans">
          <RestorePlanExecutionsSection />
        </TabPanel>
      )}
    </div>
  );
}
