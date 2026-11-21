import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router-dom";

import { ApiError } from "../api/client";
import { billingApi, customersApi, fileIndexSettingsApi } from "../api/resources";
import { AlertMailerPanel } from "../components/AlertMailerPanel";
import { EmptyState } from "../components/EmptyState";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { QueryState } from "../components/QueryState";
import { StatusBadge } from "../components/StatusBadge";
import styles from "./CustomerPanelPage.module.css";

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** exponent).toFixed(exponent === 0 ? 0 : 1)} ${units[exponent]}`;
}

/** The first real page behind the top-left Customer Switcher (raised
 * directly by the user, "to prepare a Customer Panel") -- a single
 * tenant's own at-a-glance view, built entirely from endpoints that
 * already exist (billing/usage, File Index settings), rather than
 * duplicating Protected Workloads' own per-group table here. Quick
 * links hand off to the existing pages for anything deeper; they don't
 * (yet) carry this customer as a pre-applied filter -- each of those
 * pages already has its own "Nach Mandant filtern" picker one click
 * away.
 */
export function CustomerPanelPage() {
  const { t } = useTranslation();
  const { customerId } = useParams<{ customerId: string }>();

  const customerQuery = useQuery({
    queryKey: ["customers", customerId],
    queryFn: () => customersApi.get(customerId!),
    enabled: !!customerId,
  });
  const billingQuery = useQuery({
    queryKey: ["billing", "customer", customerId],
    queryFn: () => billingApi.get(customerId!),
    enabled: !!customerId,
  });
  const fileIndexQuery = useQuery({
    queryKey: ["file-index-settings", customerId],
    queryFn: () => fileIndexSettingsApi.get(customerId!),
    enabled: !!customerId,
    retry: false,
  });
  const fileIndexNotConfigured =
    fileIndexQuery.isError && fileIndexQuery.error instanceof ApiError && fileIndexQuery.error.status === 404;

  if (!customerId) {
    return <EmptyState title={t("common.empty")} />;
  }

  return (
    <QueryState query={customerQuery} isEmpty={() => false}>
      {(customer) => (
        <div>
          <PageHeader
            title={customer.name}
            breadcrumbs={[{ label: t("nav.administrationCustomers"), to: "/administration/customers" }, { label: customer.name }]}
            actions={
              <StatusBadge
                tone={customer.disabled ? "unknown" : "ok"}
                label={customer.disabled ? t("common.statusDisabled") : t("common.statusActive")}
              />
            }
            description={
              <span className={styles.identity}>
                <span>
                  <span className={styles.identityLabel}>{t("customers.matchCode")}</span> {customer.match_code}
                </span>
                <span>
                  <span className={styles.identityLabel}>{t("customers.debitorNumber")}</span> {customer.debitor_number}
                </span>
                <span>
                  <span className={styles.identityLabel}>{t("customers.slug")}</span> {customer.slug}
                </span>
              </span>
            }
          />

          <div className={`kpiGrid ${styles.kpiRow}`}>
            <div className="kpi">
              <span className="kpiLabel">{t("nav.backupsVms")}</span>
              <span className="kpiValue">{billingQuery.data?.vm_count ?? "—"}</span>
            </div>
            <div className="kpi">
              <span className="kpiLabel">{t("nav.backupsContainers")}</span>
              <span className="kpiValue">{billingQuery.data?.ct_count ?? "—"}</span>
            </div>
            <div className="kpi">
              <span className="kpiLabel">{t("nav.backupsHosts")}</span>
              <span className="kpiValue">{billingQuery.data?.host_count ?? "—"}</span>
            </div>
            <div className="kpi">
              <span className="kpiLabel">{t("customerPanel.estimatedStorage")}</span>
              <span className="kpiValue">
                {billingQuery.data ? formatBytes(billingQuery.data.estimated_physical_bytes) : "—"}
                {billingQuery.data && !billingQuery.data.physical_bytes_estimate_complete && (
                  <span className={styles.estimateFlag} title={t("customerPanel.estimateIncomplete")}>
                    *
                  </span>
                )}
              </span>
            </div>
          </div>

          <div className={styles.panelsGrid}>
            <Panel title={t("fileIndex.settingsTitle")}>
              {fileIndexNotConfigured ? (
                <p className="mutedText">{t("customerPanel.fileIndexNotConfigured")}</p>
              ) : fileIndexQuery.data ? (
                <dl className="detailGrid">
                  <dt>{t("common.status")}</dt>
                  <dd>
                    <StatusBadge
                      tone={fileIndexQuery.data.enabled ? "ok" : "unknown"}
                      label={fileIndexQuery.data.enabled ? t("common.statusActive") : t("common.statusDisabled")}
                    />
                  </dd>
                  <dt>{t("fileIndex.retentionDays")}</dt>
                  <dd>{fileIndexQuery.data.retention_days}</dd>
                </dl>
              ) : (
                <p className="mutedText">{t("common.loading")}</p>
              )}
            </Panel>

            <AlertMailerPanel customerId={customerId!} />

            <Panel title={t("customerPanel.quickLinks")}>
              <div className={styles.quickLinks}>
                <Link className="button" to="/backups/vms">
                  {t("nav.backupsVms")}
                </Link>
                <Link className="button" to="/backups/containers">
                  {t("nav.backupsContainers")}
                </Link>
                <Link className="button" to="/recovery/restore-center">
                  {t("nav.recoveryRestoreCenter")}
                </Link>
                <Link className="button" to="/recovery/recovery-lab">
                  {t("nav.recoveryLab")}
                </Link>
                <Link className="button" to="/reporting/recovery-evidence">
                  {t("nav.reportingRecoveryEvidence")}
                </Link>
              </div>
            </Panel>
          </div>
        </div>
      )}
    </QueryState>
  );
}
