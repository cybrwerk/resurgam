import { useState, type FormEvent, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { datastoresApi, pbsInstancesApi, pbsJobsApi } from "../api/resources";
import type {
  DatastoreOut,
  PbsPruneJobCreateRequest,
  PbsPruneJobOut,
  PbsSyncJobCreateRequest,
  PbsSyncJobOut,
  PbsVerifyJobCreateRequest,
  PbsVerifyJobOut,
} from "../api/types";
import { ConfirmActionDialog } from "./ConfirmActionDialog";
import { DataTable, type DataTableColumn } from "./DataTable";
import { InlineAlert } from "./InlineAlert";
import { JobFeedbackAlert, JobRowActions, errorMessage, useWriteBlockedReason, type JobFeedback } from "./JobRowActions";
import { Modal } from "./Modal";
import { PruneSimulationButton } from "./PruneSimulationButton";
import { QueryState } from "./QueryState";
import styles from "./DatastoreJobsModal.module.css";

function toKeepInt(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === "") return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

function normalized(value: unknown): unknown {
  if (value === undefined || value === "") return null;
  return typeof value === "string" ? value.trim() || null : value;
}

/** Only the fields the operator actually changed in an edit form: a
 * changed value is sent as-is, an emptied field as `null` (which the
 * backend turns into PBS's `delete` parameter), an untouched field not at
 * all -- so an edit never clears or rewrites something the operator
 * didn't touch. */
export function changedFields<T extends object>(initial: T, next: T): Partial<T> {
  const diff: Partial<T> = {};
  for (const key of Object.keys(next) as (keyof T)[]) {
    const nextValue = normalized(next[key]);
    if (nextValue !== normalized(initial[key])) diff[key] = nextValue as T[keyof T];
  }
  return diff;
}

/** Shared props of the three job form modals. `onSaved` receives a short
 * success sentence the opening page shows once the modal closes. */
interface JobFormProps<J> {
  instanceId: string;
  datastoreName: string;
  job: J | null;
  onClose: () => void;
  onSaved?: (message: string) => void;
}

function FormError({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <div className={styles.alert}>
      <InlineAlert>{error}</InlineAlert>
    </div>
  );
}

function FormActions({ pending, onClose, submitDisabled = false }: { pending: boolean; onClose: () => void; submitDisabled?: boolean }) {
  const { t } = useTranslation();
  return (
    <div className={styles.formActions}>
      <button type="button" className="button" onClick={onClose} disabled={pending}>
        {t("common.cancel")}
      </button>
      <button type="submit" className="button buttonPrimary" disabled={pending || submitDisabled}>
        {t("common.save")}
      </button>
    </div>
  );
}

function FieldHint({ children }: { children: ReactNode }) {
  return <p className={`mutedText ${styles.fieldHint}`}>{children}</p>;
}

// --- Prune jobs ---------------------------------------------------------------

export function PruneJobFormModal({ instanceId, datastoreName, job, onClose, onSaved }: JobFormProps<PbsPruneJobOut>) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [schedule, setSchedule] = useState(job?.schedule ?? "");
  const [ns, setNs] = useState(job?.ns ?? "");
  const [keepLast, setKeepLast] = useState(job?.keep_last?.toString() ?? "");
  const [keepHourly, setKeepHourly] = useState(job?.keep_hourly?.toString() ?? "");
  const [keepDaily, setKeepDaily] = useState(job?.keep_daily?.toString() ?? "");
  const [keepWeekly, setKeepWeekly] = useState(job?.keep_weekly?.toString() ?? "");
  const [keepMonthly, setKeepMonthly] = useState(job?.keep_monthly?.toString() ?? "");
  const [keepYearly, setKeepYearly] = useState(job?.keep_yearly?.toString() ?? "");
  const [disable, setDisable] = useState(job?.disable ?? false);
  const [comment, setComment] = useState(job?.comment ?? "");
  const [error, setError] = useState<string | null>(null);

  const datastoresQuery = useQuery({ queryKey: ["datastores"], queryFn: datastoresApi.list });
  const localDatastore = datastoresQuery.data?.find((d) => d.pbs_instance_id === instanceId && d.name === datastoreName);

  const queryKey = ["pbs-prune-jobs", instanceId, datastoreName];
  const buildBody = (): PbsPruneJobCreateRequest => ({
    schedule: schedule || null,
    ns: ns || null,
    keep_last: toKeepInt(keepLast),
    keep_hourly: toKeepInt(keepHourly),
    keep_daily: toKeepInt(keepDaily),
    keep_weekly: toKeepInt(keepWeekly),
    keep_monthly: toKeepInt(keepMonthly),
    keep_yearly: toKeepInt(keepYearly),
    disable,
    comment: comment || null,
  });
  const [initialBody] = useState(buildBody);

  const saveMutation = useMutation({
    mutationFn: (body: Partial<PbsPruneJobCreateRequest>) =>
      job ? pbsJobsApi.updatePruneJob(instanceId, job.id, body) : pbsJobsApi.createPruneJob(instanceId, datastoreName, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
      onSaved?.(t("pbsJobs.pruneSaved", { datastore: datastoreName }));
      onClose();
    },
    onError: (err) => setError(errorMessage(err, t("common.loadError"))),
  });

  return (
    <Modal open title={job ? t("pbsJobs.editPruneTitle") : t("pbsJobs.createPruneTitle")} onClose={onClose}>
      <form
        className={styles.form}
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          setError(null);
          if (!schedule.trim()) {
            setError(t("pbsJobs.pruneScheduleRequired"));
            return;
          }
          if (!job) {
            saveMutation.mutate(buildBody());
            return;
          }
          const body = changedFields(initialBody, buildBody());
          if (Object.keys(body).length === 0) onClose();
          else saveMutation.mutate(body);
        }}
      >
        <FormError error={error} />
        <div className="field">
          <label className="label" htmlFor="prune-schedule">
            {t("pbsJobs.pruneSchedule")}
          </label>
          <input id="prune-schedule" className="input" value={schedule} onChange={(e) => setSchedule(e.target.value)} required />
          <FieldHint>{t("pbsJobs.pruneScheduleHint")}</FieldHint>
        </div>
        <div className="field">
          <label className="label" htmlFor="prune-ns">
            {t("pbsJobs.pruneNs")}
          </label>
          <input id="prune-ns" className="input" value={ns} onChange={(e) => setNs(e.target.value)} />
        </div>
        <div className={styles.keepGrid}>
          <div className="field">
            <label className="label" htmlFor="prune-keep-last">
              {t("pbsJobs.pruneKeepLast")}
            </label>
            <input id="prune-keep-last" type="number" min={1} className="input" value={keepLast} onChange={(e) => setKeepLast(e.target.value)} />
          </div>
          <div className="field">
            <label className="label" htmlFor="prune-keep-hourly">
              {t("pbsJobs.pruneKeepHourly")}
            </label>
            <input id="prune-keep-hourly" type="number" min={1} className="input" value={keepHourly} onChange={(e) => setKeepHourly(e.target.value)} />
          </div>
          <div className="field">
            <label className="label" htmlFor="prune-keep-daily">
              {t("pbsJobs.pruneKeepDaily")}
            </label>
            <input id="prune-keep-daily" type="number" min={1} className="input" value={keepDaily} onChange={(e) => setKeepDaily(e.target.value)} />
          </div>
          <div className="field">
            <label className="label" htmlFor="prune-keep-weekly">
              {t("pbsJobs.pruneKeepWeekly")}
            </label>
            <input id="prune-keep-weekly" type="number" min={1} className="input" value={keepWeekly} onChange={(e) => setKeepWeekly(e.target.value)} />
          </div>
          <div className="field">
            <label className="label" htmlFor="prune-keep-monthly">
              {t("pbsJobs.pruneKeepMonthly")}
            </label>
            <input id="prune-keep-monthly" type="number" min={1} className="input" value={keepMonthly} onChange={(e) => setKeepMonthly(e.target.value)} />
          </div>
          <div className="field">
            <label className="label" htmlFor="prune-keep-yearly">
              {t("pbsJobs.pruneKeepYearly")}
            </label>
            <input id="prune-keep-yearly" type="number" min={1} className="input" value={keepYearly} onChange={(e) => setKeepYearly(e.target.value)} />
          </div>
        </div>
        <PruneSimulationButton
          datastoreId={localDatastore?.id ?? null}
          namespace={ns || null}
          keepLast={keepLast}
          keepHourly={keepHourly}
          keepDaily={keepDaily}
          keepWeekly={keepWeekly}
          keepMonthly={keepMonthly}
          keepYearly={keepYearly}
        />
        <div className="field">
          <label className="label" htmlFor="prune-comment">
            {t("pbsJobs.pruneComment")}
          </label>
          <input id="prune-comment" className="input" value={comment} onChange={(e) => setComment(e.target.value)} />
        </div>
        <label className={styles.checkboxRow}>
          <input type="checkbox" checked={disable} onChange={(e) => setDisable(e.target.checked)} />
          {t("pbsJobs.pruneDisable")}
        </label>
        {job && <FieldHint>{t("pbsJobs.clearFieldHint")}</FieldHint>}
        <FormActions pending={saveMutation.isPending} onClose={onClose} />
      </form>
    </Modal>
  );
}

