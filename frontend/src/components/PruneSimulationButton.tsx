import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { datastoresApi, inventoryApi } from "../api/resources";
import type { BackupGroupOut } from "../api/types";
import { simulatePrune } from "../lib/pruneSimulation";
import { EmptyState } from "./EmptyState";
import { Modal } from "./Modal";
import { PruneSimulationTimeline } from "./PruneSimulationTimeline";
import styles from "./PruneSimulationButton.module.css";

function toKeepInt(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === "") return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

function workloadLabel(group: BackupGroupOut): string {
  const kind = group.backup_type.toUpperCase();
  const name = group.resolved_name ? `${group.resolved_name} (${group.backup_id})` : group.backup_id;
  return group.namespace ? `${kind} ${name} — ${group.namespace}` : `${kind} ${name}`;
}

interface PruneSimulationButtonProps {
  /** This app's local `Datastore.id` the simulation should pick a
   * backup group from -- `null` while it hasn't been resolved yet
   * (e.g. the vzdump job's target PVE storage isn't matched to an
   * imported RESURGAM datastore), which disables the button rather
   * than offering a simulation with no real data behind it. */
  datastoreId: string | null;
  /** Restricts the group picker to this namespace when known (matches
   * a specific PBS Prune Job's `ns` scope); omitted shows every group
   * on the datastore. */
  namespace?: string | null;
  keepLast: string;
  keepHourly: string;
  keepDaily: string;
  keepWeekly: string;
  keepMonthly: string;
  keepYearly: string;
}

/** Opens a modal to preview which of a real backup group's snapshots
 * the *currently entered* (not yet necessarily saved) retention policy
 * would keep vs. prune -- raised directly by the user, modeled on
 * Synology Active Backup's retention preview and Proxmox's own Prune
 * Simulator. Shared by both real retention surfaces this app has
 * (`PruneJobFormModal` and the vzdump job's own optional retention),
 * since both carry the identical keep-last/hourly/daily/weekly/monthly/
 * yearly fields.
 */
export function PruneSimulationButton({
  datastoreId,
  namespace,
  keepLast,
  keepHourly,
  keepDaily,
  keepWeekly,
  keepMonthly,
  keepYearly,
}: PruneSimulationButtonProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [groupId, setGroupId] = useState("");

  const groupsQuery = useQuery({
    queryKey: ["backup-groups", datastoreId],
    queryFn: () => datastoresApi.backupGroups(datastoreId!),
    enabled: open && datastoreId !== null,
  });
  const groups = (groupsQuery.data ?? []).filter((g) => namespace == null || g.namespace === namespace);

  const snapshotsQuery = useQuery({
    queryKey: ["snapshots", groupId],
    queryFn: () => inventoryApi.snapshots(groupId),
    enabled: open && groupId !== "",
  });

  const keepOptions = useMemo(
    () => ({
      keepLast: toKeepInt(keepLast),
      keepHourly: toKeepInt(keepHourly),
      keepDaily: toKeepInt(keepDaily),
      keepWeekly: toKeepInt(keepWeekly),
      keepMonthly: toKeepInt(keepMonthly),
      keepYearly: toKeepInt(keepYearly),
    }),
    [keepLast, keepHourly, keepDaily, keepWeekly, keepMonthly, keepYearly],
  );

  const results = useMemo(() => {
    if (!snapshotsQuery.data) return null;
    return simulatePrune(snapshotsQuery.data.map((s) => new Date(s.snapshot_time)), keepOptions);
  }, [snapshotsQuery.data, keepOptions]);

  return (
    <>
      <button
        type="button"
        className="button"
        onClick={() => setOpen(true)}
        disabled={datastoreId === null}
        title={datastoreId === null ? t("pbsJobs.pruneSimUnresolved") : undefined}
      >
        {t("pbsJobs.pruneSimulate")}
      </button>
      {open && (
        <Modal open title={t("pbsJobs.pruneSimulateTitle")} onClose={() => setOpen(false)} size="large">
          <div className="field">
            <label className="label" htmlFor="prune-sim-group">
              {t("pbsJobs.pruneSimGroup")}
            </label>
            <select id="prune-sim-group" className="select" value={groupId} onChange={(e) => setGroupId(e.target.value)}>
              <option value="">{t("pbsJobs.pruneSimPickGroupPlaceholder")}</option>
              {groups.map((group) => (
                <option key={group.id} value={group.id}>
                  {workloadLabel(group)}
                </option>
              ))}
            </select>
            {groupsQuery.isSuccess && groups.length === 0 && (
              <p className="mutedText">{t("pbsJobs.pruneSimNoGroups")}</p>
            )}
          </div>

          <div className={styles.body}>
            {groupId === "" && <EmptyState title={t("pbsJobs.pruneSimPickGroupFirst")} />}
            {groupId !== "" && snapshotsQuery.isLoading && <p className="mutedText">{t("common.loading")}</p>}
            {groupId !== "" && snapshotsQuery.data && snapshotsQuery.data.length === 0 && (
              <EmptyState title={t("pbsJobs.pruneSimNoSnapshots")} />
            )}
            {results && results.length > 0 && <PruneSimulationTimeline results={results} />}
          </div>
        </Modal>
      )}
    </>
  );
}
