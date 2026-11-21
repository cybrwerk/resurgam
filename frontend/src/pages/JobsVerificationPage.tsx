import { useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { customersApi, datastoresApi, pbsInstancesApi, pbsJobsApi, tasksApi } from "../api/resources";
import type { DatastoreOut, PbsVerifyJobOut } from "../api/types";
import { CollectorStalenessNote } from "../components/CollectorStalenessNote";
import { ConfirmActionDialog } from "../components/ConfirmActionDialog";
import { VerifyJobFormModal } from "../components/DatastoreJobsModal";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import { InlineAlert } from "../components/InlineAlert";
import { JobFeedbackAlert, JobRowActions, errorMessage, writeBlockedReason, type JobFeedback } from "../components/JobRowActions";
import { Modal } from "../components/Modal";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";
import { useSelectedCustomer } from "../contexts/CustomerContext";
import { lastVerifyRun } from "../lib/jobLastRun";
import { taskStatusPresentation } from "../lib/statusPresentation";

interface VerifyRow {
  datastore: DatastoreOut;
  job: PbsVerifyJobOut;
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
        <label className="label" htmlFor="verify-target-datastore">
          {t("jobs.pickDatastoreLabel")}
        </label>
        <select id="verify-target-datastore" className="select" value={datastoreId} onChange={(e) => setDatastoreId(e.target.value)}>
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

export function JobsVerificationPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { selectedCustomerId } = useSelectedCustomer();
  const [pendingDelete, setPendingDelete] = useState<VerifyRow | null>(null);
  const [editing, setEditing] = useState<{ instanceId: string; datastoreName: string; job: PbsVerifyJobOut | null } | null>(null);
  const [showCreatePicker, setShowCreatePicker] = useState(false);
  const [feedback, setFeedback] = useState<JobFeedback | null>(null);

  const datastoresQuery = useQuery({ queryKey: ["datastores"], queryFn: datastoresApi.list });
  const allDatastores = datastoresQuery.data ?? [];
  const datastores = selectedCustomerId ? allDatastores.filter((d) => d.customer_id === selectedCustomerId) : allDatastores;
  const customersQuery = useQuery({ queryKey: ["customers"], queryFn: customersApi.list });
  const customerById = new Map((customersQuery.data ?? []).map((c) => [c.id, c.name]));

  const jobsQueries = useQueries({
    queries: datastores.map((d) => ({
      queryKey: ["pbs-verify-jobs", d.pbs_instance_id, d.name],
      queryFn: () => pbsJobsApi.verifyJobs(d.pbs_instance_id, d.name),
    })),
  });
  const capabilitiesQueries = useQueries({
    queries: datastores.map((d) => ({
      queryKey: ["pbs-verify-job-capabilities", d.pbs_instance_id],
      queryFn: () => pbsJobsApi.verifyJobCapabilities(d.pbs_instance_id),
    })),
  });

  const instanceIds = [...new Set(datastores.map((d) => d.pbs_instance_id))];
  const tasksQuery = useQuery({
    queryKey: ["tasks", instanceIds, ["verify"]],
    queryFn: () => tasksApi.list({ instanceIds, taskTypes: ["verify"], limit: 500 }),
    enabled: instanceIds.length > 0,
  });
  const pbsInstancesQuery = useQuery({ queryKey: ["pbs-instances"], queryFn: pbsInstancesApi.list });

  const isLoading = datastoresQuery.isLoading || jobsQueries.some((q) => q.isLoading);

  const rows: VerifyRow[] = [];
  datastores.forEach((datastore, i) => {
    const jobs = jobsQueries[i]?.data ?? [];
    const capabilities = capabilitiesQueries[i]?.data;
    const canWrite = capabilities?.can_write ?? false;
    const blockedReason = writeBlockedReason(capabilities, t);
    for (const job of jobs) rows.push({ datastore, job, canWrite, blockedReason });
  });

  const deleteMutation = useMutation({
    mutationFn: ({ instanceId, jobId }: { instanceId: string; datastoreName: string; jobId: string }) =>
      pbsJobsApi.deleteVerifyJob(instanceId, jobId),
    onSuccess: (_data, { instanceId, datastoreName }) => {
      queryClient.invalidateQueries({ queryKey: ["pbs-verify-jobs", instanceId, datastoreName] });
      setFeedback({ tone: "info", message: t("pbsJobs.jobDeletedOn", { datastore: datastoreName }) });
    },
    onError: (err) => setFeedback({ tone: "danger", message: errorMessage(err, t("pbsJobs.deleteFailed")) }),
  });

  const columns: DataTableColumn<VerifyRow>[] = [
    { key: "datastore", header: t("jobs.datastoreColumn"), render: (row) => row.datastore.name },
    {
      key: "customer",
      header: t("customerSwitcher.label"),
      render: (row) => (row.datastore.customer_id ? customerById.get(row.datastore.customer_id) ?? "—" : "—"),
    },
    { key: "schedule", header: t("pbsJobs.verifySchedule"), render: (row) => row.job.schedule ?? "—" },
    { key: "ns", header: t("pbsJobs.verifyNs"), render: (row) => row.job.ns || "/" },
    { key: "outdated_after", header: t("pbsJobs.verifyOutdatedAfter"), render: (row) => row.job.outdated_after ?? "—" },
    {
      key: "last_run",
      header: t("jobs.lastRun"),
      render: (row) => {
        const run = lastVerifyRun(tasksQuery.data ?? [], row.job.store, row.job.id);
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
        title={t("jobs.verificationTitle")}
        description={<CollectorStalenessNote instances={pbsInstancesQuery.data ?? []} instanceIds={instanceIds} />}
        actions={
          <button type="button" className="button buttonPrimary" onClick={() => setShowCreatePicker(true)} disabled={datastores.length === 0}>
            {t("common.create")}
          </button>
        }
      />
      <JobFeedbackAlert feedback={feedback} />
      {datastoresQuery.isError ? (
        <InlineAlert tone="danger">{t("common.loadError")}</InlineAlert>
      ) : isLoading ? (
        <p className="mutedText">{t("common.loading")}</p>
      ) : rows.length === 0 ? (
        <EmptyState title={t("common.empty")} />
      ) : (
        <DataTable columns={columns} rows={rows} rowKey={(row) => `${row.datastore.id}:${row.job.id}`} />
      )}

      <ConfirmActionDialog
        open={pendingDelete !== null}
        title={t("pbsJobs.confirmDeleteVerifyTitle")}
        body={t("pbsJobs.confirmDeleteVerifyBody", { schedule: pendingDelete?.job.schedule ?? "" })}
        confirmLabel={t("common.delete")}
        danger
        onConfirm={() => {
          if (pendingDelete)
            deleteMutation.mutate({
              instanceId: pendingDelete.datastore.pbs_instance_id,
              datastoreName: pendingDelete.datastore.name,
              jobId: pendingDelete.job.id,
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
        <VerifyJobFormModal instanceId={editing.instanceId} datastoreName={editing.datastoreName} job={editing.job} onClose={() => setEditing(null)} onSaved={(message) => setFeedback({ tone: "info", message })} />
      )}
    </div>
  );
}
