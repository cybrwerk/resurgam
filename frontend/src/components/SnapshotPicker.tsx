import { useState } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { customersApi, datastoresApi, inventoryApi } from "../api/resources";
import type { BackupGroupOut } from "../api/types";
import { Combobox } from "./Combobox";
import { EmptyState } from "./EmptyState";
import { RestorePointCalendar } from "./RestorePointCalendar";
import { RestorePointList } from "./RestorePointList";
import styles from "./SnapshotPicker.module.css";

type RestorePointView = "list" | "calendar";

interface SnapshotPickerProps {
  customerId: string;
  onCustomerChange: (id: string) => void;
  groupId: string;
  onGroupChange: (id: string) => void;
  snapshotId: string;
  onSnapshotChange: (id: string) => void;
  /** Restrict the group picker to groups matching this predicate — e.g.
   * Guided Restore only supports vm/ct, not host. */
  groupFilter?: (group: BackupGroupOut) => boolean;
}

/** Customer -> backup group -> snapshot, the same cascading picker used
 * (as inline, not-yet-shared logic) by `FileRecoveryPage`,
 * `ProtectedWorkloadsPage`, and `DatastoresPage`'s own group listings.
 * Extracted here for its fourth use (the Guided Restore wizard) rather
 * than copy-pasted again — the three existing pages still have their own
 * inline copies, left as-is to avoid touching three already-shipped,
 * already-tested pages for this feature; a follow-up could migrate them
 * to this component.
 *
 * Groups aren't served by a single "backup groups for this customer"
 * endpoint — none exists yet — so this fetches every datastore's groups
 * once a customer is picked (same `useQueries` pattern used everywhere
 * else) and filters client-side by `effective_customer_id`. Fine at the
 * real data scale seen so far; would want a real endpoint if that grows.
 *
 * The restore-point step itself (once a group is picked) offers a List
 * view (a sortable table — a superset of the plain type-to-filter
 * dropdown it replaces) and a Calendar view (a month grid), toggled with
 * a small segmented control — raised directly by the user as a richer
 * way to browse a group's restore points than a flat dropdown.
 */
/** Picker value for backups with no tenant at all (neither the group nor its
 * datastore assigned to one): without it they could not be selected in the
 * wizard. Never sent to the API. */
export const UNASSIGNED_CUSTOMER = "__unassigned__";

export function SnapshotPicker({
  customerId,
  onCustomerChange,
  groupId,
  onGroupChange,
  snapshotId,
  onSnapshotChange,
  groupFilter,
}: SnapshotPickerProps) {
  const { t } = useTranslation();
  const [view, setView] = useState<RestorePointView>("list");

  const customersQuery = useQuery({ queryKey: ["customers"], queryFn: customersApi.list });
  const datastoresQuery = useQuery({ queryKey: ["datastores"], queryFn: datastoresApi.list });

  const datastoreIds = datastoresQuery.data?.map((d) => d.id) ?? [];
  const groupsQueries = useQueries({
    queries:
      customerId === ""
        ? []
        : datastoreIds.map((id) => ({
            queryKey: ["backup-groups", id],
            queryFn: () => datastoresApi.backupGroups(id),
          })),
  });
  const groupsLoading = customerId !== "" && groupsQueries.some((q) => q.isLoading);
  const groups = (customerId === "" ? [] : groupsQueries.flatMap((q) => q.data ?? []))
    .filter((group) =>
      customerId === UNASSIGNED_CUSTOMER ? !group.effective_customer_id : group.effective_customer_id === customerId,
    )
    .filter((group) => (groupFilter ? groupFilter(group) : true));

  const snapshotsQuery = useQuery({
    queryKey: ["snapshots", groupId],
    queryFn: () => inventoryApi.snapshots(groupId),
    enabled: groupId !== "",
  });

  const handleCustomerChange = (id: string) => {
    onCustomerChange(id);
    onGroupChange("");
    onSnapshotChange("");
  };

  const handleGroupChange = (id: string) => {
    onGroupChange(id);
    onSnapshotChange("");
  };

  const groupLabel = (group: (typeof groups)[number]) => {
    const kind = group.backup_type.toUpperCase();
    const name = group.resolved_name ? `${group.resolved_name} (${group.backup_id})` : group.backup_id;
    return group.namespace ? `${kind} ${name} — ${group.namespace}` : `${kind} ${name}`;
  };

  return (
    <div className={styles.pickers}>
      <div className="field">
        <label className="label" htmlFor="snapshot-picker-customer">
          {t("snapshotPicker.customer")}
        </label>
        <select
          id="snapshot-picker-customer"
          className="select"
          value={customerId}
          onChange={(e) => handleCustomerChange(e.target.value)}
        >
          <option value="">{t("snapshotPicker.selectCustomer")}</option>
          <option value={UNASSIGNED_CUSTOMER}>{t("snapshotPicker.unassigned")}</option>
          {customersQuery.data?.map((customer) => (
            <option key={customer.id} value={customer.id}>
              {customer.name}
            </option>
          ))}
        </select>
      </div>

      {customerId !== "" && (
        <div className="field">
          <label className="label" htmlFor="snapshot-picker-group">
            {t("snapshotPicker.backupGroup")}
          </label>
          <Combobox
            id="snapshot-picker-group"
            options={groups.map((group) => ({ value: group.id, label: groupLabel(group) }))}
            value={groupId}
            onChange={handleGroupChange}
            placeholder={t("snapshotPicker.selectGroup")}
            disabled={groupsLoading}
            loadingLabel={t("common.loading")}
          />
          {!groupsLoading && groups.length === 0 && (
            <p className={`mutedText ${styles.emptyHint}`}>{t("snapshotPicker.noGroups")}</p>
          )}
        </div>
      )}

      {groupId !== "" && (
        <div className={`field ${styles.snapshotField}`}>
          <div className={styles.snapshotHeader}>
            <span className="label">{t("snapshotPicker.snapshot")}</span>
            <div className={styles.viewToggle} role="group" aria-label={t("snapshotPicker.viewMode")}>
              <button
                type="button"
                className={`button buttonGhost ${view === "list" ? styles.viewToggleActive : ""}`}
                onClick={() => setView("list")}
                aria-pressed={view === "list"}
              >
                {t("snapshotPicker.viewList")}
              </button>
              <button
                type="button"
                className={`button buttonGhost ${view === "calendar" ? styles.viewToggleActive : ""}`}
                onClick={() => setView("calendar")}
                aria-pressed={view === "calendar"}
              >
                {t("snapshotPicker.viewCalendar")}
              </button>
            </div>
          </div>

          {snapshotsQuery.isLoading && <p className="mutedText">{t("common.loading")}</p>}
          {snapshotsQuery.isError && <p className="mutedText">{t("common.loadError")}</p>}
          {snapshotsQuery.data && snapshotsQuery.data.length === 0 && (
            <EmptyState title={t("snapshotPicker.noSnapshots")} />
          )}
          {snapshotsQuery.data && snapshotsQuery.data.length > 0 && view === "list" && (
            <RestorePointList snapshots={snapshotsQuery.data} selectedSnapshotId={snapshotId} onSelect={onSnapshotChange} />
          )}
          {snapshotsQuery.data && snapshotsQuery.data.length > 0 && view === "calendar" && (
            <RestorePointCalendar snapshots={snapshotsQuery.data} selectedSnapshotId={snapshotId} onSelect={onSnapshotChange} />
          )}
        </div>
      )}
    </div>
  );
}
