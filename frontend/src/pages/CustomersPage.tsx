import { Fragment, useEffect, useRef, useState, type ChangeEvent, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import {
  billingApi,
  complianceAttestationsApi,
  customersApi,
  datastoresApi,
  fileIndexSettingsApi,
  instanceSettingsApi,
  legApplicabilityApi,
  notificationSettingsApi,
  pbsInstancesApi,
  pveClustersApi,
} from "../api/resources";
import type { CustomerImportResult, CustomerOut, ThreeTwoOneOneZeroLeg } from "../api/types";
import { ConfirmActionDialog } from "../components/ConfirmActionDialog";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { InlineAlert } from "../components/InlineAlert";
import { AlertMailerPanel } from "../components/AlertMailerPanel";
import { Modal } from "../components/Modal";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { QueryState } from "../components/QueryState";
import { StatusBadge } from "../components/StatusBadge";
import { formatBytes } from "../lib/formatBytes";
import styles from "./CustomersPage.module.css";

const QUERY_KEY = ["customers"];

/** The backend's slug rule (lowercase letters, digits, inner hyphens) applied
 * to a display name -- "Müller & Söhne AG" -> "mueller-soehne-ag". */
export function slugFromName(name: string): string {
  return name
    .toLowerCase()
    .replace(/ä/g, "ae")
    .replace(/ö/g, "oe")
    .replace(/ü/g, "ue")
    .replace(/ß/g, "ss")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 100)
    .replace(/-+$/, "");
}

function CreateCustomerForm({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  // The slug follows the name until the admin edits it themselves.
  const [slugTouched, setSlugTouched] = useState(false);
  const [debitorNumber, setDebitorNumber] = useState("");
  const [matchCode, setMatchCode] = useState("");
  const [error, setError] = useState<string | null>(null);

  const createMutation = useMutation({
    mutationFn: () => customersApi.create({ name, slug, debitor_number: debitorNumber, match_code: matchCode }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEY });
      onDone();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  return (
    <Panel title={t("customers.createTitle")} className={styles.createPanel}>
      <form
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          setError(null);
          createMutation.mutate();
        }}
      >
        {error && (
          <div className={styles.alert}>
            <InlineAlert>{error}</InlineAlert>
          </div>
        )}
        <div className="formGrid">
          <div className="field">
            <label className="label" htmlFor="customer-name">
              {t("customers.name")}
            </label>
            <input
              id="customer-name"
              className="input"
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                if (!slugTouched) setSlug(slugFromName(e.target.value));
              }}
              required
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="customer-slug">
              {t("customers.slug")}
            </label>
            <input
              id="customer-slug"
              className="input mono"
              value={slug}
              onChange={(e) => {
                setSlugTouched(true);
                setSlug(e.target.value);
              }}
              pattern="[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?"
              maxLength={100}
              required
              aria-describedby="customer-slug-hint"
            />
            <p id="customer-slug-hint" className="mutedText">
              {t("customers.slugHint")}
            </p>
          </div>
          <div className="field">
            <label className="label" htmlFor="customer-debitor">
              {t("customers.debitorNumber")}
            </label>
            <input
              id="customer-debitor"
              className="input"
              value={debitorNumber}
              onChange={(e) => setDebitorNumber(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="customer-matchcode">
              {t("customers.matchCode")}
            </label>
            <input
              id="customer-matchcode"
              className="input mono"
              maxLength={3}
              pattern="[A-Za-z]{3}"
              value={matchCode}
              onChange={(e) => setMatchCode(e.target.value.toUpperCase())}
              required
              aria-describedby="customer-matchcode-hint"
            />
            <p id="customer-matchcode-hint" className="mutedText">
              {t("customers.matchCodeHint")}
            </p>
          </div>
        </div>
        <div className="formActions">
          <button type="button" className="button" onClick={onDone}>
            {t("common.cancel")}
          </button>
          <button type="submit" className="button buttonPrimary" disabled={createMutation.isPending}>
            {t("common.create")}
          </button>
        </div>
      </form>
    </Panel>
  );
}

/** Manifest §13's reopened persistent File Indexing (ADR 0016): per-
 * customer opt-in + retention + schedule + time window, its own section
 * alongside the billing section above -- own query, own independent
 * mutations (create/update/enable-toggle/delete), not folded into the
 * name/debitor edit flow, mirroring how disableMutation/enableMutation
 * are already their own independent mutations in this same modal. */
