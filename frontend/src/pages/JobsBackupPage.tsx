import { useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { customersApi, datastoresApi, pbsInstancesApi, pveClustersApi, tasksApi } from "../api/resources";
import { ApiError } from "../api/client";
import type { BackupRunOut, PveClusterOut, VzdumpJobOut } from "../api/types";
import { CollectorStalenessNote } from "../components/CollectorStalenessNote";
import { BackupJobDisableDialog } from "../components/BackupJobDisableDialog";
import { BackupJobStatus } from "../components/BackupJobStatus";
import { BackupRunViewer } from "../components/BackupRunViewer";
import { ConfirmActionDialog } from "../components/ConfirmActionDialog";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { InlineAlert } from "../components/InlineAlert";
import { Modal } from "../components/Modal";
import { PageHeader } from "../components/PageHeader";
import { QueryState } from "../components/QueryState";
import { StatusBadge } from "../components/StatusBadge";
import { useSelectedCustomer } from "../contexts/CustomerContext";
import { buildDatastoresByName, resolveVzdumpJobCustomerId } from "../lib/jobCustomerScope";
import { lastBackupRun } from "../lib/jobLastRun";
import { taskStatusPresentation } from "../lib/statusPresentation";
import { VzdumpJobFormModal } from "./PveClustersPage";
import styles from "./JobsPage.module.css";

interface JobRow {
  cluster: PveClusterOut;
  job: VzdumpJobOut;
  canWrite: boolean;
  pbsDatastore: string | null;
}

/** Which target cluster a new job goes on -- the cross-entity page has no
 * pre-known parent the way the PVE Cluster detail modal's own "Backup
 * jobs" section does, so "Create" needs this one extra step before
 * reusing the exact same `VzdumpJobFormModal` unchanged. */
function CreateTargetPicker({
  clusters,
  onPick,
  onClose,
}: {
  clusters: PveClusterOut[];
  onPick: (clusterId: string) => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [clusterId, setClusterId] = useState("");

  return (
    <Modal open title={t("jobs.pickClusterTitle")} onClose={onClose}>
      <div className="field">
        <label className="label" htmlFor="job-target-cluster">
          {t("jobs.pickClusterLabel")}
        </label>
        <select id="job-target-cluster" className="select" value={clusterId} onChange={(e) => setClusterId(e.target.value)}>
          <option value="">{t("jobs.pickClusterPlaceholder")}</option>
          {clusters.map((cluster) => (
            <option key={cluster.id} value={cluster.id}>
              {cluster.display_name}
            </option>
          ))}
        </select>
      </div>
      <div className="formActions">
        <button type="button" className="button" onClick={onClose}>
          {t("common.cancel")}
        </button>
        <button type="button" className="button buttonPrimary" disabled={!clusterId} onClick={() => onPick(clusterId)}>
          {t("common.next")}
        </button>
      </div>
    </Modal>
  );
}

/** The job as operators know it: its comment (shown as "Name"), else its id. */
function jobName(job: VzdumpJobOut): string {
  return job.comment?.trim() || job.id;
}

export function JobsBackupPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { selectedCustomerId } = useSelectedCustomer();
  const [pendingDisable, setPendingDisable] = useState<JobRow | null>(null);
  const [pendingTimerChange, setPendingTimerChange] = useState<JobRow | null>(null);
  const [pendingDelete, setPendingDelete] = useState<JobRow | null>(null);
  const [editing, setEditing] = useState<{ clusterId: string; job: VzdumpJobOut | null } | null>(null);
  const [showCreatePicker, setShowCreatePicker] = useState(false);
  const [pendingRun, setPendingRun] = useState<JobRow | null>(null);
  // The last started run stays reachable after its viewer is closed ("Show live status").
  const [activeRun, setActiveRun] = useState<{ clusterId: string; run: BackupRunOut; jobName: string } | null>(null);
  const [viewerOpen, setViewerOpen] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);

  const clustersQuery = useQuery({ queryKey: ["pve-clusters"], queryFn: pveClustersApi.list });
  const clusters = clustersQuery.data ?? [];
  const datastoresQuery = useQuery({ queryKey: ["datastores"], queryFn: datastoresApi.list });
  const customersQuery = useQuery({ queryKey: ["customers"], queryFn: customersApi.list });

  const jobsQueries = useQueries({
    queries: clusters.map((c) => ({
      queryKey: ["pve-cluster-backup-jobs", c.id],
      queryFn: () => pveClustersApi.backupJobs(c.id),
    })),
  });
  const capabilitiesQueries = useQueries({
    queries: clusters.map((c) => ({
      queryKey: ["pve-cluster-backup-job-capabilities", c.id],
      queryFn: () => pveClustersApi.backupJobCapabilities(c.id),
    })),
  });
  const nodesQueries = useQueries({
    queries: clusters.map((c) => ({ queryKey: ["pve-cluster-nodes", c.id], queryFn: () => pveClustersApi.nodes(c.id) })),
  });
  const storagesQueries = useQueries({
    queries: clusters.map((c, i) => {
      const firstNode = nodesQueries[i]?.data?.[0]?.node;
      return {
        queryKey: ["pve-cluster-storages", c.id, firstNode],
        queryFn: () => pveClustersApi.storages(c.id, firstNode!),
        enabled: !!firstNode,
      };
    }),
  });

  const isLoading = clustersQuery.isLoading || jobsQueries.some((q) => q.isLoading);
  const datastoresByName = buildDatastoresByName(datastoresQuery.data ?? []);

  const rows: JobRow[] = [];
  clusters.forEach((cluster, i) => {
    const jobs = jobsQueries[i]?.data ?? [];
    const canWrite = capabilitiesQueries[i]?.data?.can_write ?? false;
    const storages = storagesQueries[i]?.data ?? [];
    for (const job of jobs) {
      const effectiveCustomerId = resolveVzdumpJobCustomerId(job, storages, datastoresByName);
      if (selectedCustomerId && effectiveCustomerId !== selectedCustomerId) continue;
      const pbsDatastore = storages.find((s) => s.storage === job.storage)?.pbs_datastore ?? null;
      rows.push({ cluster, job, canWrite, pbsDatastore });
    }
  });

  const instanceIds = [...new Set((datastoresQuery.data ?? []).map((d) => d.pbs_instance_id))];
  const tasksQuery = useQuery({
    queryKey: ["tasks", instanceIds, ["backup"]],
    queryFn: () => tasksApi.list({ instanceIds, taskTypes: ["backup"], limit: 500 }),
    enabled: instanceIds.length > 0,
  });
  const pbsInstancesQuery = useQuery({ queryKey: ["pbs-instances"], queryFn: pbsInstancesApi.list });

  const invalidate = (clusterId: string) => queryClient.invalidateQueries({ queryKey: ["pve-cluster-backup-jobs", clusterId] });
  const enableMutation = useMutation({
    mutationFn: ({ clusterId, jobId }: { clusterId: string; jobId: string }) => pveClustersApi.enableBackupJob(clusterId, jobId),
    onSuccess: (_data, { clusterId }) => invalidate(clusterId),
  });
  const disableMutation = useMutation({
    mutationFn: ({ clusterId, jobId, hours }: { clusterId: string; jobId: string; hours: number | null }) =>
      pveClustersApi.disableBackupJob(clusterId, jobId, hours),
    onSuccess: (_data, { clusterId }) => invalidate(clusterId),
  });
  const cancelReenableMutation = useMutation({
    mutationFn: ({ clusterId, jobId }: { clusterId: string; jobId: string }) =>
      pveClustersApi.cancelBackupJobReenable(clusterId, jobId),
    onSuccess: (_data, { clusterId }) => invalidate(clusterId),
  });
  const changeReenableMutation = useMutation({
    mutationFn: ({ clusterId, jobId, hours }: { clusterId: string; jobId: string; hours: number }) =>
      pveClustersApi.setBackupJobReenable(clusterId, jobId, hours),
    onSuccess: (_data, { clusterId }) => invalidate(clusterId),
  });
  const runMutation = useMutation({
    mutationFn: ({ clusterId, jobId }: { clusterId: string; jobId: string; jobName: string }) =>
      pveClustersApi.runBackupJob(clusterId, jobId),
    onSuccess: (run, { clusterId, jobName }) => {
      setRunError(null);
      setActiveRun({ clusterId, run, jobName });
      setViewerOpen(true);
    },
    onError: (err) => setRunError(err instanceof ApiError ? err.message : t("common.loadError")),
  });
  const deleteMutation = useMutation({
    mutationFn: ({ clusterId, jobId }: { clusterId: string; jobId: string }) => pveClustersApi.deleteBackupJob(clusterId, jobId),
    onSuccess: (_data, { clusterId }) => invalidate(clusterId),
  });
  const assignCustomerMutation = useMutation({
    mutationFn: ({ clusterId, jobId, customerId }: { clusterId: string; jobId: string; customerId: string }) =>
      customerId ? pveClustersApi.assignBackupJobCustomer(clusterId, jobId, customerId) : pveClustersApi.unassignBackupJobCustomer(clusterId, jobId),
    onSuccess: (_data, { clusterId }) => invalidate(clusterId),
  });

  const columns: DataTableColumn<JobRow>[] = [
    { key: "cluster", header: t("jobs.clusterColumn"), render: (row) => row.cluster.display_name },
    { key: "schedule", header: t("pveClusters.backupJobSchedule"), render: (row) => row.job.schedule ?? "—" },
    { key: "storage", header: t("pveClusters.backupJobStorage"), render: (row) => row.job.storage ?? "—" },
    { key: "comment", header: t("common.name"), render: (row) => row.job.comment ?? "—" },
    {
      key: "customer_id",
      header: t("pveClusters.backupJobCustomerOverride"),
      render: (row) => (
        <select
          className="select"
          value={row.job.customer_id ?? ""}
          disabled={!row.canWrite}
          onChange={(e) =>
            assignCustomerMutation.mutate({ clusterId: row.cluster.id, jobId: row.job.id, customerId: e.target.value })
          }
        >
          <option value="">{t("pveClusters.backupJobCustomerOverrideNone")}</option>
          {customersQuery.data?.map((customer) => (
            <option key={customer.id} value={customer.id}>
              {customer.name}
            </option>
          ))}
        </select>
      ),
    },
    {
      key: "enabled",
      header: t("common.status"),
      render: (row) => (
        <BackupJobStatus
          job={row.job}
          canWrite={row.canWrite}
          cancelPending={cancelReenableMutation.isPending || changeReenableMutation.isPending}
          onCancelTimer={() => cancelReenableMutation.mutate({ clusterId: row.cluster.id, jobId: row.job.id })}
          onChangeTimer={() => setPendingTimerChange(row)}
        />
      ),
    },
    {
      key: "last_run",
      header: t("jobs.lastRun"),
      render: (row) => {
        const vmids = (row.job.vmid ?? "").split(",").map((v) => v.trim()).filter(Boolean);
        const run = row.pbsDatastore && vmids.length > 0 ? lastBackupRun(tasksQuery.data ?? [], row.pbsDatastore, vmids) : null;
        if (!run) return <span className="mutedText">{t("jobs.lastRunNone")}</span>;
        const { tone, label } = taskStatusPresentation(run.status, t);
        return (
          <>
            <StatusBadge tone={tone} label={label} /> <span className="mutedText">{new Date(run.startedAt).toLocaleString()}</span>
          </>
        );
      },
    },
    {
      key: "actions",
      header: t("common.actions"),
      render: (row) => (
        <span className={styles.compactRowActions}>
          <button
            type="button"
            className="button"
            onClick={() => setPendingRun(row)}
            disabled={runMutation.isPending || !row.canWrite}
            title={row.canWrite ? undefined : t("backupRun.noPrivileges")}
          >
            {t("backupRun.runNow")}
          </button>
          {row.job.enabled ? (
            <button
              type="button"
              className="button buttonDanger"
              onClick={() => setPendingDisable(row)}
              disabled={!row.canWrite}
              title={row.canWrite ? undefined : t("backupRun.noPrivileges")}
            >
              {t("common.disable")}
            </button>
          ) : (
            <button
              type="button"
              className="button"
              onClick={() => enableMutation.mutate({ clusterId: row.cluster.id, jobId: row.job.id })}
              disabled={!row.canWrite}
              title={row.canWrite ? undefined : t("backupRun.noPrivileges")}
            >
              {row.job.reenable_at ? t("backupJobReenable.enableNow") : t("common.enable")}
            </button>
          )}
          <button
            type="button"
            className="button"
            onClick={() => setEditing({ clusterId: row.cluster.id, job: row.job })}
            disabled={!row.canWrite}
            title={row.canWrite ? undefined : t("backupRun.noPrivileges")}
          >
            {t("common.edit")}
          </button>
          <button
            type="button"
            className="button buttonDanger"
            onClick={() => setPendingDelete(row)}
            disabled={!row.canWrite}
            title={row.canWrite ? undefined : t("backupRun.noPrivileges")}
          >
            {t("common.delete")}
          </button>
        </span>
      ),
    },
  ];

  return (
    <div>
      <PageHeader
        title={t("jobs.backupTitle")}
        description={<CollectorStalenessNote instances={pbsInstancesQuery.data ?? []} instanceIds={instanceIds} />}
        actions={
          <button type="button" className="button buttonPrimary" onClick={() => setShowCreatePicker(true)} disabled={clusters.length === 0}>
            {t("common.create")}
          </button>
        }
      />
      {runError && (
        <div className={styles.alert}>
          <InlineAlert>{runError}</InlineAlert>
        </div>
      )}
      {activeRun && !viewerOpen && (
        <div className={styles.alert}>
          <InlineAlert tone="info">
            {t("backupRun.lastRun", { job: activeRun.jobName })}{" "}
            <button type="button" className="button" onClick={() => setViewerOpen(true)}>
              {t("backupRun.showLastRun")}
            </button>
          </InlineAlert>
        </div>
      )}
      {clustersQuery.isError ? (
        <InlineAlert tone="danger">{t("common.loadError")}</InlineAlert>
      ) : isLoading ? (
        <p className="mutedText">{t("common.loading")}</p>
      ) : rows.length === 0 ? (
        <QueryState query={clustersQuery} isEmpty={() => true}>
          {() => null}
        </QueryState>
      ) : (
        <DataTable columns={columns} rows={rows} rowKey={(row) => `${row.cluster.id}:${row.job.id}`} />
      )}

      <BackupJobDisableDialog
        open={pendingTimerChange !== null}
        mode="changeTimer"
        schedule={pendingTimerChange?.job.schedule}
        onConfirm={(hours) => {
          if (pendingTimerChange && hours !== null)
            changeReenableMutation.mutate({
              clusterId: pendingTimerChange.cluster.id,
              jobId: pendingTimerChange.job.id,
              hours,
            });
          setPendingTimerChange(null);
        }}
        onCancel={() => setPendingTimerChange(null)}
      />
      <BackupJobDisableDialog
        open={pendingDisable !== null}
        schedule={pendingDisable?.job.schedule}
        onConfirm={(hours) => {
          if (pendingDisable)
            disableMutation.mutate({ clusterId: pendingDisable.cluster.id, jobId: pendingDisable.job.id, hours });
          setPendingDisable(null);
        }}
        onCancel={() => setPendingDisable(null)}
      />
      <ConfirmActionDialog
        open={pendingRun !== null}
        title={t("backupRun.confirmTitle", { job: pendingRun ? jobName(pendingRun.job) : "" })}
        body={t("backupRun.confirmBody", { cluster: pendingRun?.cluster.display_name ?? "" })}
        confirmLabel={t("backupRun.runNow")}
        onConfirm={() => {
          if (pendingRun) {
            runMutation.mutate({
              clusterId: pendingRun.cluster.id,
              jobId: pendingRun.job.id,
              jobName: jobName(pendingRun.job),
            });
          }
          setPendingRun(null);
        }}
        onCancel={() => setPendingRun(null)}
      />
      <ConfirmActionDialog
        open={pendingDelete !== null}
        title={t("pveClusters.confirmDeleteBackupJobTitle")}
        body={t("pveClusters.confirmDeleteBackupJobBody", { schedule: pendingDelete?.job.schedule ?? "" })}
        confirmLabel={t("common.delete")}
        danger
        onConfirm={() => {
          if (pendingDelete) deleteMutation.mutate({ clusterId: pendingDelete.cluster.id, jobId: pendingDelete.job.id });
          setPendingDelete(null);
        }}
        onCancel={() => setPendingDelete(null)}
      />
      {showCreatePicker && (
        <CreateTargetPicker
          clusters={clusters}
          onClose={() => setShowCreatePicker(false)}
          onPick={(clusterId) => {
            setShowCreatePicker(false);
            setEditing({ clusterId, job: null });
          }}
        />
      )}
      {activeRun && viewerOpen && (
        <BackupRunViewer
          clusterId={activeRun.clusterId}
          run={activeRun.run}
          jobName={activeRun.jobName}
          onClose={() => setViewerOpen(false)}
        />
      )}
      {editing && <VzdumpJobFormModal clusterId={editing.clusterId} job={editing.job} onClose={() => setEditing(null)} />}
    </div>
  );
}
