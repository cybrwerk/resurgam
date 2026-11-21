import { useTranslation } from "react-i18next";

import type { SimulatedSnapshot } from "../lib/pruneSimulation";
import { StatusBadge } from "./StatusBadge";
import styles from "./PruneSimulationTimeline.module.css";

interface PruneSimulationTimelineProps {
  results: SimulatedSnapshot[];
}

/** A horizontal timeline of real snapshot history, marking which
 * snapshots a retention policy would keep vs. prune -- raised directly
 * by the user, modeled on Synology Active Backup's per-job retention
 * preview and Proxmox's own Prune Simulator tool. Kept/pruned is never
 * color-only (manifest §26.6/§26.9): the legend pairs color with a
 * label, and the marks themselves differ in size/shape too, not just hue.
 */
export function PruneSimulationTimeline({ results }: PruneSimulationTimelineProps) {
  const { t } = useTranslation();

  if (results.length === 0) return null;

  const sorted = [...results].sort((a, b) => a.timestamp.getTime() - b.timestamp.getTime());
  const minTime = sorted[0].timestamp.getTime();
  const maxTime = sorted[sorted.length - 1].timestamp.getTime();
  const span = Math.max(1, maxTime - minTime);

  const keptCount = results.filter((r) => r.kept).length;
  const prunedCount = results.length - keptCount;

  return (
    <div className={styles.container}>
      <div className={styles.summary}>
        <StatusBadge tone="ok" label={t("pbsJobs.pruneSimKeptCount", { count: keptCount })} />
        <StatusBadge tone="danger" label={t("pbsJobs.pruneSimPrunedCount", { count: prunedCount })} />
      </div>

      {keptCount === 0 && <StatusBadge tone="danger" label={t("pbsJobs.pruneSimNothingKeptWarning")} />}

      <div className={styles.legend}>
        <span className={styles.legendItem}>
          <span className={`${styles.mark} ${styles.markKept}`} /> {t("pbsJobs.pruneSimLegendKept")}
        </span>
        <span className={styles.legendItem}>
          <span className={`${styles.mark} ${styles.markPruned}`} /> {t("pbsJobs.pruneSimLegendPruned")}
        </span>
      </div>

      <div className={styles.track}>
        {sorted.map((r, i) => {
          const pct = ((r.timestamp.getTime() - minTime) / span) * 100;
          const title = r.kept
            ? `${r.timestamp.toLocaleString()} — ${t("pbsJobs.pruneSimKeptByRules", { rules: r.keptByRules.map((rule) => t(`pbsJobs.pruneSimRule_${rule}`)).join(", ") })}`
            : `${r.timestamp.toLocaleString()} — ${t("pbsJobs.pruneSimLegendPruned")}`;
          return (
            <div
              key={i}
              className={`${styles.mark} ${r.kept ? styles.markKept : styles.markPruned}`}
              style={{ left: `${pct}%` }}
              title={title}
            />
          );
        })}
      </div>

      <div className={styles.axisLabels}>
        <span>{sorted[0].timestamp.toLocaleDateString()}</span>
        <span>{sorted[sorted.length - 1].timestamp.toLocaleDateString()}</span>
      </div>
    </div>
  );
}
