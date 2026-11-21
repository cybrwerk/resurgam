import styles from "./Spinner.module.css";

/** A small inline busy indicator (inherits the text colour). */
export function Spinner({ label }: { label?: string }) {
  return <span className={styles.spinner} role={label ? "status" : undefined} aria-label={label} aria-hidden={label ? undefined : true} />;
}