function PruneJobsSection({ instanceId, datastoreName }: { instanceId: string; datastoreName: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [pendingDelete, setPendingDelete] = useState<PbsPruneJobOut | null>(null);
  const [editingJob, setEditingJob] = useState<PbsPruneJobOut | null | "new">(null);
  const queryKey = ["pbs-prune-jobs", instanceId, datastoreName];

  const capabilitiesQuery = useQuery({
    queryKey: ["pbs-prune-job-capabilities", instanceId],
    queryFn: () => pbsJobsApi.pruneJobCapabilities(instanceId),
  });
  const canWrite = capabilitiesQuery.data?.can_write ?? false;
  const blockedReason = useWriteBlockedReason(capabilitiesQuery.data);
  const [feedback, setFeedback] = useState<JobFeedback | null>(null);

  const jobsQuery = useQuery({ queryKey, queryFn: () => pbsJobsApi.pruneJobs(instanceId, datastoreName) });
  const deleteMutation = useMutation({
    mutationFn: (jobId: string) => pbsJobsApi.deletePruneJob(instanceId, jobId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
      queryClient.invalidateQueries({ queryKey: ["sync-jobs", instanceId] });
      setFeedback({ tone: "info", message: t("pbsJobs.jobDeleted") });
    },
    onError: (err) => setFeedback({ tone: "danger", message: errorMessage(err, t("pbsJobs.deleteFailed")) }),
  });

  const columns: DataTableColumn<PbsPruneJobOut>[] = [
    { key: "schedule", header: t("pbsJobs.pruneSchedule"), render: (row) => row.schedule ?? "—" },
    { key: "ns", header: t("pbsJobs.pruneNs"), render: (row) => row.ns || "/" },
    {
      key: "keep",
      header: t("pbsJobs.pruneKeepDaily"),
      render: (row) =>
        [
          row.keep_last && `last=${row.keep_last}`,
          row.keep_hourly && `hourly=${row.keep_hourly}`,
          row.keep_daily && `daily=${row.keep_daily}`,
          row.keep_weekly && `weekly=${row.keep_weekly}`,
          row.keep_monthly && `monthly=${row.keep_monthly}`,
          row.keep_yearly && `yearly=${row.keep_yearly}`,
        ]
          .filter(Boolean)
          .join(", ") || "—",
    },
    {
      key: "actions",
      header: t("common.actions"),
      render: (row) => (
        <JobRowActions
          canWrite={canWrite}
          blockedReason={blockedReason}
          onEdit={() => setEditingJob(row)}
          onDelete={() => setPendingDelete(row)}
        />
      ),
    },
  ];

  return (
    <div>
      <h3 className={styles.sectionSubtitle}>{t("pbsJobs.pruneTitle")}</h3>
      <p className="mutedText">{t("pbsJobs.pruneHint")}</p>
      {blockedReason && <InlineAlert tone="info">{blockedReason}</InlineAlert>}
      <JobFeedbackAlert feedback={feedback} />
      <QueryState query={jobsQuery} isEmpty={(data) => data.length === 0}>
        {(jobs) => <DataTable columns={columns} rows={jobs} rowKey={(row) => row.id} />}
      </QueryState>
      <div className={styles.formActions}>
        <button type="button" className="button" onClick={() => setEditingJob("new")} disabled={!canWrite}>
          {t("common.create")}
        </button>
      </div>
      <ConfirmActionDialog
        open={pendingDelete !== null}
        title={t("pbsJobs.confirmDeletePruneTitle")}
        body={t("pbsJobs.confirmDeletePruneBody", { schedule: pendingDelete?.schedule ?? "" })}
        confirmLabel={t("common.delete")}
        danger
        onConfirm={() => {
          if (pendingDelete) deleteMutation.mutate(pendingDelete.id);
          setPendingDelete(null);
        }}
        onCancel={() => setPendingDelete(null)}
      />
      {editingJob !== null && (
        <PruneJobFormModal
          instanceId={instanceId}
          datastoreName={datastoreName}
          job={editingJob === "new" ? null : editingJob}
          onClose={() => setEditingJob(null)}
          onSaved={(message) => setFeedback({ tone: "info", message })}
        />
      )}
    </div>
  );
}

// --- Verify jobs ---------------------------------------------------------------

export function VerifyJobFormModal({ instanceId, datastoreName, job, onClose, onSaved }: JobFormProps<PbsVerifyJobOut>) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [schedule, setSchedule] = useState(job?.schedule ?? "");
  const [ns, setNs] = useState(job?.ns ?? "");
  const [outdatedAfter, setOutdatedAfter] = useState(job?.outdated_after?.toString() ?? "");
  // PBS's own default for `ignore-verified` is true (api-viewer), so an
  // unset value on an existing job is shown as checked.
  const [ignoreVerified, setIgnoreVerified] = useState(job ? (job.ignore_verified ?? true) : true);
  const [comment, setComment] = useState(job?.comment ?? "");
  const [error, setError] = useState<string | null>(null);

  const queryKey = ["pbs-verify-jobs", instanceId, datastoreName];
  const buildBody = (): PbsVerifyJobCreateRequest => ({
    schedule: schedule || null,
    ns: ns || null,
    outdated_after: toKeepInt(outdatedAfter),
    ignore_verified: ignoreVerified,
    comment: comment || null,
  });
  const [initialBody] = useState(buildBody);

  const saveMutation = useMutation({
    mutationFn: (body: Partial<PbsVerifyJobCreateRequest>) =>
      job ? pbsJobsApi.updateVerifyJob(instanceId, job.id, body) : pbsJobsApi.createVerifyJob(instanceId, datastoreName, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
      onSaved?.(t("pbsJobs.verifySaved", { datastore: datastoreName }));
      onClose();
    },
    onError: (err) => setError(errorMessage(err, t("common.loadError"))),
  });

  return (
    <Modal open title={job ? t("pbsJobs.editVerifyTitle") : t("pbsJobs.createVerifyTitle")} onClose={onClose}>
      <form
        className={styles.form}
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          setError(null);
          if (!job) {
            saveMutation.mutate(buildBody());
            return;
          }
          const body = changedFields(initialBody, buildBody());
          if (Object.keys(body).length === 0) onClose();
          else saveMutation.mutate(body);
        }}
      >
        <FormError error={error} />
        <div className="field">
          <label className="label" htmlFor="verify-schedule">
            {t("pbsJobs.verifySchedule")}
          </label>
          <input id="verify-schedule" className="input" value={schedule} onChange={(e) => setSchedule(e.target.value)} />
          <FieldHint>{t("pbsJobs.manualOnlyScheduleHint")}</FieldHint>
        </div>
        <div className="field">
          <label className="label" htmlFor="verify-ns">
            {t("pbsJobs.verifyNs")}
          </label>
          <input id="verify-ns" className="input" value={ns} onChange={(e) => setNs(e.target.value)} />
        </div>
        <div className="field">
          <label className="label" htmlFor="verify-outdated-after">
            {t("pbsJobs.verifyOutdatedAfter")}
          </label>
          <input
            id="verify-outdated-after"
            type="number"
            min={0}
            className="input"
            value={outdatedAfter}
            onChange={(e) => setOutdatedAfter(e.target.value)}
          />
        </div>
        <div className="field">
          <label className="label" htmlFor="verify-comment">
            {t("pbsJobs.verifyComment")}
          </label>
          <input id="verify-comment" className="input" value={comment} onChange={(e) => setComment(e.target.value)} />
        </div>
        <label className={styles.checkboxRow}>
          <input type="checkbox" checked={ignoreVerified} onChange={(e) => setIgnoreVerified(e.target.checked)} />
          {t("pbsJobs.verifyIgnoreVerified")}
        </label>
        {job && <FieldHint>{t("pbsJobs.verifyNoDisableHint")}</FieldHint>}
        {job && <FieldHint>{t("pbsJobs.clearFieldHint")}</FieldHint>}
        <FormActions pending={saveMutation.isPending} onClose={onClose} />
      </form>
    </Modal>
  );
}

