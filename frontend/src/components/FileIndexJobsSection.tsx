import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { fileIndexApi } from "../api/resources";
import type { FileIndexJobOut, FileIndexJobStatus } from "../api/types";
import { ConfirmActionDialog } from "./ConfirmActionDialog";
import { DataTable, type DataTableColumn } from "./DataTable";
import { QueryState } from "./QueryState";
import { StatusBadge, type StatusTone } from "./StatusBadge";
import styles from "./FileIndexJobsSection.module.css";

const JOB_STATUS_TONE: Record<FileIndexJobStatus, StatusTone> = {
  pending: "info",
  running: "info",
  paused: "warning",
  completed: "ok",
  failed: "danger",
  cancelled: "unknown",
};

/** This backup group's File Index job history -- lifted as-is from the
 * old FileIndexPage into its own component for the Unified File
 * Workspace. Jobs move through non-terminal states on their own
 * (worker-driven); a short poll keeps progress/cancel-requested visible
 * without a manual refresh, matching RestoreCenterPage's execution-poll
 * pattern. */
export function FileIndexJobsSection({ groupId }: { groupId: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [pendingCancel, setPendingCancel] = useState<FileIndexJobOut | null>(null);

  const jobsQuery = useQuery({
    queryKey: ["file-index-jobs", groupId],
    queryFn: () => fileIndexApi.listJobs({ backupGroupId: groupId }),
    refetchInterval: 5000,
  });

  const cancelMutation = useMutation({
    mutationFn: (jobId: string) => fileIndexApi.cancelJob(jobId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["file-index-jobs", groupId] }),
  });

  const columns: DataTableColumn<FileIndexJobOut>[] = [
    {
      key: "status",
      header: t("common.status"),
      render: (row) => (
        <StatusBadge tone={JOB_STATUS_TONE[row.status]} label={t(`fileIndex.jobStatus.${row.status}`)} />
      ),
    },
    { key: "trigger", header: t("fileIndex.trigger"), render: (row) => t(`fileIndex.triggerType.${row.trigger}`) },
    { key: "entries", header: t("fileIndex.entriesIndexed"), render: (row) => row.entries_indexed },
    { key: "currentPath", header: t("fileIndex.currentPath"), render: (row) => row.current_path ?? "—" },
    {
      key: "created",
      header: t("common.createdAt"),
      render: (row) => new Date(row.created_at).toLocaleString(),
      sortValue: (row) => row.created_at,
    },
    {
      key: "actions",
      header: t("common.actions"),
      render: (row) =>
        ["pending", "running", "paused"].includes(row.status) ? (
          <button type="button" className="button buttonDanger" onClick={() => setPendingCancel(row)}>
            {t("fileIndex.cancelJob")}
          </button>
        ) : (
          "—"
        ),
    },
  ];

  return (
    <div className={styles.jobsSection}>
      <h2 className={styles.sectionTitle}>{t("fileIndex.jobsTitle")}</h2>
      <QueryState query={jobsQuery} isEmpty={(data) => data.length === 0}>
        {(jobs) => <DataTable columns={columns} rows={jobs} rowKey={(row) => row.id} />}
      </QueryState>

      <ConfirmActionDialog
        open={pendingCancel !== null}
        title={t("fileIndex.confirmCancelTitle")}
        body={t("fileIndex.confirmCancelBody")}
        danger
        confirmLabel={t("fileIndex.cancelJob")}
        onCancel={() => setPendingCancel(null)}
        onConfirm={() => {
          if (pendingCancel) cancelMutation.mutate(pendingCancel.id);
          setPendingCancel(null);
        }}
      />
    </div>
  );
}
