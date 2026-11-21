import { useTranslation } from "react-i18next";

import type { PbsInstanceOut } from "../api/types";
import { collectorStaleness } from "../lib/collectorStaleness";

/** One-line staleness note for the cross-entity Jobs pages: this data
 * comes from the Collector's own periodic sync
 * (`worker/collector_scheduler.py`, interval editable in System
 * Settings), not a live PVE/PBS read on every page load -- raised
 * directly by the user after finding no visible indication of how
 * fresh it was. Rendered as a plain inline span because the pages pass
 * it as the `PageHeader` description (which supplies the <p> + muted
 * styling itself).
 */
export function CollectorStalenessNote({
  instances,
  instanceIds,
}: {
  instances: PbsInstanceOut[];
  instanceIds: string[];
}) {
  const { t } = useTranslation();
  if (instanceIds.length === 0) return null;

  const { oldestSyncedAt, anyNeverSynced } = collectorStaleness(instances, instanceIds);
  if (anyNeverSynced) {
    return <span>{t("jobs.collectorNeverSynced")}</span>;
  }
  if (!oldestSyncedAt) return null;

  return <span>{t("jobs.collectorDataAsOf", { time: new Date(oldestSyncedAt).toLocaleString() })}</span>;
}
