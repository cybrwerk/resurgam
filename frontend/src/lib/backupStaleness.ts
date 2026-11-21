// A real UX-audit finding (2026-09-23): nothing in the app visually
// distinguished a workload backed up an hour ago from one unprotected
// for years. 48h mirrors the Protection Policy form's own default "max
// hours since last backup"; 30 days is the point where "overdue"
// becomes "effectively unprotected," warranting the same danger tone a
// failed check gets elsewhere in this app. Shared by Protected
// Workloads (the row-level flag/sort) and the Customer Panel (the
// fleet-wide summary count) so the two never drift apart on what
// "stale" means.
export const STALE_WARNING_HOURS = 48;
export const STALE_DANGER_DAYS = 30;

export function hoursSinceLastBackup(lastBackupAt: string | null): number | null {
  if (!lastBackupAt) return null;
  return (Date.now() - new Date(lastBackupAt).getTime()) / (1000 * 60 * 60);
}

export function backupStaleness(lastBackupAt: string | null): "danger" | "warning" | null {
  const hours = hoursSinceLastBackup(lastBackupAt);
  if (hours === null || hours >= STALE_DANGER_DAYS * 24) return "danger";
  if (hours >= STALE_WARNING_HOURS) return "warning";
  return null;
}
