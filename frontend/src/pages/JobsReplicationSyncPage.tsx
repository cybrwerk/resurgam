import { useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { customersApi, datastoresApi, pbsInstancesApi, pbsJobsApi, tasksApi } from "../api/resources";
import type { DatastoreOut, PbsSyncJobOut } from "../api/types";
import { CollectorStalenessNote } from "../components/CollectorStalenessNote";
import { ConfirmActionDialog } from "../components/ConfirmActionDialog";
import { SyncJobFormModal } from "../components/DatastoreJobsModal";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import { InlineAlert } from "../components/InlineAlert";
import { JobFeedbackAlert, JobRowActions, errorMessage, writeBlockedReason, type JobFeedback } from "../components/JobRowActions";
import { Modal } from "../components/Modal";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { StatusBadge } from "../components/StatusBadge";
import { useSelectedCustomer } from "../contexts/CustomerContext";
import { lastSyncRun } from "../lib/jobLastRun";
import { taskStatusPresentation } from "../lib/statusPresentation";
import styles from "./JobsPage.module.css";

interface SyncRow {
  datastore: DatastoreOut;
  job: PbsSyncJobOut;
  canWrite: boolean;
  blockedReason: string | null;
}

function CreateTargetPicker({
  datastores,
  onPick,
  onClose,
}: {
  datastores: DatastoreOut[];
  onPick: (datastore: DatastoreOut) => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [datastoreId, setDatastoreId] = useState("");

  return (
    <Modal open title={t("jobs.pickDatastoreTitle")} onClose={onClose}>
      <div className="field">
        <label className="label" htmlFor="sync-target-datastore">
          {t("jobs.pickDatastoreLabel")}
        </label>
        <select id="sync-target-datastore" className="select" value={datastoreId} onChange={(e) => setDatastoreId(e.target.value)}>
          <option value="">{t("jobs.pickDatastorePlaceholder")}</option>
          {datastores.map((d) => (
            <option key={d.id} value={d.id}>
              {d.name}
            </option>
          ))}
        </select>
      </div>
      <div className="formActions">
        <button type="button" className="button" onClick={onClose}>
          {t("common.cancel")}
        </button>
        <button
          type="button"
          className="button buttonPrimary"
          disabled={!datastoreId}
          onClick={() => onPick(datastores.find((d) => d.id === datastoreId)!)}
        >
          {t("common.next")}
        </button>
      </div>
    </Modal>
  );
}

export function JobsReplicationSyncPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { selectedCustomerId } = useSelectedCustomer();
  const [pendingDelete, setPendingDelete] = useState<SyncRow | null>(null);
  const [editing, setEditing] = useState<{ instanceId: string; datastoreName: string; job: PbsSyncJobOut | null } | null>(null);
  const [showCreatePicker, setShowCreatePicker] = useState(false);
  const [feedback, setFeedback] = useState<JobFeedback | null>(null);

  const datastoresQuery = useQuery({ queryKey: ["datastores"], queryFn: datastoresApi.list });
  const allDatastores = datastoresQuery.data ?? [];
  const datastores = selectedCustomerId ? allDatastores.filter((d) => d.customer_id === selectedCustomerId) : allDatastores;
  const customersQuery = useQuery({ queryKey: ["customers"], queryFn: customersApi.list });
  const customerById = new Map((customersQuery.data ?? []).map((c) => [c.id, c.name]));
  const instancesQuery = useQuery({ queryKey: ["pbs-instances"], queryFn: pbsInstancesApi.list });

  const jobsQueries = useQueries({
    queries: datastores.map((d) => ({
      queryKey: ["pbs-sync-jobs", d.pbs_instance_id, d.name],
      queryFn: () => pbsJobsApi.syncJobs(d.pbs_instance_id, d.name),
    })),
  });
  const capabilitiesQueries = useQueries({
    queries: datastores.map((d) => ({
      queryKey: ["pbs-sync-job-capabilities", d.pbs_instance_id],
      queryFn: () => pbsJobsApi.syncJobCapabilities(d.pbs_instance_id),
    })),
  });

  const instanceIds = [...new Set(datastores.map((d) => d.pbs_instance_id))];
  const tasksQuery = useQuery({
    queryKey: ["tasks", instanceIds, ["sync"]],
    queryFn: () => tasksApi.list({ instanceIds, taskTypes: ["sync"], limit: 500 }),
    enabled: instanceIds.length > 0,
  });

  const isLoading = datastoresQuery.isLoading || jobsQueries.some((q) => q.isLoading);

  const rows: SyncRow[] = [];
  datastores.forEach((datastore, i) => {
    const jobs = jobsQueries[i]?.data ?? [];
    const capabilities = capabilitiesQueries[i]?.data;
    const canWrite = capabilities?.can_write ?? false;
    const blockedReason = writeBlockedReason(capabilities, t);
    for (const job of jobs) rows.push({ datastore, job, canWrite, blockedReason });
  });

  const deleteMutation = useMutation({
    mutationFn: ({ instanceId, jobId }: { instanceId: string; datastoreName: string; jobId: string }) =>
      pbsJobsApi.deleteSyncJob(instanceId, jobId),
    onSuccess: (_data, { instanceId, datastoreName }) => {
      queryClient.invalidateQueries({ queryKey: ["pbs-sync-jobs", instanceId, datastoreName] });
      queryClient.invalidateQueries({ queryKey: ["sync-jobs", instanceId] });
      setFeedback({ tone: "info", message: t("pbsJobs.jobDeletedOn", { datastore: datastoreName }) });
    },
    onError: (err) => setFeedback({ tone: "danger", message: errorMessage(err, t("pbsJobs.deleteFailed")) }),
  });

  const columns: DataTableColumn<SyncRow>[] = [
    { key: "datastore", header: t("jobs.datastoreColumn"), render: (row) => row.datastore.name },
    {
      key: "customer",
      header: t("customerSwitcher.label"),
      render: (row) => (row.datastore.customer_id ? customerById.get(row.datastore.customer_id) ?? "—" : "—"),
    },
    { key: "remote", header: t("pbsJobs.syncRemote"), render: (row) => row.job.remote_name || t("pbsJobs.syncRemoteNone") },
    { key: "remote_store", header: t("pbsJobs.syncRemoteStore"), render: (row) => row.job.remote_store },
    {
      key: "last_run",
      header: t("jobs.lastRun"),
      render: (row) => {
        const run = lastSyncRun(tasksQuery.data ?? [], row.datastore.name, row.job.job_id);
        if (!run) return <span className="mutedText">{t("jobs.lastRunNone")}</span>;
        const { tone, label } = taskStatusPresentation(run.status, t);
        return (
          <>
            <StatusBadge tone={tone} label={label} /> <span className="mutedText">{new Date(run.startedAt).toLocaleString()}</span>
          </>
        );
      },
    },
    { key: "schedule", header: t("pbsJobs.syncSchedule"), render: (row) => row.job.schedule ?? "—" },
    {
      key: "actions",
      header: t("common.actions"),
      render: (row) => (
        <JobRowActions
          canWrite={row.canWrite}
          blockedReason={row.blockedReason}
          onEdit={() => setEditing({ instanceId: row.datastore.pbs_instance_id, datastoreName: row.datastore.name, job: row.job })}
          onDelete={() => setPendingDelete(row)}
        />
      ),
    },
  ];

  return (
    <div>
      <PageHeader
        title={t("jobs.replicationSyncTitle")}
        description={<CollectorStalenessNote instances={instancesQuery.data ?? []} instanceIds={instanceIds} />}
        actions={
          <button type="button" className="button buttonPrimary" onClick={() => setShowCreatePicker(true)} disabled={datastores.length === 0}>
            {t("common.create")}
          </button>
        }
      />
      <div className="stack">
        <JobFeedbackAlert feedback={feedback} />
        {datastoresQuery.isError ? (
          <InlineAlert tone="danger">{t("common.loadError")}</InlineAlert>
        ) : isLoading ? (
          <p className="mutedText">{t("common.loading")}</p>
        ) : rows.length === 0 ? (
          <EmptyState title={t("common.empty")} />
        ) : (
          <DataTable columns={columns} rows={rows} rowKey={(row) => `${row.datastore.id}:${row.job.job_id}`} />
        )}

        <Panel title={t("pbsRemotes.title")}>
          <p className="panelIntro">{t("pbsRemotes.hint")}</p>
          <ul className={styles.linkList}>
            {instancesQuery.data?.map((instance) => (
              <li key={instance.id}>
                <Link className="button" to={`/infrastructure/pbs-instances?open=${instance.id}`}>
                  {t("jobs.manageRemotesOn", { name: instance.display_name })}
                </Link>
              </li>
            ))}
          </ul>
        </Panel>
      </div>

      <ConfirmActionDialog
        open={pendingDelete !== null}
        title={t("pbsJobs.confirmDeleteSyncTitle")}
        body={t("pbsJobs.confirmDeleteSyncBody", { schedule: pendingDelete?.job.schedule ?? "" })}
        confirmLabel={t("common.delete")}
        danger
        onConfirm={() => {
          if (pendingDelete)
            deleteMutation.mutate({
              instanceId: pendingDelete.datastore.pbs_instance_id,
              datastoreName: pendingDelete.datastore.name,
              jobId: pendingDelete.job.job_id,
            });
          setPendingDelete(null);
        }}
        onCancel={() => setPendingDelete(null)}
      />
      {showCreatePicker && (
        <CreateTargetPicker
          datastores={datastores}
          onClose={() => setShowCreatePicker(false)}
          onPick={(datastore) => {
            setShowCreatePicker(false);
            setEditing({ instanceId: datastore.pbs_instance_id, datastoreName: datastore.name, job: null });
          }}
        />
      )}
      {editing && (
        <SyncJobFormModal instanceId={editing.instanceId} datastoreName={editing.datastoreName} job={editing.job} onClose={() => setEditing(null)} onSaved={(message) => setFeedback({ tone: "info", message })} />
      )}
    </div>
  );
}
