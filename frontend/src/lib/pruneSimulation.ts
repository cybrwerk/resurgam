// Simulates PBS's real prune-retention selection algorithm client-side,
// against a backup group's actual snapshot history -- raised directly
// by the user ("a prune simulation on a timeline like synology is doing
// it... Proxmox provides a prune simulator"), so a keep-last/hourly/
// daily/weekly/monthly/yearly policy can be previewed before it's saved
// and actually runs against real backups.
//
// Algorithm (PBS's own, confirmed docs: https://pbs.proxmox.com/docs/maintenance.html):
// snapshots are sorted newest-first; `keep-last` marks the N most recent
// as kept outright; each of hourly/daily/weekly/monthly/yearly then
// independently partitions the *entire* snapshot history into its own
// calendar buckets and keeps the single newest snapshot in each of the
// N most recent non-empty buckets. A snapshot survives if kept by *any*
// rule -- rules are a union, not applied in sequence against a shrinking
// pool. An unset (null/undefined/<=0) keep-* option means that rule
// doesn't apply at all, not "keep unlimited" -- a policy with every
// keep-* option unset keeps nothing (mirrors real PBS: an empty prune
// job would remove everything), which is exactly the kind of mistake
// this simulation exists to catch before it's saved.

export type PruneRule = "last" | "hourly" | "daily" | "weekly" | "monthly" | "yearly";

export interface PruneKeepOptions {
  keepLast?: number | null;
  keepHourly?: number | null;
  keepDaily?: number | null;
  keepWeekly?: number | null;
  keepMonthly?: number | null;
  keepYearly?: number | null;
}

export interface SimulatedSnapshot {
  timestamp: Date;
  kept: boolean;
  keptByRules: PruneRule[];
}

const pad = (n: number) => String(n).padStart(2, "0");

function hourlyKey(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}-${pad(d.getHours())}`;
}
function dailyKey(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
function monthlyKey(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
}
function yearlyKey(d: Date): string {
  return `${d.getFullYear()}`;
}
/** ISO-8601 week-year + week number (Monday-start weeks, week 1 contains
 * the year's first Thursday) -- the standard definition, matching how
 * PBS's own "weekly" bucket is understood. */
function weeklyKey(d: Date): string {
  const date = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const dayNum = date.getDay() || 7; // Sun=0 -> 7, so Mon=1..Sun=7
  date.setDate(date.getDate() + 4 - dayNum); // Thursday of this ISO week
  const yearStart = new Date(date.getFullYear(), 0, 1);
  const weekNo = Math.ceil(((date.getTime() - yearStart.getTime()) / 86_400_000 + 1) / 7);
  return `${date.getFullYear()}-W${pad(weekNo)}`;
}

const BUCKET_RULES: [Exclude<PruneRule, "last">, (d: Date) => string][] = [
  ["hourly", hourlyKey],
  ["daily", dailyKey],
  ["weekly", weeklyKey],
  ["monthly", monthlyKey],
  ["yearly", yearlyKey],
];

export function simulatePrune(timestamps: Date[], options: PruneKeepOptions): SimulatedSnapshot[] {
  const sorted = [...timestamps].sort((a, b) => b.getTime() - a.getTime());
  const keptByRule: Record<PruneRule, Set<number>> = {
    last: new Set(), hourly: new Set(), daily: new Set(), weekly: new Set(), monthly: new Set(), yearly: new Set(),
  };

  if (options.keepLast && options.keepLast > 0) {
    for (let i = 0; i < Math.min(options.keepLast, sorted.length); i++) keptByRule.last.add(i);
  }

  const counts: Record<Exclude<PruneRule, "last">, number | null | undefined> = {
    hourly: options.keepHourly, daily: options.keepDaily, weekly: options.keepWeekly,
    monthly: options.keepMonthly, yearly: options.keepYearly,
  };

  for (const [rule, keyFn] of BUCKET_RULES) {
    const count = counts[rule];
    if (!count || count <= 0) continue;
    const seenBuckets = new Set<string>();
    for (let i = 0; i < sorted.length && seenBuckets.size < count; i++) {
      const key = keyFn(sorted[i]);
      if (seenBuckets.has(key)) continue;
      seenBuckets.add(key);
      keptByRule[rule].add(i);
    }
  }

  return sorted.map((timestamp, i) => {
    const keptByRules = (Object.keys(keptByRule) as PruneRule[]).filter((rule) => keptByRule[rule].has(i));
    return { timestamp, kept: keptByRules.length > 0, keptByRules };
  });
}
