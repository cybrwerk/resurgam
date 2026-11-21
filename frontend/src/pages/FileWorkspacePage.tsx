import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { customersApi, fileIndexApi, fileIndexSettingsApi, inventoryApi } from "../api/resources";
import { Combobox } from "../components/Combobox";
import { EmptyState } from "../components/EmptyState";
import { FileBrowserWindow } from "../components/FileBrowserWindow";
import { FileIndexJobsSection } from "../components/FileIndexJobsSection";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { RestorePointCalendar } from "../components/RestorePointCalendar";
import { useSelectedCustomer } from "../contexts/CustomerContext";
import { useBackupGroups } from "../hooks/useBackupGroups";
import styles from "./FileWorkspacePage.module.css";

/** Manifest §13 File Indexing/Recovery, unified: pick a customer, a
 * backup group, and a restore point on its calendar (green = already
 * indexed, instant browse/search; yellow = live on-demand browsing) --
 * raised directly by the user in place of the two separate,
 * largely-duplicate pages this replaces (FileIndexPage,
 * FileRecoveryPage). The actual browsing (disk/partition/folder tree,
 * content, group-wide search) opens in its own `FileBrowserWindow`
 * rather than living inline here -- also raised directly by the user,
 * after this page felt overloaded with the calendar, the tree/browser,
 * and the indexing job history all stacked at once. This page stays
 * the lighter-weight "pick a group, track its indexing" surface.
 */
