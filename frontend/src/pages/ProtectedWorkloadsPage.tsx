import { useEffect, useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { customersApi, datastoresApi, inventoryApi, pbsInstancesApi } from "../api/resources";
import type { BackupGroupOut } from "../api/types";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import { FileIndexExcludePatternsModal } from "../components/FileIndexExcludePatternsModal";
import { InlineAlert } from "../components/InlineAlert";
import { PageHeader } from "../components/PageHeader";
import { PriorGenerationsModal } from "../components/PriorGenerationsModal";
import { RestorePointsModal } from "../components/RestorePointsModal";
import { StatusBadge } from "../components/StatusBadge";
import { ApiError } from "../api/client";
import { useSelectedCustomer } from "../contexts/CustomerContext";
import { backupStaleness } from "../lib/backupStaleness";
import { protectionStatusPresentation, verifyStatusPresentation } from "../lib/statusPresentation";
import styles from "./ProtectedWorkloadsPage.module.css";

interface ProtectedWorkloadsPageProps {
  titleKey: string;
  /** Restricts the view to this backup type — the Backups nav's
   * Virtuelle Maschinen/Container/Hosts entries share this one
   * component and one underlying inventory (manifest §9.4's "aus PVE
   * aufgelöster Name"/"Schutzstatus" etc. are all already on
   * `BackupGroupOut`), each filtering client-side, same pattern
   * `TasksPage` already uses for the four Jobs entries. */
  backupType: "vm" | "ct" | "host";
}

/** No "backup groups of type X across every datastore" endpoint exists
 * yet, so this is built the same way `FileRecoveryPage`'s group picker
 * is: fetch every datastore's groups (once, unconditionally — this is
 * meant as a full inventory view, and RBAC scope-filtering on
 * `/datastores` and `/backup-groups` already limits what a non-admin
 * sees) and filter client-side. Fine at the real data scale seen so far
 * (a handful of datastores, ~135 groups total); would want a real
 * endpoint if that grows. */
export function ProtectedWorkloadsPage({ titleKey, backupType }: ProtectedWorkloadsPageProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { selectedCustomerId } = useSelectedCustomer();
  const [customerId, setCustomerId] = useState(selectedCustomerId ?? "");
  const [search, setSearch] = useState("");

  // Follows the sidebar's tenant switcher -- raised directly by the
  // user: having to re-filter by customer here even though it's already
  // scoped globally is redundant.
  useEffect(() => {
    setCustomerId(selectedCustomerId ?? "");
  }, [selectedCustomerId]);
  const [selectedGroup, setSelectedGroup] = useState<BackupGroupOut | null>(null);
  const [excludePatternsGroup, setExcludePatternsGroup] = useState<BackupGroupOut | null>(null);
  const [historyGroup, setHistoryGroup] = useState<BackupGroupOut | null>(null);

  const customersQuery = useQuery({ queryKey: ["customers"], queryFn: customersApi.list });
  const instancesQuery = useQuery({ queryKey: ["pbs-instances"], queryFn: pbsInstancesApi.list });
  const datastoresQuery = useQuery({ queryKey: ["datastores"], queryFn: datastoresApi.list });

  const datastoreIds = datastoresQuery.data?.map((d) => d.id) ?? [];
  const groupsQueries = useQueries({
    queries: datastoreIds.map((id) => ({
      queryKey: ["backup-groups", id],
      queryFn: () => datastoresApi.backupGroups(id),
    })),
  });

  const isLoading = customersQuery.isLoading || instancesQuery.isLoading || datastoresQuery.isLoading;
  const hasError = customersQuery.isError || instancesQuery.isError || datastoresQuery.isError;
  const firstError = customersQuery.error ?? instancesQuery.error ?? datastoresQuery.error;

  const datastoreById = new Map((datastoresQuery.data ?? []).map((d) => [d.id, d]));
  const instanceById = new Map((instancesQuery.data ?? []).map((i) => [i.id, i]));
  const customerById = new Map((customersQuery.data ?? []).map((c) => [c.id, c]));

  const groups = groupsQueries
    .flatMap((q) => q.data ?? [])
    .filter((group) => group.backup_type === backupType)
    .filter((group) => customerId === "" || group.effective_customer_id === customerId);

  const groupsStillLoading = datastoreIds.length > 0 && groupsQueries.some((q) => q.isLoading);

  // Per-VM File Indexing opt-in (manifest §13, ADR 0016) -- surfaced
  // directly on the inventory itself, raised by the user as more
  // discoverable than only having it on the File Recovery/File Index
  // pickers. Toggling here never fails on "customer not enabled": the
  // per-group flag can be set independently of the customer-level master
  // switch (manifest §13's two-level gate) -- it just won't actually
  // index anything until the customer's own settings are enabled too.
  const toggleFileIndexMutation = useMutation({
    mutationFn: (group: BackupGroupOut) =>
      group.file_index_enabled ? inventoryApi.disableFileIndex(group.id) : inventoryApi.enableFileIndex(group.id),
    onSuccess: () => {
      for (const id of datastoreIds) queryClient.invalidateQueries({ queryKey: ["backup-groups", id] });
    },
  });

  const workloadLabel = (group: BackupGroupOut) =>
    group.resolved_name ? `${group.resolved_name} (${group.backup_id})` : group.backup_id;

  const customerLabel = (group: BackupGroupOut) =>
    group.effective_customer_id ? customerById.get(group.effective_customer_id)?.name ?? "—" : "—";

  const datastoreLabel = (group: BackupGroupOut) => {
    const datastore = datastoreById.get(group.datastore_id);
    if (!datastore) return "—";
    const instance = instanceById.get(datastore.pbs_instance_id);
    return instance ? `${instance.display_name} / ${datastore.name}` : datastore.name;
  };

  const searchTerm = search.trim().toLowerCase();
  const matchingGroups =
    searchTerm === ""
      ? groups
      : groups.filter((group) =>
          [workloadLabel(group), customerLabel(group), datastoreLabel(group), group.namespace]
            .join(" ")
            .toLowerCase()
            .includes(searchTerm),
        );
  // Default order: stalest (or never-backed-up) first, so what needs
  // attention is what an operator sees without first having to think to
  // sort for it (a real UX-audit finding, 2026-09-23) -- still fully
  // overridable by clicking any column header afterward.
  const visibleGroups = [...matchingGroups].sort(
    (a, b) => (a.last_backup_at ? new Date(a.last_backup_at).getTime() : -1) -
      (b.last_backup_at ? new Date(b.last_backup_at).getTime() : -1),
  );

  const columns: DataTableColumn<BackupGroupOut>[] = [
    {
      key: "customer",
      header: t("protectedWorkloads.customer"),
      render: (row) => <span className={styles.nowrap}>{customerLabel(row)}</span>,
      sortValue: customerLabel,
    },
    {
      key: "workload",
      header: t("protectedWorkloads.workload"),
      // Where it's stored ("PBS / datastore · namespace") is the secondary
      // line under the workload (2026-10-04): as two more columns, the
      // table didn't fit beside the sidebar at 1440px and scrolled sideways.
      render: (row) => {
        const location = `${datastoreLabel(row)} · ${row.namespace || "/"}`;
        return (
          <div className={styles.workloadCell}>
            <span className={styles.workloadName}>{workloadLabel(row)}</span>
            <span className={styles.workloadLocation} title={location}>
              <span>{datastoreLabel(row)}</span> · <span>{row.namespace || "/"}</span>
            </span>
          </div>
        );
      },
      sortValue: workloadLabel,
    },
    {
      key: "lastBackup",
      header: t("protectedWorkloads.lastBackup"),
      render: (row) => {
        const tone = backupStaleness(row.last_backup_at);
        const text = row.last_backup_at ? new Date(row.last_backup_at).toLocaleString() : "—";
        return <span className={tone ? `${styles.nowrap} ${styles[`stale-${tone}`]}` : styles.nowrap}>{text}</span>;
      },
      // Oldest (or never-backed-up) first -- the whole point of surfacing
      // staleness is that the workloads needing attention are the ones
      // an operator should see without having to think to sort for them.
      sortValue: (row) => (row.last_backup_at ? new Date(row.last_backup_at).getTime() : -1),
    },
    {
      key: "verifyStatus",
      header: t("snapshots.verifyStatus"),
      render: (row) => {
        const { tone, label } = verifyStatusPresentation(row.last_verify_status, t);
        return <StatusBadge tone={tone} label={label} />;
      },
      sortValue: (row) => row.last_verify_status,
    },
    {
      key: "protectionStatus",
      header: t("protectedWorkloads.protectionStatus"),
      render: (row) => {
        const { tone, label } = protectionStatusPresentation(row.protection_status, t);
        return <StatusBadge tone={tone} label={label} />;
      },
      sortValue: (row) => row.protection_status,
    },
    {
      key: "fileIndex",
      header: t("protectedWorkloads.fileIndexEnabled"),
      render: (row) => (
        <div className={styles.fileIndexCell} onClick={(e) => e.stopPropagation()}>
          <label
            className={styles.fileIndexToggle}
            title={row.file_index_enabled ? t("fileRecovery.disableFileIndexForVm") : t("fileRecovery.enableFileIndexForVm")}
          >
            <input
              type="checkbox"
              checked={row.file_index_enabled}
              onChange={() => toggleFileIndexMutation.mutate(row)}
              disabled={toggleFileIndexMutation.isPending}
              aria-label={
                row.file_index_enabled ? t("fileRecovery.disableFileIndexForVm") : t("fileRecovery.enableFileIndexForVm")
              }
            />
          </label>
        </div>
      ),
      sortValue: (row) => (row.file_index_enabled ? 1 : 0),
    },
    {
      // Keyed "actions" so the table pins it to the right edge on phones.
      key: "actions",
      header: t("common.actions"),
      render: (row) => (
        <div className={styles.fileIndexCell} onClick={(e) => e.stopPropagation()}>
          <button
            type="button"
            className={`button buttonGhost ${styles.compactButton} ${styles.excludePatternsButton}`}
            title={t("protectedWorkloads.editExcludePatterns")}
            aria-label={t("protectedWorkloads.editExcludePatterns")}
            onClick={() => setExcludePatternsGroup(row)}
          >
            {row.file_index_exclude_patterns.length > 0
              ? t("protectedWorkloads.excludeCount", { count: row.file_index_exclude_patterns.length })
              : t("protectedWorkloads.excludeNone")}
          </button>
          <button
            type="button"
            className={`button buttonGhost ${styles.compactButton}`}
            title={t("protectedWorkloads.viewPriorGenerations")}
            aria-label={t("protectedWorkloads.viewPriorGenerations")}
            onClick={() => setHistoryGroup(row)}
          >
            {t("protectedWorkloads.viewPriorGenerations")}
          </button>
        </div>
      ),
    },
  ];

  return (
    <div>
      <PageHeader title={t(titleKey)} />

      <div className="toolbar">
        <div className={`field ${styles.filter}`}>
          <label className="label" htmlFor="protected-workloads-customer">
            {t("protectedWorkloads.filterByCustomer")}
          </label>
          <select
            id="protected-workloads-customer"
            className="select"
            value={customerId}
            onChange={(e) => setCustomerId(e.target.value)}
          >
            <option value="">{t("protectedWorkloads.allCustomers")}</option>
            {customersQuery.data?.map((customer) => (
              <option key={customer.id} value={customer.id}>
                {customer.name}
              </option>
            ))}
          </select>
        </div>

        <div className={`field ${styles.filter}`}>
          <label className="label" htmlFor="protected-workloads-search">
            {t("protectedWorkloads.search")}
          </label>
          <input
            id="protected-workloads-search"
            type="text"
            className="input"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("protectedWorkloads.searchPlaceholder")}
          />
        </div>
      </div>

      {isLoading && <p className="mutedText">{t("common.loading")}</p>}

      {hasError &&
        (firstError instanceof ApiError && firstError.status === 403 ? (
          <EmptyState title={t("common.forbidden")} description={t("common.forbiddenDescription")} />
        ) : (
          <InlineAlert tone="danger">{t("common.loadError")}</InlineAlert>
        ))}

      {!isLoading && !hasError && groupsStillLoading && <p className="mutedText">{t("common.loading")}</p>}

      {!isLoading && !hasError && !groupsStillLoading && groups.length === 0 && (
        <EmptyState title={t("common.empty")} />
      )}

      {!isLoading && !hasError && !groupsStillLoading && groups.length > 0 && visibleGroups.length === 0 && (
        <EmptyState title={t("protectedWorkloads.noSearchMatches")} />
      )}

      {!isLoading && !hasError && !groupsStillLoading && visibleGroups.length > 0 && (
        <DataTable
          columns={columns}
          rows={visibleGroups}
          rowKey={(row) => row.id}
          onRowClick={(row) => setSelectedGroup(row)}
        />
      )}

      <RestorePointsModal
        open={selectedGroup !== null}
        onClose={() => setSelectedGroup(null)}
        groupId={selectedGroup?.id ?? ""}
        customerId={selectedGroup?.effective_customer_id ?? ""}
        backupType={(selectedGroup?.backup_type as "vm" | "ct" | "host") ?? "vm"}
        groupFileIndexEnabled={selectedGroup?.file_index_enabled ?? false}
        workloadLabel={selectedGroup ? workloadLabel(selectedGroup) : ""}
      />

      {excludePatternsGroup && (
        <FileIndexExcludePatternsModal
          group={excludePatternsGroup}
          onClose={() => setExcludePatternsGroup(null)}
          onSaved={() => {
            for (const id of datastoreIds) queryClient.invalidateQueries({ queryKey: ["backup-groups", id] });
          }}
        />
      )}

      {historyGroup && (
        <PriorGenerationsModal
          datastoreId={historyGroup.datastore_id}
          currentGroup={historyGroup}
          onClose={() => setHistoryGroup(null)}
        />
      )}
    </div>
  );
}