function VerifyJobsSection({ instanceId, datastoreName }: { instanceId: string; datastoreName: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [pendingDelete, setPendingDelete] = useState<PbsVerifyJobOut | null>(null);
  const [editingJob, setEditingJob] = useState<PbsVerifyJobOut | null | "new">(null);
  const queryKey = ["pbs-verify-jobs", instanceId, datastoreName];

  const capabilitiesQuery = useQuery({
    queryKey: ["pbs-verify-job-capabilities", instanceId],
    queryFn: () => pbsJobsApi.verifyJobCapabilities(instanceId),
  });
  const canWrite = capabilitiesQuery.data?.can_write ?? false;
  const blockedReason = useWriteBlockedReason(capabilitiesQuery.data);
  const [feedback, setFeedback] = useState<JobFeedback | null>(null);

  const jobsQuery = useQuery({ queryKey, queryFn: () => pbsJobsApi.verifyJobs(instanceId, datastoreName) });
  const deleteMutation = useMutation({
    mutationFn: (jobId: string) => pbsJobsApi.deleteVerifyJob(instanceId, jobId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
      queryClient.invalidateQueries({ queryKey: ["sync-jobs", instanceId] });
      setFeedback({ tone: "info", message: t("pbsJobs.jobDeleted") });
    },
    onError: (err) => setFeedback({ tone: "danger", message: errorMessage(err, t("pbsJobs.deleteFailed")) }),
  });

  const columns: DataTableColumn<PbsVerifyJobOut>[] = [
    { key: "schedule", header: t("pbsJobs.verifySchedule"), render: (row) => row.schedule ?? "—" },
    { key: "ns", header: t("pbsJobs.verifyNs"), render: (row) => row.ns || "/" },
    { key: "outdated_after", header: t("pbsJobs.verifyOutdatedAfter"), render: (row) => row.outdated_after ?? "—" },
    {
      key: "actions",
      header: t("common.actions"),
      render: (row) => (
        <JobRowActions
          canWrite={canWrite}
          blockedReason={blockedReason}
          onEdit={() => setEditingJob(row)}
          onDelete={() => setPendingDelete(row)}
        />
      ),
    },
  ];

  return (
    <div>
      <h3 className={styles.sectionSubtitle}>{t("pbsJobs.verifyTitle")}</h3>
      {blockedReason && <InlineAlert tone="info">{blockedReason}</InlineAlert>}
      <JobFeedbackAlert feedback={feedback} />
      <QueryState query={jobsQuery} isEmpty={(data) => data.length === 0}>
        {(jobs) => <DataTable columns={columns} rows={jobs} rowKey={(row) => row.id} />}
      </QueryState>
      <div className={styles.formActions}>
        <button type="button" className="button" onClick={() => setEditingJob("new")} disabled={!canWrite}>
          {t("common.create")}
        </button>
      </div>
      <ConfirmActionDialog
        open={pendingDelete !== null}
        title={t("pbsJobs.confirmDeleteVerifyTitle")}
        body={t("pbsJobs.confirmDeleteVerifyBody", { schedule: pendingDelete?.schedule ?? "" })}
        confirmLabel={t("common.delete")}
        danger
        onConfirm={() => {
          if (pendingDelete) deleteMutation.mutate(pendingDelete.id);
          setPendingDelete(null);
        }}
        onCancel={() => setPendingDelete(null)}
      />
      {editingJob !== null && (
        <VerifyJobFormModal
          instanceId={instanceId}
          datastoreName={datastoreName}
          job={editingJob === "new" ? null : editingJob}
          onClose={() => setEditingJob(null)}
          onSaved={(message) => setFeedback({ tone: "info", message })}
        />
      )}
    </div>
  );
}

// --- Sync jobs ---------------------------------------------------------------

export function SyncJobFormModal({ instanceId, datastoreName, job, onClose, onSaved }: JobFormProps<PbsSyncJobOut>) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const remotesQuery = useQuery({ queryKey: ["pbs-remotes", instanceId], queryFn: () => pbsJobsApi.remotes(instanceId) });
  const [remoteName, setRemoteName] = useState(job?.remote_name ?? "");
  const [remoteStore, setRemoteStore] = useState(job?.remote_store ?? "");
  const [schedule, setSchedule] = useState(job?.schedule ?? "");
  const [ns, setNs] = useState(job?.ns ?? "");
  const [owner, setOwner] = useState(job?.owner ?? "");
  const [removeVanished, setRemoveVanished] = useState(job?.remove_vanished ?? false);
  const [comment, setComment] = useState(job?.comment ?? "");
  const [error, setError] = useState<string | null>(null);

  const queryKey = ["pbs-sync-jobs", instanceId, datastoreName];
  const buildBody = (): PbsSyncJobCreateRequest => ({
    remote_store: remoteStore,
    remote_name: remoteName || null,
    schedule: schedule || null,
    ns: ns || null,
    owner: owner || null,
    remove_vanished: removeVanished,
    comment: comment || null,
  });
  const [initialBody] = useState(buildBody);

  const saveMutation = useMutation({
    mutationFn: (body: Partial<PbsSyncJobCreateRequest>) =>
      job
        ? pbsJobsApi.updateSyncJob(instanceId, job.job_id, body)
        : pbsJobsApi.createSyncJob(instanceId, datastoreName, body as PbsSyncJobCreateRequest),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
      // The Collector-cached list (DR posture, Inventory) may have changed too.
      queryClient.invalidateQueries({ queryKey: ["sync-jobs", instanceId] });
      onSaved?.(t("pbsJobs.syncSaved", { datastore: datastoreName }));
      onClose();
    },
    onError: (err) => setError(errorMessage(err, t("common.loadError"))),
  });

  const noRemotes = remotesQuery.data?.length === 0;

  return (
    <Modal open title={job ? t("pbsJobs.editSyncTitle") : t("pbsJobs.createSyncTitle")} onClose={onClose}>
      <form
        className={styles.form}
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          setError(null);
          if (!remoteStore.trim()) {
            setError(t("pbsJobs.syncRemoteStoreRequired"));
            return;
          }
          if (!job) {
            saveMutation.mutate(buildBody());
            return;
          }
          const body = changedFields(initialBody, buildBody());
          if (Object.keys(body).length === 0) onClose();
          else saveMutation.mutate(body);
        }}
      >
        <FormError error={error} />
        {noRemotes && <InlineAlert tone="info">{t("pbsJobs.syncNoRemotesHint")}</InlineAlert>}
        <div className="field">
          <label className="label" htmlFor="sync-remote">
            {t("pbsJobs.syncRemote")}
          </label>
          <select id="sync-remote" className="select" value={remoteName} onChange={(e) => setRemoteName(e.target.value)}>
            <option value="">{t("pbsJobs.syncRemoteNone")}</option>
            {remotesQuery.data?.map((remote) => (
              <option key={remote.name} value={remote.name}>
                {remote.name}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label className="label" htmlFor="sync-remote-store">
            {t("pbsJobs.syncRemoteStore")}
          </label>
          <input id="sync-remote-store" className="input" value={remoteStore} onChange={(e) => setRemoteStore(e.target.value)} required />
        </div>
        <div className="field">
          <label className="label" htmlFor="sync-schedule">
            {t("pbsJobs.syncSchedule")}
          </label>
          <input id="sync-schedule" className="input" value={schedule} onChange={(e) => setSchedule(e.target.value)} />
          <FieldHint>{t("pbsJobs.manualOnlyScheduleHint")}</FieldHint>
        </div>
        <div className="field">
          <label className="label" htmlFor="sync-ns">
            {t("pbsJobs.syncNs")}
          </label>
          <input id="sync-ns" className="input" value={ns} onChange={(e) => setNs(e.target.value)} />
        </div>
        <div className="field">
          <label className="label" htmlFor="sync-owner">
            {t("pbsJobs.syncOwner")}
          </label>
          <input id="sync-owner" className="input" value={owner} onChange={(e) => setOwner(e.target.value)} />
        </div>
        <div className="field">
          <label className="label" htmlFor="sync-comment">
            {t("pbsJobs.syncComment")}
          </label>
          <input id="sync-comment" className="input" value={comment} onChange={(e) => setComment(e.target.value)} />
        </div>
        <label className={styles.checkboxRow}>
          <input type="checkbox" checked={removeVanished} onChange={(e) => setRemoveVanished(e.target.checked)} />
          {t("pbsJobs.syncRemoveVanished")}
        </label>
        {job && <FieldHint>{t("pbsJobs.syncNoDisableHint")}</FieldHint>}
        {job && <FieldHint>{t("pbsJobs.clearFieldHint")}</FieldHint>}
        <FormActions pending={saveMutation.isPending} onClose={onClose} submitDisabled={!remoteStore.trim()} />
      </form>
    </Modal>
  );
}

function SyncJobsSection({ instanceId, datastoreName }: { instanceId: string; datastoreName: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [pendingDelete, setPendingDelete] = useState<PbsSyncJobOut | null>(null);
  const [editingJob, setEditingJob] = useState<PbsSyncJobOut | null | "new">(null);
  const queryKey = ["pbs-sync-jobs", instanceId, datastoreName];

  const capabilitiesQuery = useQuery({
    queryKey: ["pbs-sync-job-capabilities", instanceId],
    queryFn: () => pbsJobsApi.syncJobCapabilities(instanceId),
  });
  const canWrite = capabilitiesQuery.data?.can_write ?? false;
  const blockedReason = useWriteBlockedReason(capabilitiesQuery.data);
  const [feedback, setFeedback] = useState<JobFeedback | null>(null);

  const jobsQuery = useQuery({ queryKey, queryFn: () => pbsJobsApi.syncJobs(instanceId, datastoreName) });
  const deleteMutation = useMutation({
    mutationFn: (jobId: string) => pbsJobsApi.deleteSyncJob(instanceId, jobId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
      queryClient.invalidateQueries({ queryKey: ["sync-jobs", instanceId] });
      setFeedback({ tone: "info", message: t("pbsJobs.jobDeleted") });
    },
    onError: (err) => setFeedback({ tone: "danger", message: errorMessage(err, t("pbsJobs.deleteFailed")) }),
  });

  const columns: DataTableColumn<PbsSyncJobOut>[] = [
    { key: "remote_name", header: t("pbsJobs.syncRemote"), render: (row) => row.remote_name || t("pbsJobs.syncRemoteNone") },
    { key: "remote_store", header: t("pbsJobs.syncRemoteStore"), render: (row) => row.remote_store },
    { key: "schedule", header: t("pbsJobs.syncSchedule"), render: (row) => row.schedule ?? "—" },
    {
      key: "actions",
      header: t("common.actions"),
      render: (row) => (
        <JobRowActions
          canWrite={canWrite}
          blockedReason={blockedReason}
          onEdit={() => setEditingJob(row)}
          onDelete={() => setPendingDelete(row)}
        />
      ),
    },
  ];

  return (
    <div>
      <h3 className={styles.sectionSubtitle}>{t("pbsJobs.syncTitle")}</h3>
      {blockedReason && <InlineAlert tone="info">{blockedReason}</InlineAlert>}
      <JobFeedbackAlert feedback={feedback} />
      <QueryState query={jobsQuery} isEmpty={(data) => data.length === 0}>
        {(jobs) => <DataTable columns={columns} rows={jobs} rowKey={(row) => row.job_id} />}
      </QueryState>
      <div className={styles.formActions}>
        <button type="button" className="button" onClick={() => setEditingJob("new")} disabled={!canWrite}>
          {t("common.create")}
        </button>
      </div>
      <ConfirmActionDialog
        open={pendingDelete !== null}
        title={t("pbsJobs.confirmDeleteSyncTitle")}
        body={t("pbsJobs.confirmDeleteSyncBody", { schedule: pendingDelete?.schedule ?? "" })}
        confirmLabel={t("common.delete")}
        danger
        onConfirm={() => {
          if (pendingDelete) deleteMutation.mutate(pendingDelete.job_id);
          setPendingDelete(null);
        }}
        onCancel={() => setPendingDelete(null)}
      />
      {editingJob !== null && (
        <SyncJobFormModal
          instanceId={instanceId}
          datastoreName={datastoreName}
          job={editingJob === "new" ? null : editingJob}
          onClose={() => setEditingJob(null)}
          onSaved={(message) => setFeedback({ tone: "info", message })}
        />
      )}
    </div>
  );
}

// --- GC schedule ---------------------------------------------------------------

/** Edit form for one datastore's GC schedule. Removing the schedule is a
 * separate, confirmed action (`GcScheduleRow`'s Remove), so here the
 * field is required. */
export function GcScheduleFormModal({
  instanceId,
  datastoreName,
  currentSchedule,
  onClose,
  onSaved,
}: {
  instanceId: string;
  datastoreName: string;
  currentSchedule: string | null;
  onClose: () => void;
  onSaved?: (message: string) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [schedule, setSchedule] = useState(currentSchedule ?? "");
  const [error, setError] = useState<string | null>(null);

  const saveMutation = useMutation({
    mutationFn: (value: string) => pbsInstancesApi.setGcSchedule(instanceId, datastoreName, { gc_schedule: value }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["datastore-gc-schedule", instanceId, datastoreName] });
      onSaved?.(t("pbsJobs.gcSaved", { datastore: datastoreName }));
      onClose();
    },
    onError: (err) => setError(errorMessage(err, t("common.loadError"))),
  });

  return (
    <Modal open title={t("pbsJobs.gcEditTitle", { datastore: datastoreName })} onClose={onClose}>
      <form
        className={styles.form}
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          setError(null);
          const value = schedule.trim();
          if (!value) {
            setError(t("pbsJobs.gcScheduleRequired"));
            return;
          }
          if (value === (currentSchedule ?? "")) onClose();
          else saveMutation.mutate(value);
        }}
      >
        <FormError error={error} />
        <div className="field">
          <label className="label" htmlFor="gc-schedule">
            {t("pbsJobs.gcSchedule")}
          </label>
          <input id="gc-schedule" className="input" value={schedule} onChange={(e) => setSchedule(e.target.value)} required />
          <FieldHint>{t("pbsJobs.gcScheduleHint")}</FieldHint>
        </div>
        <FormActions pending={saveMutation.isPending} onClose={onClose} />
      </form>
    </Modal>
  );
}