export function FileWorkspacePage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { selectedCustomerId } = useSelectedCustomer();
  const [customerId, setCustomerId] = useState(selectedCustomerId ?? "");
  const [groupId, setGroupId] = useState("");
  const [snapshotId, setSnapshotId] = useState("");
  const [path, setPath] = useState("/");
  // A not-yet-indexed snapshot needs a real, potentially slow live mount
  // (proxmox-file-restore) before anything can browse it -- raised
  // directly by the user: that must be an explicit "Run" action, not
  // something that fires the instant a yellow day is clicked, so the
  // operator knows to expect a wait. Reset whenever the snapshot changes;
  // irrelevant (never gates anything) once the snapshot is indexed.
  const [mountConfirmed, setMountConfirmed] = useState(false);
  const [browserOpen, setBrowserOpen] = useState(false);

  // Follows the sidebar's tenant switcher -- raised directly by the user.
  useEffect(() => {
    setCustomerId(selectedCustomerId ?? "");
    setGroupId("");
    setSnapshotId("");
    setPath("/");
  }, [selectedCustomerId]);

  const customersQuery = useQuery({ queryKey: ["customers"], queryFn: customersApi.list });
  const { groups, groupsLoading } = useBackupGroups(customerId);
  const selectedGroup = groups.find((g) => g.id === groupId);

  const groupLabel = (group: (typeof groups)[number]) => {
    const kind = group.backup_type.toUpperCase();
    const name = group.resolved_name ? `${group.resolved_name} (${group.backup_id})` : group.backup_id;
    return group.namespace ? `${kind} ${name} — ${group.namespace}` : `${kind} ${name}`;
  };

  const handleCustomerChange = (id: string) => {
    setCustomerId(id);
    setGroupId("");
    setSnapshotId("");
    setPath("/");
    setMountConfirmed(false);
  };

  const handleGroupChange = (id: string) => {
    setGroupId(id);
    setSnapshotId("");
    setPath("/");
    setMountConfirmed(false);
  };

  const handleSelectSnapshot = (id: string) => {
    setSnapshotId(id);
    setPath("/");
    setMountConfirmed(false);
    setBrowserOpen(true);
  };

  const handleSearchResultSelect = (resultSnapshotId: string, resultPath: string) => {
    // Search results only ever come from indexed (green) snapshots --
    // fileIndexApi.search only ever matches FileIndexEntry rows -- so
    // there's never a mount to confirm here, but this stays consistent
    // in case that ever changes. The window is already open (this only
    // fires from inside it), so no need to open it again here.
    setSnapshotId(resultSnapshotId);
    setPath(resultPath);
    setMountConfirmed(false);
  };

  // A 404 means "never configured" -- treated as disabled, not an error.
  const fileIndexSettingsQuery = useQuery({
    queryKey: ["file-index-settings", customerId],
    queryFn: () => fileIndexSettingsApi.get(customerId),
    enabled: customerId !== "",
    retry: false,
  });
  const indexingEnabled = fileIndexSettingsQuery.isSuccess && fileIndexSettingsQuery.data.enabled;
  const groupIndexingEnabled = indexingEnabled && (selectedGroup?.file_index_enabled ?? false);

  const toggleGroupFileIndexMutation = useMutation({
    mutationFn: () =>
      selectedGroup?.file_index_enabled ? inventoryApi.disableFileIndex(groupId) : inventoryApi.enableFileIndex(groupId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["backup-groups"] }),
  });
  const indexAllMutation = useMutation({
    mutationFn: () => fileIndexApi.createBulkJob(groupId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["file-index-jobs", groupId] }),
  });

  const snapshotsQuery = useQuery({
    queryKey: ["snapshots", groupId],
    queryFn: () => inventoryApi.snapshots(groupId),
    enabled: groupId !== "",
  });
  const snapshots = snapshotsQuery.data ?? [];

  const statusQuery = useQuery({
    queryKey: ["file-index-snapshot-status", groupId],
    queryFn: () => fileIndexApi.snapshotStatus(groupId),
    enabled: groupId !== "",
  });
  const indexedIds = new Set(statusQuery.data?.indexed_snapshot_ids ?? []);
  const isIndexed = indexedIds.has(snapshotId);

  return (
    <div>
      <PageHeader title={t("fileWorkspace.title")} description={t("fileWorkspace.description")} />

      <div className="toolbar">
        <div className={`field ${styles.toolbarField}`}>
          <label className="label" htmlFor="file-workspace-customer">
            {t("fileRecovery.customer")}
          </label>
          <select
            id="file-workspace-customer"
            className="select"
            value={customerId}
            onChange={(e) => handleCustomerChange(e.target.value)}
          >
            <option value="">{t("fileRecovery.selectCustomer")}</option>
            {customersQuery.data?.map((customer) => (
              <option key={customer.id} value={customer.id}>
                {customer.name}
              </option>
            ))}
          </select>
        </div>

        {customerId !== "" && (
          <div className={`field ${styles.toolbarField}`}>
            <label className="label" htmlFor="file-workspace-group">
              {t("fileRecovery.backupGroup")}
            </label>
            <Combobox
              id="file-workspace-group"
              options={groups.map((group) => ({ value: group.id, label: groupLabel(group) }))}
              value={groupId}
              onChange={handleGroupChange}
              placeholder={t("fileRecovery.selectGroup")}
              disabled={groupsLoading}
              loadingLabel={t("common.loading")}
            />
          </div>
        )}

        {groupId !== "" && indexingEnabled && (
          <>
            <div className="toolbarSpacer" />
            <button
              type="button"
              className="button"
              onClick={() => toggleGroupFileIndexMutation.mutate()}
              disabled={toggleGroupFileIndexMutation.isPending}
            >
              {selectedGroup?.file_index_enabled ? t("fileRecovery.disableFileIndexForVm") : t("fileRecovery.enableFileIndexForVm")}
            </button>
            {groupIndexingEnabled && (
              <button
                type="button"
                className="button"
                onClick={() => indexAllMutation.mutate()}
                disabled={indexAllMutation.isPending}
              >
                {t("fileRecovery.indexAllSnapshots")}
              </button>
            )}
          </>
        )}
      </div>

      {customerId === "" && (
        <div className="panel">
          <EmptyState title={t("fileRecovery.pickCustomerFirst")} />
        </div>
      )}

      {customerId !== "" && !groupsLoading && groups.length === 0 && (
        <div className="panel">
          <EmptyState title={t("fileRecovery.noGroups")} />
        </div>
      )}

      {customerId !== "" && groupId === "" && !groupsLoading && groups.length > 0 && (
        <div className="panel">
          <EmptyState title={t("fileWorkspace.pickGroupFirst")} />
        </div>
      )}

      {groupId !== "" && (
        <div className="stack">
          <Panel title={selectedGroup ? groupLabel(selectedGroup) : undefined}>
            <RestorePointCalendar
              snapshots={snapshots}
              selectedSnapshotId={snapshotId}
              onSelect={handleSelectSnapshot}
              statusForSnapshot={(s) => (indexedIds.has(s.id) ? "indexed" : "indexable")}
            />
            {snapshotId === "" && <EmptyState title={t("fileIndex.pickSnapshotFirst")} />}
          </Panel>

          <FileIndexJobsSection groupId={groupId} />

          {snapshotId !== "" && selectedGroup && !statusQuery.isLoading && (
            <FileBrowserWindow
              open={browserOpen}
              onClose={() => setBrowserOpen(false)}
              title={groupLabel(selectedGroup)}
              customerId={customerId}
              groupId={groupId}
              snapshotId={snapshotId}
              path={path}
              onPathChange={setPath}
              backupType={selectedGroup.backup_type as "vm" | "ct" | "host"}
              isIndexed={isIndexed}
              indexingEnabled={groupIndexingEnabled}
              snapshots={snapshots}
              onResultSelect={handleSearchResultSelect}
              mountConfirmed={mountConfirmed}
              onConfirmMount={() => setMountConfirmed(true)}
            />
          )}
        </div>
      )}
    </div>
  );
}