function FileIndexSettingsSection({ customerId }: { customerId: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const settingsKey = ["file-index-settings", customerId];
  const [editing, setEditing] = useState(false);
  const [retentionDays, setRetentionDays] = useState("30");
  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [scheduleIntervalHours, setScheduleIntervalHours] = useState("24");
  const [windowStart, setWindowStart] = useState("");
  const [windowEnd, setWindowEnd] = useState("");
  const [guestExecPveClusterId, setGuestExecPveClusterId] = useState("");
  const [fullReindexEveryNRuns, setFullReindexEveryNRuns] = useState("");
  const [pendingDelete, setPendingDelete] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const settingsQuery = useQuery({
    queryKey: settingsKey,
    queryFn: () => fileIndexSettingsApi.get(customerId),
    retry: false,
  });
  const notConfigured = settingsQuery.isError && settingsQuery.error instanceof ApiError && settingsQuery.error.status === 404;
  // Guest-exec File Indexing (ADR 0021): 'guest_exec_ephemeral' clones
  // boot on a lab-enabled cluster, same pool Recovery Lab already draws
  // from -- fetched here regardless of whether a group has opted into
  // that method yet, since this picker is where an operator sets it up
  // in advance.
  const labClustersQuery = useQuery({ queryKey: ["pve-clusters"], queryFn: pveClustersApi.list });
  const labClusters = labClustersQuery.data?.filter((c) => c.is_lab_compute) ?? [];

  useEffect(() => {
    const data = settingsQuery.data;
    if (!data) return;
    setRetentionDays(String(data.retention_days));
    setScheduleEnabled(data.schedule_enabled);
    setScheduleIntervalHours(data.schedule_interval_hours ? String(data.schedule_interval_hours) : "24");
    setWindowStart(data.window_start_time ?? "");
    setWindowEnd(data.window_end_time ?? "");
    setGuestExecPveClusterId(data.guest_exec_pve_cluster_id ?? "");
    setFullReindexEveryNRuns(data.full_reindex_every_n_runs ? String(data.full_reindex_every_n_runs) : "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settingsQuery.data]);

  const formBody = () => ({
    retention_days: Number(retentionDays),
    schedule_enabled: scheduleEnabled,
    schedule_interval_hours: scheduleEnabled ? Number(scheduleIntervalHours) : null,
    window_start_time: windowStart || null,
    window_end_time: windowEnd || null,
    guest_exec_pve_cluster_id: guestExecPveClusterId || null,
    full_reindex_every_n_runs: fullReindexEveryNRuns.trim() === "" ? null : Number(fullReindexEveryNRuns),
  });

  const createMutation = useMutation({
    mutationFn: () => fileIndexSettingsApi.create(customerId, formBody()),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: settingsKey });
      setEditing(false);
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const updateMutation = useMutation({
    mutationFn: () => fileIndexSettingsApi.update(customerId, formBody()),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: settingsKey });
      setEditing(false);
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const toggleEnabledMutation = useMutation({
    mutationFn: () => fileIndexSettingsApi.update(customerId, { enabled: !settingsQuery.data?.enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: settingsKey }),
  });

  const deleteMutation = useMutation({
    mutationFn: () => fileIndexSettingsApi.remove(customerId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: settingsKey }),
  });

  return (
    <Panel
      testId="file-index-settings"
      title={t("fileIndex.settingsTitle")}
      actions={
        !editing &&
        (notConfigured ? (
          <button type="button" className="button buttonPrimary" onClick={() => setEditing(true)}>
            {t("common.enable")}
          </button>
        ) : (
          settingsQuery.data && (
            <>
              <button type="button" className="button buttonDanger" onClick={() => setPendingDelete(true)}>
                {t("common.delete")}
              </button>
              <button type="button" className="button" onClick={() => toggleEnabledMutation.mutate()}>
                {settingsQuery.data.enabled ? t("common.disable") : t("common.enable")}
              </button>
              <button type="button" className="button buttonPrimary" onClick={() => setEditing(true)}>
                {t("common.edit")}
              </button>
            </>
          )
        ))
      }
    >
      {error && (
        <div className={styles.alert}>
          <InlineAlert>{error}</InlineAlert>
        </div>
      )}

      {notConfigured && !editing && <p className={styles.bodyText}>{t("fileIndex.notConfigured")}</p>}

      {settingsQuery.data && !editing && (
        <>
          <dl className="detailGrid">
            <dt>{t("common.status")}</dt>
            <dd>
              {settingsQuery.data.enabled ? (
                <StatusBadge tone="ok" label={t("common.statusActive")} />
              ) : (
                <StatusBadge tone="danger" label={t("common.statusDisabled")} />
              )}
            </dd>
            <dt>{t("fileIndex.retentionDays")}</dt>
            <dd>{settingsQuery.data.retention_days}</dd>
            <dt>{t("fileIndex.schedule")}</dt>
            <dd>
              {settingsQuery.data.schedule_enabled
                ? t("fileIndex.scheduleEveryNHours", { count: settingsQuery.data.schedule_interval_hours ?? 0 })
                : t("fileIndex.scheduleDisabled")}
            </dd>
            <dt>{t("fileIndex.window")}</dt>
            <dd>
              {settingsQuery.data.window_start_time && settingsQuery.data.window_end_time
                ? `${settingsQuery.data.window_start_time}–${settingsQuery.data.window_end_time} UTC`
                : t("fileIndex.windowUnrestricted")}
            </dd>
            <dt>{t("fileIndex.guestExecCluster")}</dt>
            <dd>
              {settingsQuery.data.guest_exec_pve_cluster_id
                ? (labClusters.find((c) => c.id === settingsQuery.data?.guest_exec_pve_cluster_id)?.display_name ??
                   settingsQuery.data.guest_exec_pve_cluster_id)
                : t("fileIndex.guestExecClusterNotSet")}
            </dd>
            <dt>{t("fileIndex.fullReindexEveryNRuns")}</dt>
            <dd>
              {settingsQuery.data.full_reindex_every_n_runs
                ? t("fileIndex.fullReindexEveryNRunsValue", { count: settingsQuery.data.full_reindex_every_n_runs })
                : t("fileIndex.fullReindexDisabled")}
            </dd>
          </dl>
        </>
      )}

      {editing && (
        <form
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            setError(null);
            if (settingsQuery.data) updateMutation.mutate();
            else createMutation.mutate();
          }}
        >
          <div className="formGrid">
            <div className="field">
              <label className="label" htmlFor="file-index-retention">
                {t("fileIndex.retentionDays")}
              </label>
              <input
                id="file-index-retention"
                type="number"
                min={1}
                className="input"
                value={retentionDays}
                onChange={(e) => setRetentionDays(e.target.value)}
                required
              />
            </div>
            <div className="field">
              <label className="label" htmlFor="file-index-schedule-enabled">
                {t("fileIndex.schedule")}
              </label>
              <label className="checkboxField">
                <input
                  id="file-index-schedule-enabled"
                  type="checkbox"
                  checked={scheduleEnabled}
                  onChange={(e) => setScheduleEnabled(e.target.checked)}
                />{" "}
                {t("fileIndex.scheduleEnabled")}
              </label>
            </div>
            {scheduleEnabled && (
              <div className="field">
                <label className="label" htmlFor="file-index-schedule-interval">
                  {t("fileIndex.scheduleIntervalHours")}
                </label>
                <input
                  id="file-index-schedule-interval"
                  type="number"
                  min={1}
                  className="input"
                  value={scheduleIntervalHours}
                  onChange={(e) => setScheduleIntervalHours(e.target.value)}
                  required
                />
              </div>
            )}
            <div className="field">
              <label className="label" htmlFor="file-index-window-start">
                {t("fileIndex.windowStart")}
              </label>
              <input
                id="file-index-window-start"
                type="time"
                className="input"
                value={windowStart}
                onChange={(e) => setWindowStart(e.target.value)}
              />
            </div>
            <div className="field">
              <label className="label" htmlFor="file-index-window-end">
                {t("fileIndex.windowEnd")}
              </label>
              <input
                id="file-index-window-end"
                type="time"
                className="input"
                value={windowEnd}
                onChange={(e) => setWindowEnd(e.target.value)}
              />
            </div>
            <div className="field">
              <label className="label" htmlFor="file-index-guest-exec-cluster">
                {t("fileIndex.guestExecCluster")}
              </label>
              <select
                id="file-index-guest-exec-cluster"
                className="select"
                value={guestExecPveClusterId}
                onChange={(e) => setGuestExecPveClusterId(e.target.value)}
              >
                <option value="">{t("fileIndex.guestExecClusterNotSet")}</option>
                {labClusters.map((cluster) => (
                  <option key={cluster.id} value={cluster.id}>
                    {cluster.display_name}
                  </option>
                ))}
              </select>
              <p className="mutedText">{t("fileIndex.guestExecClusterHint")}</p>
            </div>
            <div className="field">
              <label className="label" htmlFor="file-index-full-reindex">
                {t("fileIndex.fullReindexEveryNRuns")}
              </label>
              <input
                id="file-index-full-reindex"
                type="number"
                min={1}
                className="input"
                placeholder={t("fileIndex.fullReindexDisabled")}
                value={fullReindexEveryNRuns}
                onChange={(e) => setFullReindexEveryNRuns(e.target.value)}
              />
              <p className="mutedText">{t("fileIndex.fullReindexHint")}</p>
            </div>
          </div>
          <div className="formActions">
            <button type="button" className="button" onClick={() => setEditing(false)}>
              {t("common.cancel")}
            </button>
            <button
              type="submit"
              className="button buttonPrimary"
              disabled={createMutation.isPending || updateMutation.isPending}
            >
              {t("common.save")}
            </button>
          </div>
        </form>
      )}

      <ConfirmActionDialog
        open={pendingDelete}
        title={t("fileIndex.confirmDeleteTitle")}
        body={t("fileIndex.confirmDeleteBody")}
        danger
        confirmLabel={t("common.delete")}
        onCancel={() => setPendingDelete(false)}
        onConfirm={() => {
          deleteMutation.mutate();
          setPendingDelete(false);
        }}
      />
    </Panel>
  );
}

/** 3-2-1-1-0 self-assessment for the two legs RESURGAM cannot observe at
 * the customer level (production copy count, media-type diversity) --
 * raised directly by the user: audited (author-attributed) and kept as
 * permanent history, never overwritten. `platform_admin` or that
 * customer's own `customer_admin` can record one (enforced server-side;
 * a 403 here just surfaces as a normal mutation error, same as every
 * other admin-gated action in this modal).
 */
function ComplianceAttestationSection({ customerId }: { customerId: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const historyKey = ["customer-compliance-attestations", customerId];
  const [copyCount, setCopyCount] = useState("3");
  const [mediaTypes, setMediaTypes] = useState("");
  const [error, setError] = useState<string | null>(null);

  const historyQuery = useQuery({ queryKey: historyKey, queryFn: () => complianceAttestationsApi.listForCustomer(customerId) });
  const copyCountHistory = historyQuery.data?.filter((a) => a.dimension === "production_copy_count") ?? [];
  const mediaTypesHistory = historyQuery.data?.filter((a) => a.dimension === "media_type_diversity") ?? [];
  const currentCopyCount = copyCountHistory[0] ?? null;
  const currentMediaTypes = mediaTypesHistory[0] ?? null;

  const recordCopyCountMutation = useMutation({
    mutationFn: () => complianceAttestationsApi.createForCustomer(customerId, { dimension: "production_copy_count", value: Number(copyCount) }),
    onSuccess: () => {
      setError(null);
      queryClient.invalidateQueries({ queryKey: historyKey });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const recordMediaTypesMutation = useMutation({
    mutationFn: () =>
      complianceAttestationsApi.createForCustomer(customerId, {
        dimension: "media_type_diversity",
        value: mediaTypes.split(",").map((v) => v.trim()).filter((v) => v !== ""),
      }),
    onSuccess: () => {
      setError(null);
      setMediaTypes("");
      queryClient.invalidateQueries({ queryKey: historyKey });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  return (
    <Panel testId="compliance-attestations" title={t("customers.complianceTitle")}>
      <p className="panelIntro">{t("customers.complianceIntro")}</p>
      {error && (
        <div className={styles.alert}>
          <InlineAlert>{error}</InlineAlert>
        </div>
      )}

      <dl className="detailGrid">
        <dt>{t("customers.productionCopyCount")}</dt>
        <dd>{currentCopyCount ? String(currentCopyCount.value) : t("datastores.attestationNotAttested")}</dd>
        <dt>{t("customers.mediaTypeDiversity")}</dt>
        <dd>{currentMediaTypes && Array.isArray(currentMediaTypes.value) ? currentMediaTypes.value.join(", ") : t("datastores.attestationNotAttested")}</dd>
      </dl>

      <div className={`formGrid ${styles.recordForm}`}>
        <div className="field">
          <label className="label" htmlFor="compliance-copy-count">
            {t("customers.recordProductionCopyCount")}
          </label>
          <div className={styles.inlineControl}>
            <input
              id="compliance-copy-count"
              type="number"
              min={0}
              className="input"
              value={copyCount}
              onChange={(e) => setCopyCount(e.target.value)}
            />
            <button type="button" className="button" onClick={() => recordCopyCountMutation.mutate()} disabled={recordCopyCountMutation.isPending}>
              {t("datastores.attestationRecord")}
            </button>
          </div>
        </div>
        <div className="field">
          <label className="label" htmlFor="compliance-media-types">
            {t("customers.recordMediaTypeDiversity")}
          </label>
          <div className={styles.inlineControl}>
            <input
              id="compliance-media-types"
              className="input"
              placeholder={t("customers.mediaTypesPlaceholder")}
              value={mediaTypes}
              onChange={(e) => setMediaTypes(e.target.value)}
            />
            <button type="button" className="button" onClick={() => recordMediaTypesMutation.mutate()} disabled={recordMediaTypesMutation.isPending}>
              {t("datastores.attestationRecord")}
            </button>
          </div>
        </div>
      </div>

      {historyQuery.data && historyQuery.data.length > 0 && (
        <dl className={`detailGrid ${styles.history}`}>
          <dt>{t("datastores.attestationHistory")}</dt>
          <dd className={styles.historyList}>
            {historyQuery.data.map((row) => (
              <div key={row.id} className="mutedText">
                {row.dimension === "production_copy_count" ? t("customers.productionCopyCount") : t("customers.mediaTypeDiversity")}:{" "}
                {Array.isArray(row.value) ? row.value.join(", ") : String(row.value)} — {row.asserted_by_display_name},{" "}
                {new Date(row.created_at).toLocaleString()}
              </div>
            ))}
          </dd>
        </dl>
      )}
    </Panel>
  );
}

const LEGS: ThreeTwoOneOneZeroLeg[] = ["copies", "media_types", "offsite", "immutable", "zero_errors"];
const LEG_TITLE_KEY: Record<ThreeTwoOneOneZeroLeg, string> = {
  copies: "recoveryEvidence.legCopies",
  media_types: "recoveryEvidence.legMediaTypes",
  offsite: "recoveryEvidence.legOffsite",
  immutable: "recoveryEvidence.legImmutable",
  zero_errors: "recoveryEvidence.legZeroErrors",
};

/** Raised directly by the user: not every 3-2-1-1-0 leg is contractually
 * relevant to every customer (e.g. a deliberately single-site customer
 * has no "offsite" obligation) -- a `platform_admin`-only toggle per
 * leg, distinct from the attestation section above (a customer's own
 * `customer_admin` can self-report a fact, but cannot turn off their own
 * compliance checks -- a materially different, easier-to-game action).
 * A disabled leg shows as "Not applicable" in the Recovery Evidence
 * report instead of counting as unmet.
 */
function LegApplicabilitySection({ customerId }: { customerId: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const listKey = ["leg-applicability", customerId];
  const [error, setError] = useState<string | null>(null);

  const listQuery = useQuery({ queryKey: listKey, queryFn: () => legApplicabilityApi.list(customerId) });

  const toggleMutation = useMutation({
    mutationFn: ({ leg, applicable }: { leg: ThreeTwoOneOneZeroLeg; applicable: boolean }) =>
      legApplicabilityApi.update(customerId, leg, applicable),
    onSuccess: () => {
      setError(null);
      queryClient.invalidateQueries({ queryKey: listKey });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const byLeg = new Map((listQuery.data ?? []).map((row) => [row.leg, row]));

  return (
    <Panel testId="leg-applicability" title={t("customers.legApplicabilityTitle")}>
      <p className="panelIntro">{t("customers.legApplicabilityIntro")}</p>
      {error && (
        <div className={styles.alert}>
          <InlineAlert>{error}</InlineAlert>
        </div>
      )}

      <div className={styles.checkList}>
        {LEGS.map((leg) => {
          const row = byLeg.get(leg);
          const applicable = row?.applicable ?? true;
          return (
            <label key={leg} className={styles.checkRow}>
              <input
                type="checkbox"
                checked={applicable}
                onChange={(e) => toggleMutation.mutate({ leg, applicable: e.target.checked })}
              />{" "}
              {t(LEG_TITLE_KEY[leg])}
              {row?.updated_by_display_name && (
                <span className="mutedText">
                  {" — "}
                  {t("customers.legApplicabilityChangedBy", { name: row.updated_by_display_name, when: row.updated_at ? new Date(row.updated_at).toLocaleString() : "" })}
                </span>
              )}
            </label>
          );
        })}
      </div>
    </Panel>
  );
}

// Alarm mails have their own panel (AlertMailerPanel: several recipients,
// severity, reminders, test mail), so only reports and billing are here.
type NotificationCategory = "report" | "billing";
const NOTIFICATION_CATEGORIES: NotificationCategory[] = ["report", "billing"];
const NOTIFICATION_CATEGORY_TITLE_KEY: Record<NotificationCategory, string> = {
  report: "customers.notificationReport",
  billing: "customers.notificationBilling",
};

/** Item 3 of the mailer/PDF-redesign/notifications build order: a
 * Report and an Alarms receiver email per customer, plus Billing as a
 * third independent category -- raised directly by the user. Each
 * category has its own recipient address and its own on/off switch
 * (confirmed via AskUserQuestion): an address can be on file without
 * that category's notifications being live yet.
 */
function NotificationSettingsSection({ customerId }: { customerId: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const settingsKey = ["notification-settings", customerId];
  const [editing, setEditing] = useState(false);
  const [emails, setEmails] = useState<Record<NotificationCategory, string>>({ report: "", billing: "" });
  const [enabled, setEnabled] = useState<Record<NotificationCategory, boolean>>({ report: false, billing: false });
  const [error, setError] = useState<string | null>(null);

  const settingsQuery = useQuery({ queryKey: settingsKey, queryFn: () => notificationSettingsApi.get(customerId) });

  const startEditing = (data: typeof settingsQuery.data) => {
    if (!data) return;
    setEmails({
      report: data.report_notification_email ?? "", billing: data.billing_notification_email ?? "",
    });
    setEnabled({
      report: data.report_notifications_enabled, billing: data.billing_notifications_enabled,
    });
    setEditing(true);
  };

  const saveMutation = useMutation({
    mutationFn: () =>
      notificationSettingsApi.update(customerId, {
        report_notification_email: emails.report.trim() === "" ? null : emails.report.trim(), report_notifications_enabled: enabled.report,
        billing_notification_email: emails.billing.trim() === "" ? null : emails.billing.trim(), billing_notifications_enabled: enabled.billing,
      }),
    onSuccess: () => {
      setError(null);
      setEditing(false);
      queryClient.invalidateQueries({ queryKey: settingsKey });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const emailFor = (data: NonNullable<typeof settingsQuery.data>, category: NotificationCategory) =>
    category === "report" ? data.report_notification_email : data.billing_notification_email;
  const enabledFor = (data: NonNullable<typeof settingsQuery.data>, category: NotificationCategory) =>
    category === "report" ? data.report_notifications_enabled : data.billing_notifications_enabled;

  return (
    <Panel
      testId="notification-settings"
      title={t("customers.notificationSettingsTitle")}
      actions={
        settingsQuery.data &&
        !editing && (
          <button type="button" className="button buttonPrimary" onClick={() => startEditing(settingsQuery.data)}>
            {t("common.edit")}
          </button>
        )
      }
    >
      <p className="panelIntro">{t("customers.notificationSettingsIntro")}</p>
      {error && (
        <div className={styles.alert}>
          <InlineAlert>{error}</InlineAlert>
        </div>
      )}

      {settingsQuery.data && !editing && (
        <dl className="detailGrid">
          {NOTIFICATION_CATEGORIES.map((category) => (
            <Fragment key={category}>
              <dt>{t(NOTIFICATION_CATEGORY_TITLE_KEY[category])}</dt>
              <dd>
                <span>{emailFor(settingsQuery.data!, category) ?? t("customers.notificationNoEmail")}</span>
                {" — "}
                <span>{enabledFor(settingsQuery.data!, category) ? t("common.statusActive") : t("common.statusDisabled")}</span>
              </dd>
            </Fragment>
          ))}
        </dl>
      )}

      {editing && (
        <>
          <div className="formGrid">
            {NOTIFICATION_CATEGORIES.map((category) => (
              <div key={category} className="field">
                <label className="label" htmlFor={`notification-email-${category}`}>
                  {t(NOTIFICATION_CATEGORY_TITLE_KEY[category])}
                </label>
                <div className={styles.inlineControl}>
                  <input
                    id={`notification-email-${category}`}
                    type="email"
                    className="input"
                    value={emails[category]}
                    onChange={(e) => setEmails((prev) => ({ ...prev, [category]: e.target.value }))}
                  />
                  <label className="checkboxField">
                    <input
                      type="checkbox"
                      checked={enabled[category]}
                      onChange={(e) => setEnabled((prev) => ({ ...prev, [category]: e.target.checked }))}
                    />{" "}
                    {t("common.enable")}
                  </label>
                </div>
              </div>
            ))}
          </div>
          <div className="formActions">
            <button type="button" className="button" onClick={() => setEditing(false)} disabled={saveMutation.isPending}>
              {t("common.cancel")}
            </button>
            <button type="button" className="button buttonPrimary" onClick={() => saveMutation.mutate()} disabled={saveMutation.isPending}>
              {t("common.save")}
            </button>
          </div>
        </>
      )}
    </Panel>
  );
}

function CustomerDetailModal({ customer, onClose }: { customer: CustomerOut; onClose: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(customer.name);
  const [debitorNumber, setDebitorNumber] = useState(customer.debitor_number);
  const [matchCode, setMatchCode] = useState(customer.match_code);
  const [reportRetentionDays, setReportRetentionDays] = useState(customer.report_retention_days === null ? "" : String(customer.report_retention_days));
  const [error, setError] = useState<string | null>(null);

  const datastoresQuery = useQuery({ queryKey: ["datastores"], queryFn: datastoresApi.list });
  const instancesQuery = useQuery({ queryKey: ["pbs-instances"], queryFn: pbsInstancesApi.list });
  const billingQuery = useQuery({
    queryKey: ["billing-customers", customer.id],
    queryFn: () => billingApi.get(customer.id),
  });
  const instanceSettingsQuery = useQuery({ queryKey: ["instance-settings"], queryFn: instanceSettingsApi.get });

  useEffect(() => {
    setEditing(false);
    setName(customer.name);
    setDebitorNumber(customer.debitor_number);
    setMatchCode(customer.match_code);
    setReportRetentionDays(customer.report_retention_days === null ? "" : String(customer.report_retention_days));
    setError(null);
  }, [customer]);

  const updateMutation = useMutation({
    mutationFn: () =>
      customersApi.update(customer.id, {
        name, debitor_number: debitorNumber, match_code: matchCode,
        // Empty input -> explicit null, clearing the override back to the
        // platform floor; a real number -> that override. Either way this
        // key is always included, never omitted, so the backend's
        // model_fields_set check always sees the caller's real intent.
        report_retention_days: reportRetentionDays.trim() === "" ? null : Number(reportRetentionDays),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEY });
      setEditing(false);
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const disableMutation = useMutation({
    mutationFn: () => customersApi.disable(customer.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEY }),
  });
  const enableMutation = useMutation({
    mutationFn: () => customersApi.enable(customer.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEY }),
  });

  const ownDatastores = (datastoresQuery.data ?? []).filter((d) => d.customer_id === customer.id);
  const instanceNameFor = (id: string) => instancesQuery.data?.find((i) => i.id === id)?.display_name ?? id;

  if (!editing) {
    return (
      <Modal open size="large" title={customer.name} onClose={onClose}>
        <div className="stack">
          <dl className="detailGrid">
            <dt>{t("customers.slug")}</dt>
            <dd>{customer.slug}</dd>
            <dt>{t("customers.debitorNumber")}</dt>
            <dd>{customer.debitor_number}</dd>
            <dt>{t("customers.matchCode")}</dt>
            <dd>{customer.match_code}</dd>
            <dt>{t("customers.reportRetentionDays")}</dt>
            <dd>
              {customer.report_retention_days !== null
                ? t("customers.reportRetentionDaysValue", { count: customer.report_retention_days })
                : t("customers.reportRetentionUsingFloor", { count: instanceSettingsQuery.data?.min_report_retention_days ?? "—" })}
            </dd>
            <dt>{t("common.status")}</dt>
            <dd>
              {customer.disabled ? (
                <StatusBadge tone="danger" label={t("common.statusDisabled")} />
              ) : (
                <StatusBadge tone="ok" label={t("common.statusActive")} />
              )}
            </dd>
          </dl>

          <Panel title={t("customers.datastores")} padded={false}>
            {ownDatastores.length === 0 ? (
              <p className={`mutedText ${styles.emptyList}`}>{t("common.empty")}</p>
            ) : (
              <ul className={styles.list}>
                {ownDatastores.map((d) => (
                  <li key={d.id} className={styles.listRow}>
                    <span>{d.name}</span>
                    <span className="mutedText">
                      {instanceNameFor(d.pbs_instance_id)} · {d.role === "offsite" ? t("datastores.roleOffsite") : t("datastores.rolePrimary")}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Panel>

          <Panel title={t("customers.billing")}>
            {billingQuery.data && (
              <dl className="detailGrid">
                <dt>{t("billing.vmCount")}</dt>
                <dd>{billingQuery.data.vm_count}</dd>
                <dt>{t("billing.ctCount")}</dt>
                <dd>{billingQuery.data.ct_count}</dd>
                <dt>{t("billing.hostCount")}</dt>
                <dd>{billingQuery.data.host_count}</dd>
                <dt>{t("billing.estimatedBytes")}</dt>
                <dd>{formatBytes(billingQuery.data.estimated_physical_bytes)}</dd>
                <dt>{t("billing.estimatedMonthlyCost")}</dt>
                <dd>
                  {billingQuery.data.estimated_monthly_cost === null
                    ? "—"
                    : `${billingQuery.data.estimated_monthly_cost} ${billingQuery.data.currency}`}
                </dd>
              </dl>
            )}
          </Panel>

          <FileIndexSettingsSection customerId={customer.id} />

          <ComplianceAttestationSection customerId={customer.id} />

          <LegApplicabilitySection customerId={customer.id} />

          <NotificationSettingsSection customerId={customer.id} />

          <AlertMailerPanel customerId={customer.id} />
        </div>

        <div className={`formActions ${styles.modalFooter}`}>
          <button type="button" className="button" onClick={onClose}>
            {t("common.close")}
          </button>
          {customer.disabled ? (
            <button type="button" className="button" onClick={() => enableMutation.mutate()}>
              {t("common.enable")}
            </button>
          ) : (
            <button type="button" className="button buttonDanger" onClick={() => disableMutation.mutate()}>
              {t("common.disable")}
            </button>
          )}
          <button type="button" className="button buttonPrimary" onClick={() => setEditing(true)}>
            {t("common.edit")}
          </button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal open size="large" title={customer.name} onClose={onClose}>
      <form
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          setError(null);
          updateMutation.mutate();
        }}
      >
        {error && (
          <div className={styles.alert}>
            <InlineAlert>{error}</InlineAlert>
          </div>
        )}
        <div className="formGrid">
          <div className="field">
            <label className="label" htmlFor="edit-customer-name">
              {t("customers.name")}
            </label>
            <input
              id="edit-customer-name"
              className="input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="edit-customer-debitor">
              {t("customers.debitorNumber")}
            </label>
            <input
              id="edit-customer-debitor"
              className="input"
              value={debitorNumber}
              onChange={(e) => setDebitorNumber(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="edit-customer-matchcode">
              {t("customers.matchCode")}
            </label>
            <input
              id="edit-customer-matchcode"
              className="input"
              maxLength={3}
              value={matchCode}
              onChange={(e) => setMatchCode(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="edit-customer-report-retention">
              {t("customers.reportRetentionDays")}
            </label>
            <input
              id="edit-customer-report-retention"
              type="number"
              min={1}
              className="input"
              placeholder={t("customers.reportRetentionUsingFloor", { count: instanceSettingsQuery.data?.min_report_retention_days ?? "—" })}
              value={reportRetentionDays}
              onChange={(e) => setReportRetentionDays(e.target.value)}
            />
            <p className="mutedText">{t("customers.reportRetentionHint")}</p>
          </div>
        </div>
        <div className="formActions">
          <button type="button" className="button" onClick={() => setEditing(false)}>
            {t("common.cancel")}
          </button>
          <button type="submit" className="button buttonPrimary" disabled={updateMutation.isPending}>
            {t("common.save")}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function CustomersPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [selectedCustomer, setSelectedCustomer] = useState<CustomerOut | null>(null);
  const [importResult, setImportResult] = useState<CustomerImportResult | null>(null);
  const [importError, setImportError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const query = useQuery({ queryKey: QUERY_KEY, queryFn: customersApi.list });

  const exportMutation = useMutation({ mutationFn: () => customersApi.exportCsv() });
  const importMutation = useMutation({
    mutationFn: (file: File) => customersApi.importCsv(file),
    onSuccess: (result) => {
      setImportResult(result);
      setImportError(null);
      queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
    onError: (err) => {
      setImportResult(null);
      setImportError(err instanceof ApiError ? err.message : t("common.loadError"));
    },
  });

  const handleImportFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file) importMutation.mutate(file);
  };

  const selected = selectedCustomer ? query.data?.find((c) => c.id === selectedCustomer.id) ?? selectedCustomer : null;

  const columns: DataTableColumn<CustomerOut>[] = [
    { key: "name", header: t("customers.name"), render: (row) => row.name },
    { key: "slug", header: t("customers.slug"), render: (row) => row.slug },
    { key: "debitor", header: t("customers.debitorNumber"), render: (row) => row.debitor_number },
    { key: "matchCode", header: t("customers.matchCode"), render: (row) => row.match_code },
    {
      key: "status",
      header: t("common.status"),
      render: (row) =>
        row.disabled ? (
          <StatusBadge tone="danger" label={t("common.statusDisabled")} />
        ) : (
          <StatusBadge tone="ok" label={t("common.statusActive")} />
        ),
    },
  ];

  return (
    <div>
      <PageHeader
        title={t("nav.administrationCustomers")}
        actions={
          <>
            <input
              ref={fileInputRef}
              type="file"
              accept=".csv,text/csv"
              hidden
              onChange={handleImportFileChange}
            />
            <button
              type="button"
              className="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={importMutation.isPending}
            >
              {t("customers.importCsv")}
            </button>
            <button
              type="button"
              className="button"
              onClick={() => exportMutation.mutate()}
              disabled={exportMutation.isPending}
            >
              {t("customers.exportCsv")}
            </button>
            {!showCreateForm && (
              <button type="button" className="button buttonPrimary" onClick={() => setShowCreateForm(true)}>
                {t("common.create")}
              </button>
            )}
          </>
        }
      />

      {importError && (
        <div className={styles.alert}>
          <InlineAlert>{importError}</InlineAlert>
        </div>
      )}
      {importResult && (
        <Panel className={styles.importResult}>
          <dl className="detailGrid">
            <dt>{t("customers.importCreated")}</dt>
            <dd>{importResult.created}</dd>
            <dt>{t("customers.importUpdated")}</dt>
            <dd>{importResult.updated}</dd>
            {importResult.errors.length > 0 && (
              <>
                <dt>{t("customers.importErrors")}</dt>
                <dd>
                  <ul className={styles.errorList}>
                    {importResult.errors.map((e) => (
                      <li key={e.row}>
                        {t("customers.importRow", { row: e.row })}: {e.message}
                      </li>
                    ))}
                  </ul>
                </dd>
              </>
            )}
          </dl>
        </Panel>
      )}

      {showCreateForm && <CreateCustomerForm onDone={() => setShowCreateForm(false)} />}

      <QueryState query={query} isEmpty={(data) => data.length === 0}>
        {(customers) => (
          <DataTable columns={columns} rows={customers} rowKey={(row) => row.id} onRowClick={(row) => setSelectedCustomer(row)} />
        )}
      </QueryState>

      {selected && <CustomerDetailModal customer={selected} onClose={() => setSelectedCustomer(null)} />}
    </div>
  );
}