/** One datastore's garbage-collection schedule as a table row with Edit
 * and Remove actions -- the same flow as a prune job row (Edit = form,
 * Remove = in-page confirmation), gated on the token's `Datastore.Modify`.
 * Results are reported through `onFeedback` so the page shows them in
 * one place. */
export function GcScheduleRow({
  instanceId,
  datastoreName,
  onFeedback,
}: {
  instanceId: string;
  datastoreName: string;
  onFeedback: (feedback: JobFeedback) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const queryKey = ["datastore-gc-schedule", instanceId, datastoreName];
  const gcQuery = useQuery({ queryKey, queryFn: () => pbsInstancesApi.getGcSchedule(instanceId, datastoreName) });
  const capabilitiesQuery = useQuery({
    queryKey: ["pbs-gc-schedule-capabilities", instanceId],
    queryFn: () => pbsJobsApi.gcScheduleCapabilities(instanceId),
  });
  const canWrite = capabilitiesQuery.data?.can_write ?? false;
  const blockedReason = useWriteBlockedReason(capabilitiesQuery.data);
  const [editing, setEditing] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const current = gcQuery.data?.gc_schedule ?? null;

  const removeMutation = useMutation({
    mutationFn: () => pbsInstancesApi.setGcSchedule(instanceId, datastoreName, { gc_schedule: null }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
      onFeedback({ tone: "info", message: t("pbsJobs.gcRemoved", { datastore: datastoreName }) });
    },
    onError: (err) => onFeedback({ tone: "danger", message: errorMessage(err, t("pbsJobs.deleteFailed")) }),
  });

  let scheduleCell: ReactNode;
  if (gcQuery.isLoading) scheduleCell = <span className="mutedText">{t("common.loading")}</span>;
  else if (gcQuery.isError) scheduleCell = <span className="mutedText">{errorMessage(gcQuery.error, t("common.loadError"))}</span>;
  else if (current) scheduleCell = current;
  else scheduleCell = <span className="mutedText">{t("pbsJobs.gcScheduleNone")}</span>;

  return (
    <tr>
      <td>{datastoreName}</td>
      <td>{scheduleCell}</td>
      <td>
        <JobRowActions
          canWrite={canWrite && !gcQuery.isError}
          blockedReason={blockedReason}
          onEdit={() => setEditing(true)}
          onDelete={() => setConfirmRemove(true)}
          deleteLabel={t("pbsJobs.gcRemove")}
          deleteDisabled={!current || removeMutation.isPending}
        />
        <ConfirmActionDialog
          open={confirmRemove}
          title={t("pbsJobs.gcConfirmRemoveTitle")}
          body={t("pbsJobs.gcConfirmRemoveBody", { datastore: datastoreName, schedule: current ?? "" })}
          confirmLabel={t("pbsJobs.gcRemove")}
          danger
          onConfirm={() => {
            setConfirmRemove(false);
            removeMutation.mutate();
          }}
          onCancel={() => setConfirmRemove(false)}
        />
        {editing && (
          <GcScheduleFormModal
            instanceId={instanceId}
            datastoreName={datastoreName}
            currentSchedule={current}
            onClose={() => setEditing(false)}
            onSaved={(message) => onFeedback({ tone: "info", message })}
          />
        )}
      </td>
    </tr>
  );
}

/** GC schedule table for a list of datastores, with one shared feedback
 * line -- used by the Prune & GC page and the per-datastore jobs modal. */
export function GcScheduleTable({ datastores }: { datastores: { instanceId: string; datastoreName: string; key: string }[] }) {
  const { t } = useTranslation();
  const [feedback, setFeedback] = useState<JobFeedback | null>(null);
  if (datastores.length === 0) return null;
  return (
    <>
      <JobFeedbackAlert feedback={feedback} />
      <table className="table">
        <thead>
          <tr>
            <th>{t("jobs.datastoreColumn")}</th>
            <th>{t("pbsJobs.gcSchedule")}</th>
            <th>{t("common.actions")}</th>
          </tr>
        </thead>
        <tbody>
          {datastores.map((d) => (
            <GcScheduleRow key={d.key} instanceId={d.instanceId} datastoreName={d.datastoreName} onFeedback={setFeedback} />
          ))}
        </tbody>
      </table>
    </>
  );
}

function GcScheduleSection({ instanceId, datastoreName }: { instanceId: string; datastoreName: string }) {
  const { t } = useTranslation();
  return (
    <div>
      <h3 className={styles.sectionSubtitle}>{t("pbsJobs.gcTitle")}</h3>
      <p className="mutedText">{t("pbsJobs.gcHint")}</p>
      <GcScheduleTable datastores={[{ instanceId, datastoreName, key: `${instanceId}:${datastoreName}` }]} />
    </div>
  );
}

// --- Modal shell ---------------------------------------------------------------

export function DatastoreJobsModal({ datastore, onClose }: { datastore: DatastoreOut; onClose: () => void }) {
  const { t } = useTranslation();
  return (
    <Modal open title={t("datastores.jobsModalTitle", { name: datastore.name })} onClose={onClose}>
      <PruneJobsSection instanceId={datastore.pbs_instance_id} datastoreName={datastore.name} />
      <VerifyJobsSection instanceId={datastore.pbs_instance_id} datastoreName={datastore.name} />
      <SyncJobsSection instanceId={datastore.pbs_instance_id} datastoreName={datastore.name} />
      <GcScheduleSection instanceId={datastore.pbs_instance_id} datastoreName={datastore.name} />
      <div className={`${styles.formActions} ${styles.modalFooterActions}`}>
        <button type="button" className="button" onClick={onClose}>
          {t("common.close")}
        </button>
      </div>
    </Modal>
  );
}
