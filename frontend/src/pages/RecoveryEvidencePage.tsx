import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { customersApi, reportingApi } from "../api/resources";
import { ApiError, downloadFile } from "../api/client";
import type { OffsiteStatus, RecoveryEvidenceReportOut, ReportHistoryOut } from "../api/types";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import { InlineAlert } from "../components/InlineAlert";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { QueryState } from "../components/QueryState";
import { Spinner } from "../components/Spinner";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { useSelectedCustomer } from "../contexts/CustomerContext";
import { protectionStatusPresentation, verifyStatusPresentation } from "../lib/statusPresentation";
import styles from "./RecoveryEvidencePage.module.css";

const OFFSITE_STATUS_TONE: Record<OffsiteStatus, StatusTone> = {
  ok: "ok",
  stored_offsite: "ok",
  lagging: "warning",
  missing: "danger",
  local_copy_only: "warning",
  target_not_connected: "warning",
  no_copy_job: "danger",
  excepted: "info",
  not_required: "unknown",
};

function formatDateTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

function formatPercent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/** Trailing windows, not calendar-aligned ones -- "last 7 days" and "last
 * month" both mean "the N days/months up to today", matching the
 * backend's own 90-day trailing default (`_DEFAULT_WINDOW_DAYS` in
 * `api/v1/reporting.py`) rather than switching to a different convention
 * (e.g. "previous calendar month") just for these two presets.
 */
function datePreset(unit: "days" | "months", amount: number): { from: string; to: string } {
  const to = new Date();
  const from = new Date();
  if (unit === "days") from.setDate(from.getDate() - amount);
  else from.setMonth(from.getMonth() - amount);
  return { from: isoDate(from), to: isoDate(to) };
}

const TRAFFIC_LIGHT_TONE = { green: "ok", amber: "warning", red: "danger" } as const;
const TRAFFIC_LIGHT_LABEL_KEY = {
  green: "recoveryEvidence.trafficLightGreen",
  amber: "recoveryEvidence.trafficLightAmber",
  red: "recoveryEvidence.trafficLightRed",
} as const;
const LEG_STATUS_TONE = { met: "ok", not_met: "danger", not_attested: "unknown", not_applicable: "info" } as const;
const LEG_STATUS_LABEL_KEY = {
  met: "recoveryEvidence.legMet",
  not_met: "recoveryEvidence.legNotMet",
  not_attested: "recoveryEvidence.legNotAttested",
  not_applicable: "recoveryEvidence.legNotApplicable",
} as const;
const LEG_TITLE_KEY = {
  copies: "recoveryEvidence.legCopies",
  media_types: "recoveryEvidence.legMediaTypes",
  offsite: "recoveryEvidence.legOffsite",
  immutable: "recoveryEvidence.legImmutable",
  zero_errors: "recoveryEvidence.legZeroErrors",
} as const;

function SectionControls({ report, sectionKey }: { report: RecoveryEvidenceReportOut; sectionKey: string }) {
  if (!report.framework) return null;
  const controls = report.section_controls[sectionKey];
  if (!controls || controls.length === 0) return null;
  return (
    <div className={styles.controls}>
      {controls.map((c) => (
        <span key={`${c.framework}-${c.control}`} className={styles.controlBadge} title={c.title}>
          {c.framework} {c.control}
        </span>
      ))}
    </div>
  );
}

/** A permanent, checksummed archive of every past PDF generation for
 * this customer -- raised directly by the user: a generated report is
 * evidence handed to an auditor, so it should stay re-downloadable
 * byte-for-byte later, not just regenerable (which could legitimately
 * produce different numbers as the underlying data changes). Own
 * `useQuery`, independent of the report's own load state, so the
 * history list still shows even if the current report query is loading
 * or failed.
 */
