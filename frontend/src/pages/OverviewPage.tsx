import { useQueries, useQuery } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { alarmsApi, customersApi, datastoresApi, pbsInstancesApi, pveClustersApi, tasksApi } from "../api/resources";
import type { AlarmOut, BackupGroupOut, TaskRunOut } from "../api/types";
import { EmptyState } from "../components/EmptyState";
import { OverdueBackupsWindow } from "../components/OverdueBackupsWindow";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { StatusBadge } from "../components/StatusBadge";
import { useAuth } from "../contexts/AuthContext";
import { backupStaleness } from "../lib/backupStaleness";
import { formatBytes } from "../lib/formatBytes";
import { taskStatusPresentation } from "../lib/statusPresentation";
import styles from "./OverviewPage.module.css";

const RECENT_TASKS_LIMIT = 8;
const OVERDUE_LIST_LIMIT = 5;

/** Manifest §9.2's actual Dashboard scope -- recent/running tasks,
 * backup/verify/prune/gc/sync status, overdue backups, and datastore
 * utilization -- none of which the previous version showed (it was just
 * three counts and an alarms list, raised directly by the user as
 * asymmetric and uninformative: a narrow capped-width column next to a
 * mostly empty page). Deliberately still only four compact KPI tiles
 * (manifest §26.5's "maximal vier kompakte Kennzahlen"); the added depth
 * comes from the panels below, each a real bounded summary rather than a
 * bigger number (manifest §26.9 forbids decorative dashboard numbers
 * without operational context). */
