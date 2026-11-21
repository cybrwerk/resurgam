import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { BackupGroupOut } from "../api/types";
import { hoursSinceLastBackup } from "../lib/backupStaleness";
import { DataTable, type DataTableColumn } from "./DataTable";
import { EmptyState } from "./EmptyState";
import { Modal } from "./Modal";
import { StatusBadge } from "./StatusBadge";
import styles from "./OverdueBackupsWindow.module.css";

export interface OverdueEntry {
  group: BackupGroupOut;
  staleness: "danger" | "warning";
}

interface OverdueBackupsWindowProps {
  open: boolean;
  onClose: () => void;
  entries: OverdueEntry[];
  datastoreName: (id: string) => string;
  customerName: (id: string | null) => string | null;
}

type StatusFilter = "all" | "danger" | "warning";

const BACKUPS_PAGE: Record<string, string> = { vm: "/backups/vms", ct: "/backups/containers", host: "/backups/hosts" };

/** The Dashboard's Overdue Backups panel shows only the first few; this
 * window lists every overdue workload with its tenant, datastore and
 * age, filterable and sortable -- the full picture without leaving the
 * Dashboard. Same "overdue" rule as the panel (`backupStaleness`). */
export function OverdueBackupsWindow({ open, onClose, entries, datastoreName, customerName }: OverdueBackupsWindowProps) {
  const { t } = useTranslation();
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<StatusFilter>("all");

  const ageLabel = (lastBackupAt: string | null) => {
    const hours = hoursSinceLastBackup(lastBackupAt);
    if (hours === null) return t("overview.overdueNever");
    if (hours < 48) return t("overview.overdueAgeHours", { count: Math.floor(hours) });
    return t("overview.overdueAgeDays", { count: Math.floor(hours / 24) });
  };

  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return entries.filter(({ group, staleness }) => {
      if (status !== "all" && staleness !== status) return false;
      if (!needle) return true;
      return [
        group.resolved_name,
        group.backup_id,
        group.backup_type,
        group.namespace,
        datastoreName(group.datastore_id),
        customerName(group.effective_customer_id),
      ].some((value) => value?.toLowerCase().includes(needle));
    });
  }, [entries, search, status, datastoreName, customerName]);

  const dangerCount = entries.filter((e) => e.staleness === "danger").length;
  const warningCount = entries.length - dangerCount;

  const columns: DataTableColumn<OverdueEntry>[] = [
    {
      key: "status",
      header: t("common.status"),
      render: ({ staleness }) => (
        <StatusBadge
          tone={staleness}
          label={t(staleness === "danger" ? "overview.overdueDanger" : "overview.overdueWarning")}
        />
      ),
      sortValue: ({ staleness }) => (staleness === "danger" ? 0 : 1),
    },
    {
      key: "workload",
      header: t("overview.overdueWorkload"),
      render: ({ group }) => (
        <span>
          {group.resolved_name ?? group.backup_id}
          {group.resolved_name && <span className="mutedText"> ({group.backup_id})</span>}
        </span>
      ),
      sortValue: ({ group }) => (group.resolved_name ?? group.backup_id).toLowerCase(),
    },
    {
      key: "type",
      header: t("overview.overdueType"),
      render: ({ group }) => group.backup_type.toUpperCase(),
      sortValue: ({ group }) => group.backup_type,
    },
    {
      key: "customer",
      header: t("overview.overdueCustomer"),
      render: ({ group }) => customerName(group.effective_customer_id) ?? "—",
      sortValue: ({ group }) => (customerName(group.effective_customer_id) ?? "").toLowerCase(),
    },
    {
      key: "datastore",
      header: t("overview.overdueDatastore"),
      render: ({ group }) => (
        <span>
          {datastoreName(group.datastore_id)}
          {group.namespace && <span className="mutedText"> / {group.namespace}</span>}
        </span>
      ),
      sortValue: ({ group }) => `${datastoreName(group.datastore_id)}/${group.namespace}`.toLowerCase(),
    },
    {
      key: "lastBackup",
      header: t("overview.overdueLastBackup"),
      render: ({ group }) => (group.last_backup_at ? new Date(group.last_backup_at).toLocaleString() : "—"),
      // Never backed up sorts as the oldest.
      sortValue: ({ group }) => (group.last_backup_at ? new Date(group.last_backup_at).getTime() : 0),
    },
    {
      key: "age",
      header: t("overview.overdueAge"),
      render: ({ group }) => ageLabel(group.last_backup_at),
      sortValue: ({ group }) => hoursSinceLastBackup(group.last_backup_at) ?? Number.MAX_SAFE_INTEGER,
    },
    {
      key: "actions",
      header: "",
      render: ({ group }) =>
        BACKUPS_PAGE[group.backup_type] ? (
          <Link to={BACKUPS_PAGE[group.backup_type]} onClick={onClose}>
            {t("overview.overdueOpenList")}
          </Link>
        ) : null,
    },
  ];

  return (
    <Modal open={open} size="xlarge" title={t("overview.overdueWindowTitle", { count: entries.length })} onClose={onClose}>
      <div className={styles.toolbar}>
        <input
          type="search"
          className={`input ${styles.search}`}
          placeholder={t("overview.overdueSearch")}
          aria-label={t("overview.overdueSearch")}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select
          className="select"
          aria-label={t("common.status")}
          value={status}
          onChange={(e) => setStatus(e.target.value as StatusFilter)}
        >
          <option value="all">{t("overview.overdueFilterAll", { count: entries.length })}</option>
          <option value="danger">{t("overview.overdueFilterDanger", { count: dangerCount })}</option>
          <option value="warning">{t("overview.overdueFilterWarning", { count: warningCount })}</option>
        </select>
      </div>
      <p className={`mutedText ${styles.hint}`}>{t("overview.overdueRuleHint")}</p>
      {rows.length === 0 ? (
        <EmptyState title={t("overview.overdueNoMatch")} />
      ) : (
        <DataTable columns={columns} rows={rows} rowKey={({ group }) => group.id} />
      )}
    </Modal>
  );
}