function ReportHistoryPanel({ customerId }: { customerId: string }) {
  const { t } = useTranslation();
  const historyQuery = useQuery({
    queryKey: ["recovery-evidence-history", customerId],
    queryFn: () => reportingApi.recoveryEvidenceHistory(customerId),
  });

  const redownload = useMutation({
    mutationFn: (row: ReportHistoryOut) =>
      downloadFile(
        reportingApi.recoveryEvidenceHistoryPdfPath(row.id),
        `recovery-evidence-${customerId}-${row.date_to}.pdf`,
      ),
  });

  const columns: DataTableColumn<ReportHistoryOut>[] = [
    { key: "generated", header: t("recoveryEvidence.historyGeneratedAt"), render: (r) => formatDateTime(r.created_at) },
    { key: "period", header: t("recoveryEvidence.historyPeriod"), render: (r) => `${r.date_from} – ${r.date_to}` },
    { key: "framework", header: t("recoveryEvidence.historyFramework"), render: (r) => (r.framework ? t("common.yes") : t("common.no")) },
    { key: "checksum", header: t("recoveryEvidence.historyChecksum"), render: (r) => <code title={r.sha256_checksum}>{r.sha256_checksum.slice(0, 12)}…</code> },
    {
      key: "redownload",
      header: "",
      render: (r) => {
        const busy = redownload.isPending && redownload.variables?.id === r.id;
        return (
          <button type="button" className="button" disabled={busy} onClick={() => redownload.mutate(r)}>
            {busy && <Spinner />}
            {busy ? t("recoveryEvidence.downloading") : t("recoveryEvidence.historyRedownload")}
          </button>
        );
      },
    },
  ];

  return (
    <Panel title={t("recoveryEvidence.historyTitle")}>
      <p className="panelIntro">{t("recoveryEvidence.historyIntro")}</p>
      {redownload.isError && (
        <InlineAlert>
          {redownload.error instanceof ApiError ? redownload.error.message : t("recoveryEvidence.pdfFailed")}
        </InlineAlert>
      )}
      <QueryState query={historyQuery}>
        {(history) =>
          history.length === 0 ? (
            <p className="mutedText">{t("recoveryEvidence.historyEmpty")}</p>
          ) : (
            <DataTable columns={columns} rows={history} rowKey={(r) => r.id} />
          )
        }
      </QueryState>
    </Panel>
  );
}

/** Manifest Reporting -- compliance evidence for one customer at a time,
 * raised directly by the user asking what ISO27001/SOC2/ISO9001 reports
 * a Backup-as-a-Service customer would need. Same skeleton as
 * `CapacityTrendsPage`/`BillingPage` (useQuery -> QueryState -> DataTable
 * per section), with two things neither of those pages has: a required
 * date-range filter and a server-generated PDF download.
 */
