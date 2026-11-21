import type { PbsInstanceOut } from "../api/types";

export interface CollectorStaleness {
  /** The oldest `last_collector_sync_at` among the relevant instances --
   * "as stale as the least-recently-synced one shown on this page", not
   * an average or the most recent. `null` when no relevant instance has
   * ever synced (or there are no relevant instances at all). */
  oldestSyncedAt: string | null;
  anyNeverSynced: boolean;
}

/** Feeds the Jobs pages' "Data as of ..." note (raised directly by the
 * user after finding the Collector's sync had no visible cadence at
 * all) -- `instances` is typically the full `pbsInstancesApi.list()`
 * result, `instanceIds` the subset actually shown on this page (derived
 * from the datastores/clusters fanned out over).
 */
export function collectorStaleness(instances: PbsInstanceOut[], instanceIds: string[]): CollectorStaleness {
  const relevant = instances.filter((i) => instanceIds.includes(i.id));
  if (relevant.length === 0) return { oldestSyncedAt: null, anyNeverSynced: false };

  const anyNeverSynced = relevant.some((i) => i.last_collector_sync_at === null);
  const timestamps = relevant
    .map((i) => i.last_collector_sync_at)
    .filter((t): t is string => t !== null);
  const oldestSyncedAt = timestamps.length > 0 ? timestamps.reduce((a, b) => (a < b ? a : b)) : null;

  return { oldestSyncedAt, anyNeverSynced };
}
