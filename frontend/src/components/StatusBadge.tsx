import styles from "./StatusBadge.module.css";

export type StatusTone = "ok" | "warning" | "danger" | "unknown" | "info";

interface StatusBadgeProps {
  tone: StatusTone;
  label: string;
  /** Extra class applied alongside the badge's own — e.g. to allow a
   * caller's full-sentence label to wrap instead of the default nowrap
   * (correct for this badge's usual short-label usage, but not for
   * longer explanatory text like restore-plan check messages). */
  className?: string;
}

/** Petrol & Brass status pill (pb-status): a dot and the word on the soft
 * background of its status colour. The word is always there -- status is
 * never conveyed by colour alone (manifest §26.6/§26.9). */
export function StatusBadge({ tone, label, className }: StatusBadgeProps) {
  return <span className={[styles.badge, styles[tone], className].filter(Boolean).join(" ")}>{label}</span>;
}