export function RecoveryEvidencePage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { selectedCustomerId } = useSelectedCustomer();
  const [customerId, setCustomerId] = useState(selectedCustomerId ?? "");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [framework, setFramework] = useState(false);

  // Follows the sidebar's tenant switcher -- raised directly by the
  // user.
  useEffect(() => {
    setCustomerId(selectedCustomerId ?? "");
  }, [selectedCustomerId]);

  const customersQuery = useQuery({ queryKey: ["customers"], queryFn: customersApi.list });

  const reportQuery = useQuery({
    queryKey: ["recovery-evidence", customerId, dateFrom, dateTo, framework],
    queryFn: () =>
      reportingApi.recoveryEvidence({
        customerId,
        dateFrom: dateFrom || undefined,
        dateTo: dateTo || undefined,
        framework,
      }),
    enabled: customerId !== "",
  });

  // Rendering the PDF server-side takes a while (charts, every section) --
  // show that it is being generated, and any failure, instead of a button
  // that seems to do nothing.
  const pdfMutation = useMutation({
    mutationFn: () =>
      downloadFile(
        reportingApi.recoveryEvidencePdfPath({
          customerId,
          dateFrom: dateFrom || undefined,
          dateTo: dateTo || undefined,
          framework,
        }),
        `recovery-evidence-${customerId}.pdf`,
      ),
    // Every generation is archived; show it in the history right away.
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["recovery-evidence-history", customerId] }),
  });

  const backupHistoryColumns: DataTableColumn<RecoveryEvidenceReportOut["backup_history"][number]>[] = [
    { key: "workload", header: t("recoveryEvidence.workload"), render: (r) => r.workload_label },
    { key: "type", header: t("common.type"), render: (r) => r.backup_type },
    { key: "namespace", header: t("protectedWorkloads.namespace"), render: (r) => r.namespace || "/" },
    { key: "when", header: t("fileIndex.pointInTime"), render: (r) => formatDateTime(r.snapshot_time) },
    {
      key: "verify",
      header: t("snapshots.verifyStatus"),
      render: (r) => {
        const { tone, label } = verifyStatusPresentation(r.verify_status as never, t);
        return <StatusBadge tone={tone} label={label} />;
      },
    },
  ];

  const policyColumns: DataTableColumn<RecoveryEvidenceReportOut["policy_adherence"][number]>[] = [
    { key: "workload", header: t("recoveryEvidence.workload"), render: (r) => r.workload_label },
    { key: "policy", header: t("recoveryEvidence.policy"), render: (r) => r.policy_name ?? "—" },
    {
      key: "status",
      header: t("recoveryEvidence.status"),
      render: (r) => {
        const { tone, label } = protectionStatusPresentation(r.status as never, t);
        return <StatusBadge tone={tone} label={label} />;
      },
    },
    { key: "lastBackup", header: t("recoveryEvidence.lastBackup"), render: (r) => formatDateTime(r.last_backup_at) },
  ];

  const failedJobColumns: DataTableColumn<RecoveryEvidenceReportOut["failed_jobs"][number]>[] = [
    { key: "source", header: t("common.type"), render: (r) => r.source },
    { key: "label", header: t("recoveryEvidence.workload"), render: (r) => r.label },
    { key: "status", header: t("recoveryEvidence.status"), render: (r) => r.status },
    { key: "when", header: t("fileIndex.pointInTime"), render: (r) => formatDateTime(r.occurred_at) },
    { key: "error", header: "Error", render: (r) => r.error_message ?? "—" },
  ];

  const auditColumns: DataTableColumn<RecoveryEvidenceReportOut["audit_trail"][number]>[] = [
    { key: "when", header: t("fileIndex.pointInTime"), render: (r) => formatDateTime(r.occurred_at) },
    { key: "action", header: t("auditLog.action"), render: (r) => r.action },
    { key: "target", header: t("auditLog.targetType"), render: (r) => r.target_type ?? "—" },
  ];

  const restoreTestColumns: DataTableColumn<RecoveryEvidenceReportOut["restore_test_evidence"][number]>[] = [
    { key: "group", header: t("recoveryEvidence.applicationGroup"), render: (r) => r.application_group_name },
    { key: "workload", header: t("recoveryEvidence.workload"), render: (r) => r.workload_label },
    { key: "run", header: t("recoveryEvidence.runStatus"), render: (r) => r.run_status },
    {
      key: "member",
      header: t("recoveryEvidence.memberStatus"),
      render: (r) => (
        <StatusBadge tone={r.member_status === "passed" ? "ok" : r.member_status === "failed" ? "danger" : "info"} label={r.member_status} />
      ),
    },
    { key: "tested", header: t("recoveryEvidence.tested"), render: (r) => formatDateTime(r.tested_at) },
  ];

  const accessControlColumns: DataTableColumn<RecoveryEvidenceReportOut["access_control"][number]>[] = [
    { key: "user", header: t("recoveryEvidence.user"), render: (r) => r.user_display_name },
    { key: "role", header: t("recoveryEvidence.role"), render: (r) => r.role },
    { key: "scope", header: t("recoveryEvidence.scope"), render: (r) => r.scope },
    { key: "granted", header: t("recoveryEvidence.granted"), render: (r) => formatDateTime(r.granted_at) },
  ];

  const pbsDatastoreAccessColumns: DataTableColumn<RecoveryEvidenceReportOut["pbs_datastore_access"][number]>[] = [
    { key: "datastore", header: t("recoveryEvidence.datastore"), render: (r) => r.datastore_name },
    { key: "path", header: t("recoveryEvidence.aclPath"), render: (r) => r.path },
    { key: "role", header: t("recoveryEvidence.role"), render: (r) => r.role },
    { key: "subject", header: t("recoveryEvidence.aclSubject"), render: (r) => r.subject },
    { key: "type", header: t("recoveryEvidence.aclSubjectType"), render: (r) => r.subject_type },
  ];

  const offsiteColumns: DataTableColumn<RecoveryEvidenceReportOut["offsite_status"][number]>[] = [
    { key: "workload", header: t("recoveryEvidence.workload"), render: (r) => r.workload_label },
    {
      key: "copy",
      header: t("recoveryEvidence.offsiteCopy"),
      render: (r) =>
        r.status ? (
          <StatusBadge tone={OFFSITE_STATUS_TONE[r.status]} label={t(`backupGaps.status.${r.status}`)} />
        ) : (
          // Reports generated before ADR 0032 have no status.
          <StatusBadge tone={r.has_offsite_copy ? "ok" : "danger"} label={r.has_offsite_copy ? t("common.yes") : t("common.no")} />
        ),
    },
    { key: "location", header: t("backupGaps.copyAt"), render: (r) => r.copy_location ?? "—" },
    { key: "latestCopy", header: t("backupGaps.latestCopy"), render: (r) => formatDateTime(r.latest_copy_at ?? null) },
    { key: "lag", header: t("backupGaps.lag"), render: (r) => (r.lag_hours === null || r.lag_hours === undefined ? "—" : `${Math.round(r.lag_hours)} h`) },
    {
      key: "note",
      header: t("recoveryEvidence.offsiteNote"),
      render: (r) =>
        r.exception_reason
          ? `${r.exception_reason} (${r.exception_by ?? "?"}, ${r.exception_at ? new Date(r.exception_at).toLocaleDateString() : "?"})`
          : "—",
    },
  ];

  return (
    <div>
      <PageHeader
        title={t("recoveryEvidence.title")}
        actions={
          customerId !== "" && (
            <button
              type="button"
              className="button buttonPrimary"
              disabled={pdfMutation.isPending}
              aria-busy={pdfMutation.isPending}
              onClick={() => pdfMutation.mutate()}
            >
              {pdfMutation.isPending && <Spinner />}
              {pdfMutation.isPending ? t("recoveryEvidence.generatingPdf") : t("recoveryEvidence.downloadPdf")}
            </button>
          )
        }
        description={t("recoveryEvidence.description")}
      />
      {pdfMutation.isPending && (
        <div className={styles.pdfStatus} data-testid="pdf-generating">
          <InlineAlert tone="info" hideIcon>
            {t("recoveryEvidence.generatingPdfHint")}
          </InlineAlert>
        </div>
      )}
      {pdfMutation.isError && (
        <div className={styles.pdfStatus}>
          <InlineAlert>
            {pdfMutation.error instanceof ApiError ? pdfMutation.error.message : t("recoveryEvidence.pdfFailed")}
          </InlineAlert>
        </div>
      )}

      <div className="toolbar">
        <div className="field">
          <label className="label" htmlFor="recovery-evidence-customer">
            {t("fileRecovery.customer")}
          </label>
          <select
            id="recovery-evidence-customer"
            className="select"
            value={customerId}
            onChange={(e) => setCustomerId(e.target.value)}
          >
            <option value="">{t("recoveryEvidence.selectCustomer")}</option>
            {customersQuery.data?.map((customer) => (
              <option key={customer.id} value={customer.id}>
                {customer.name}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label className="label" htmlFor="recovery-evidence-date-from">
            {t("recoveryEvidence.dateFrom")}
          </label>
          <input
            id="recovery-evidence-date-from"
            type="date"
            className="input"
            value={dateFrom}
            onChange={(e) => setDateFrom(e.target.value)}
          />
        </div>
        <div className="field">
          <label className="label" htmlFor="recovery-evidence-date-to">
            {t("recoveryEvidence.dateTo")}
          </label>
          <input
            id="recovery-evidence-date-to"
            type="date"
            className="input"
            value={dateTo}
            onChange={(e) => setDateTo(e.target.value)}
          />
        </div>
        <button
          type="button"
          className="button"
          onClick={() => {
            const { from, to } = datePreset("days", 7);
            setDateFrom(from);
            setDateTo(to);
          }}
        >
          {t("recoveryEvidence.presetLast7Days")}
        </button>
        <button
          type="button"
          className="button"
          onClick={() => {
            const { from, to } = datePreset("months", 1);
            setDateFrom(from);
            setDateTo(to);
          }}
        >
          {t("recoveryEvidence.presetLastMonth")}
        </button>
        <label className="checkboxField">
          <input type="checkbox" checked={framework} onChange={(e) => setFramework(e.target.checked)} />
          {t("recoveryEvidence.frameworkToggle")}
        </label>
      </div>

      {customerId === "" ? (
        <EmptyState title={t("recoveryEvidence.pickCustomerFirst")} />
      ) : (
        <div className="stack">
        <QueryState query={reportQuery}>
          {(report) => (
            <div className="stack">
              <Panel
                title={t("recoveryEvidence.managementSummary")}
                actions={<SectionControls report={report} sectionKey="management_summary" />}
              >
                <div className={styles.summaryHeader}>
                  <StatusBadge
                    tone={TRAFFIC_LIGHT_TONE[report.management_summary.traffic_light]}
                    label={t(TRAFFIC_LIGHT_LABEL_KEY[report.management_summary.traffic_light])}
                  />
                  <span className={styles.tierBadge}>
                    {t("recoveryEvidence.protectionLevel")}:{" "}
                    {t(`recoveryEvidence.tier.${report.protection_level.tier}`, {
                      defaultValue: report.protection_level.label,
                    })}
                  </span>
                </div>
                <div className={styles.kpiRow}>
                  <span>{t("recoveryEvidence.backupSuccessRate")}: {formatPercent(report.management_summary.backup_success_rate)}</span>
                  <span>{t("recoveryEvidence.verificationPassRate")}: {formatPercent(report.management_summary.verification_pass_rate)}</span>
                  <span>
                    {t("recoveryEvidence.restoreTestPassRate")}:{" "}
                    {report.management_summary.restore_test_pass_rate === null
                      ? t("recoveryEvidence.notApplicable")
                      : formatPercent(report.management_summary.restore_test_pass_rate)}
                  </span>
                  <span>{t("recoveryEvidence.policyAdherenceRate")}: {formatPercent(report.management_summary.policy_adherence_rate)}</span>
                  <span>{t("recoveryEvidence.unresolvedFailedJobs")}: {report.management_summary.unresolved_failed_jobs}</span>
                  {report.management_summary.four_eyes_approval_rate !== null && (
                    <span>{t("recoveryEvidence.fourEyesApprovalRate")}: {formatPercent(report.management_summary.four_eyes_approval_rate)}</span>
                  )}
                </div>
                {/* Server-rendered, entirely from numeric aggregates (reporting/charts.py, reporting/timeline_svg.py)
                    -- never from customer-editable input, which is why dangerouslySetInnerHTML is safe here specifically. */}
                <div className={styles.timeline} dangerouslySetInnerHTML={{ __html: report.management_summary.kpi_bars_svg }} />
                <div className={styles.timeline} dangerouslySetInnerHTML={{ __html: report.protection_level.tier_distribution_svg }} />
                <div className={styles.timeline} dangerouslySetInnerHTML={{ __html: report.management_summary.period_timeline_svg }} />
              </Panel>

              <Panel
                title={t("recoveryEvidence.threeTwoOneOneZero")}
                actions={<SectionControls report={report} sectionKey="three_two_one_one_zero" />}
              >
                <div className={styles.legStrip}>
                  {report.three_two_one_one_zero.map((leg) => (
                    <div key={leg.leg} className={styles.leg}>
                      <div className={styles.legTitle}>{t(LEG_TITLE_KEY[leg.leg])}</div>
                      <StatusBadge tone={LEG_STATUS_TONE[leg.status]} label={t(LEG_STATUS_LABEL_KEY[leg.status])} />
                      <div>{leg.detail}</div>
                      {leg.attested_by && (
                        <div className={styles.legAttestation}>
                          {t("recoveryEvidence.attestedBy", { name: leg.attested_by, when: formatDateTime(leg.attested_at) })}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </Panel>

              <Panel
                title={t("recoveryEvidence.backupHistory")}
                actions={<SectionControls report={report} sectionKey="backup_history" />}
                padded={report.backup_history.length === 0}
              >
                {report.backup_history.length === 0 ? (
                  <p className="mutedText">{t("recoveryEvidence.noBackupHistory")}</p>
                ) : (
                  <DataTable columns={backupHistoryColumns} rows={report.backup_history} rowKey={(r) => `${r.backup_group_id}-${r.snapshot_time}`} />
                )}
              </Panel>

              <Panel
                title={t("recoveryEvidence.policyAdherence")}
                actions={<SectionControls report={report} sectionKey="policy_adherence" />}
                padded={report.policy_adherence.length === 0}
              >
                {report.policy_adherence.length === 0 ? (
                  <p className="mutedText">{t("recoveryEvidence.noPolicyAdherence")}</p>
                ) : (
                  <DataTable columns={policyColumns} rows={report.policy_adherence} rowKey={(r) => r.backup_group_id} />
                )}
              </Panel>

              <Panel
                title={t("recoveryEvidence.verificationResults")}
                actions={<SectionControls report={report} sectionKey="verification_results" />}
              >
                <div className={styles.summaryRow}>
                  <span>OK: {report.verification_results.summary.ok}</span>
                  <span>Failed: {report.verification_results.summary.failed}</span>
                  <span>None: {report.verification_results.summary.none}</span>
                  <span>Unknown: {report.verification_results.summary.unknown}</span>
                </div>
              </Panel>

              <Panel
                title={t("recoveryEvidence.failedJobs")}
                actions={<SectionControls report={report} sectionKey="failed_jobs" />}
                padded={report.failed_jobs.length === 0}
              >
                {report.failed_jobs.length === 0 ? (
                  <p className="mutedText">{t("recoveryEvidence.noFailedJobs")}</p>
                ) : (
                  <DataTable columns={failedJobColumns} rows={report.failed_jobs} rowKey={(r) => r.id} />
                )}
              </Panel>

              <Panel
                title={t("recoveryEvidence.auditTrail")}
                actions={<SectionControls report={report} sectionKey="audit_trail" />}
              >
                {report.audit_trail.length === 0 ? (
                  <p className="mutedText">{t("recoveryEvidence.noAuditEvents")}</p>
                ) : (
                  <DataTable columns={auditColumns} rows={report.audit_trail} rowKey={(r) => r.id} />
                )}
                <p className={styles.disclaimer}>{t("recoveryEvidence.auditTrailDisclaimer")}</p>
              </Panel>

              <Panel
                title={t("recoveryEvidence.encryptionAttestation")}
                actions={<SectionControls report={report} sectionKey="encryption_attestation" />}
              >
                <p className="panelIntro">{t("recoveryEvidence.encryptionIntro")}</p>
                {report.encryption_attestation.instances.length > 0 && (
                  <DataTable
                    columns={[
                      { key: "name", header: t("common.name"), render: (r) => r.display_name },
                      { key: "trust", header: t("pbsInstances.trustMode"), render: (r) => r.tls_trust_mode },
                      { key: "fingerprint", header: t("pbsInstances.fingerprint"), render: (r) => r.tls_fingerprint_masked ?? "—" },
                    ]}
                    rows={report.encryption_attestation.instances}
                    rowKey={(r) => r.pbs_instance_id}
                  />
                )}
              </Panel>

              <Panel
                title={t("recoveryEvidence.restoreTestEvidence")}
                actions={<SectionControls report={report} sectionKey="restore_test_evidence" />}
                padded={report.restore_test_evidence.length === 0}
              >
                {report.restore_test_evidence.length === 0 ? (
                  <p className="mutedText">{t("recoveryEvidence.noRestoreTestEvidence")}</p>
                ) : (
                  <DataTable
                    columns={restoreTestColumns}
                    rows={report.restore_test_evidence}
                    rowKey={(r) => `${r.backup_group_id}-${r.tested_at}`}
                  />
                )}
              </Panel>

              <Panel
                title={t("recoveryEvidence.offsiteStatus")}
                actions={<SectionControls report={report} sectionKey="offsite_status" />}
                padded={report.offsite_status.length === 0}
              >
                {report.offsite_status.length === 0 ? (
                  <p className="mutedText">{t("recoveryEvidence.noOffsiteStatus")}</p>
                ) : (
                  <DataTable columns={offsiteColumns} rows={report.offsite_status} rowKey={(r) => r.backup_group_id} />
                )}
              </Panel>

              <Panel
                title={t("recoveryEvidence.accessControl")}
                actions={<SectionControls report={report} sectionKey="access_control" />}
                padded={report.access_control.length === 0}
              >
                {report.access_control.length === 0 ? (
                  <p className="mutedText">{t("recoveryEvidence.noAccessControl")}</p>
                ) : (
                  <DataTable
                    columns={accessControlColumns}
                    rows={report.access_control}
                    rowKey={(r) => `${r.user_display_name}-${r.role}-${r.scope}`}
                  />
                )}
              </Panel>

              <Panel
                title={t("recoveryEvidence.pbsDatastoreAccess")}
                actions={<SectionControls report={report} sectionKey="pbs_datastore_access" />}
              >
                <p className="panelIntro">{t("recoveryEvidence.pbsDatastoreAccessHint")}</p>
                {report.pbs_datastore_access.length === 0 ? (
                  <p className="mutedText">{t("recoveryEvidence.noPbsDatastoreAccess")}</p>
                ) : (
                  <DataTable
                    columns={pbsDatastoreAccessColumns}
                    rows={report.pbs_datastore_access}
                    rowKey={(r) => `${r.datastore_name}-${r.path}-${r.role}-${r.subject}`}
                  />
                )}
              </Panel>
            </div>
          )}
        </QueryState>
        <ReportHistoryPanel customerId={customerId} />
        </div>
      )}
    </div>
  );
}