export function OverviewPage() {
  const { t } = useTranslation();
  const pbsQuery = useQuery({ queryKey: ["pbs-instances"], queryFn: pbsInstancesApi.list });
  const pveQuery = useQuery({ queryKey: ["pve-clusters"], queryFn: pveClustersApi.list });
  const datastoresQuery = useQuery({ queryKey: ["datastores"], queryFn: datastoresApi.list });
  const customersQuery = useQuery({ queryKey: ["customers"], queryFn: customersApi.list });
  const alarmsQuery = useQuery({ queryKey: ["alarms"], queryFn: alarmsApi.list });

  // Petrol & Brass KPI tiles drill into the matching list -- those lists
  // are platform_admin pages, so the tiles only link for that role.
  const { hasRole } = useAuth();
  const tilesLink = hasRole("platform_admin");
  const tiles = [
    { labelKey: "overview.pbsInstances", value: pbsQuery.data?.length, ok: pbsQuery.isSuccess, to: "/infrastructure/pbs-instances" },
    { labelKey: "overview.pveClusters", value: pveQuery.data?.length, ok: pveQuery.isSuccess, to: "/infrastructure/pve-targets" },
    { labelKey: "overview.datastores", value: datastoresQuery.data?.length, ok: datastoresQuery.isSuccess, to: "/infrastructure/datastores" },
    { labelKey: "overview.customers", value: customersQuery.data?.length, ok: customersQuery.isSuccess, to: "/administration/customers" },
  ].filter((tile) => tile.ok);

  const instanceIds = pbsQuery.data?.map((i) => i.id) ?? [];
  const instanceName = (id: string) => pbsQuery.data?.find((i) => i.id === id)?.display_name ?? id;

  const recentTasksQuery = useQuery({
    queryKey: ["overview-recent-tasks", instanceIds],
    queryFn: () => tasksApi.list({ instanceIds, limit: RECENT_TASKS_LIMIT }),
    enabled: instanceIds.length > 0,
  });

  // No "every backup group across every datastore" endpoint exists yet --
  // same accepted N+1 fetch-and-filter-client-side shape
  // ProtectedWorkloadsPage already uses at the same real data scale
  // (~135 groups). `backupStaleness` is the exact helper that page's
  // per-row staleness flag already comes from, kept in the one shared
  // module so this fleet-wide summary and that row-level flag can never
  // disagree on what "overdue" means.
  const datastoreIds = datastoresQuery.data?.map((d) => d.id) ?? [];
  const groupsQueries = useQueries({
    queries: datastoreIds.map((id) => ({
      queryKey: ["backup-groups", id],
      queryFn: () => datastoresApi.backupGroups(id),
    })),
  });
  const groupsLoading = datastoreIds.length > 0 && groupsQueries.some((q) => q.isLoading);
  const liveGroups = groupsQueries
    .flatMap((q) => q.data ?? [])
    .filter((g) => g.superseded_at === null && g.removed_from_pbs_at === null);
  const overdueGroups = liveGroups
    .map((group) => ({ group, staleness: backupStaleness(group.last_backup_at) }))
    .filter((entry): entry is { group: BackupGroupOut; staleness: "danger" | "warning" } => entry.staleness !== null)
    .sort((a, b) => (a.staleness === b.staleness ? 0 : a.staleness === "danger" ? -1 : 1));

  const usageQueries = useQueries({
    queries: datastoreIds.map((id) => ({
      queryKey: ["datastore-usage", id],
      queryFn: () => datastoresApi.usage(id),
    })),
  });
  const datastoreUsage = datastoreIds.map((id, index) => ({
    datastore: datastoresQuery.data?.find((d) => d.id === id),
    usage: usageQueries[index]?.data,
    isLoading: usageQueries[index]?.isLoading ?? false,
  }));

  const [overdueWindowOpen, setOverdueWindowOpen] = useState(false);
  const datastoreName = useCallback(
    (id: string) => datastoresQuery.data?.find((d) => d.id === id)?.name ?? id,
    [datastoresQuery.data],
  );
  const customerName = useCallback(
    (id: string | null) => (id ? (customersQuery.data?.find((c) => c.id === id)?.name ?? null) : null),
    [customersQuery.data],
  );

  const alarms = [...(alarmsQuery.data ?? [])].sort((a, b) => (a.severity === b.severity ? 0 : a.severity === "critical" ? -1 : 1));

  return (
    <div>
      <PageHeader title={t("overview.title")} />
      <div className={`kpiGrid ${styles.kpiRow}`}>
        {tiles.map((tile) => {
          const body = (
            <>
              <span className="kpiLabel">{t(tile.labelKey)}</span>
              <span className="kpiValue">{tile.value}</span>
            </>
          );
          return tilesLink ? (
            <Link key={tile.labelKey} to={tile.to} className="kpi">
              {body}
            </Link>
          ) : (
            <div key={tile.labelKey} className="kpi">
              {body}
            </div>
          );
        })}
      </div>

      <div className={styles.panelsGrid}>
        {alarmsQuery.isSuccess && (
          <Panel title={t("overview.alarms")} className={`${styles.panel} ${styles.alarmsArea}`}>
            {alarms.length === 0 ? (
              <StatusBadge tone="ok" label={t("overview.alarmsClear")} />
            ) : (
              <ul className={styles.list}>
                {alarms.map((alarm: AlarmOut, index: number) => (
                  <li key={`${alarm.entity_type}-${alarm.entity_id}-${index}`} className={styles.listRow}>
                    <StatusBadge
                      tone={alarm.severity === "critical" ? "danger" : "warning"}
                      label={t(`overview.alarmSeverity${alarm.severity === "critical" ? "Critical" : "Warning"}`)}
                    />
                    {alarm.customer_name && (
                      <span className={styles.listSecondary}>
                        {alarm.customer_name}
                        {alarm.customer_match_code ? ` (${alarm.customer_match_code})` : ""}
                      </span>
                    )}
                    <span className={styles.listPrimary}>{alarm.message}</span>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        )}

        <Panel title={t("overview.recentActivity")} className={`${styles.panel} ${styles.activityArea}`}>
          {recentTasksQuery.isSuccess && recentTasksQuery.data.length === 0 && (
            <EmptyState title={t("overview.noRecentActivity")} />
          )}
          {recentTasksQuery.isSuccess && recentTasksQuery.data.length > 0 && (
            <ul className={styles.list}>
              {recentTasksQuery.data.map((task: TaskRunOut) => {
                const { tone, label } = taskStatusPresentation(task.status, t);
                return (
                  <li key={task.id} className={styles.listRow}>
                    <StatusBadge tone={tone} label={label} />
                    <span className={styles.listPrimary}>{task.task_type}</span>
                    <span className={styles.listSecondary}>{instanceName(task.pbs_instance_id)}</span>
                    <span className={styles.listTimestamp}>{new Date(task.started_at).toLocaleString()}</span>
                  </li>
                );
              })}
            </ul>
          )}
        </Panel>

        <Panel
          title={t("overview.overdueBackups")}
          className={`${styles.panel} ${styles.overdueArea}`}
          actions={
            overdueGroups.length > 0 && (
              <button type="button" className="button" onClick={() => setOverdueWindowOpen(true)}>
                {t("overview.overdueOpenWindow", { count: overdueGroups.length })}
              </button>
            )
          }
        >
          {!groupsLoading && overdueGroups.length === 0 && <EmptyState title={t("overview.overdueBackupsClear")} />}
          {overdueGroups.length > 0 && (
            <ul className={styles.list}>
              {overdueGroups.slice(0, OVERDUE_LIST_LIMIT).map(({ group, staleness }) => (
                <li key={group.id} className={styles.listRow}>
                  <StatusBadge
                    tone={staleness}
                    label={t(staleness === "danger" ? "overview.overdueDanger" : "overview.overdueWarning")}
                  />
                  <span className={styles.listPrimary}>{group.resolved_name ?? group.backup_id}</span>
                  <span className={styles.listSecondary}>{group.backup_type.toUpperCase()}</span>
                </li>
              ))}
              {overdueGroups.length > OVERDUE_LIST_LIMIT && (
                <li className={styles.listMore}>
                  <button type="button" className={styles.listMoreButton} onClick={() => setOverdueWindowOpen(true)}>
                    {t("overview.overdueMore", { count: overdueGroups.length - OVERDUE_LIST_LIMIT })}
                  </button>
                </li>
              )}
            </ul>
          )}
        </Panel>

        <Panel title={t("overview.datastoreUsage")} className={`${styles.panel} ${styles.usageArea}`}>
          {datastoreUsage.length === 0 && !datastoresQuery.isLoading && <EmptyState title={t("overview.noDatastores")} />}
          {datastoreUsage.length > 0 && (
            <ul className={styles.usageList}>
              {datastoreUsage.map(({ datastore, usage }) =>
                datastore ? (
                  <li key={datastore.id} className={styles.usageRow}>
                    <div className={styles.usageHeader}>
                      <span className={styles.listPrimary}>{datastore.name}</span>
                      <span className={styles.listSecondary}>
                        {usage?.latest
                          ? `${formatBytes(usage.latest.used_bytes)} / ${formatBytes(usage.latest.total_bytes)} (${Math.round(usage.latest.usage_percent)}%)`
                          : t("overview.noUsageData")}
                      </span>
                    </div>
                    {usage?.latest && (
                      <div className={styles.usageBarTrack}>
                        <div
                          className={`${styles.usageBarFill} ${usage.latest.usage_percent >= 90 ? styles.usageBarDanger : usage.latest.usage_percent >= 75 ? styles.usageBarWarning : ""}`}
                          style={{ width: `${Math.min(100, usage.latest.usage_percent)}%` }}
                        />
                      </div>
                    )}
                  </li>
                ) : null,
              )}
            </ul>
          )}
        </Panel>
      </div>

      {overdueWindowOpen && (
        <OverdueBackupsWindow
          open
          onClose={() => setOverdueWindowOpen(false)}
          entries={overdueGroups}
          datastoreName={datastoreName}
          customerName={customerName}
        />
      )}
    </div>
  );
}
