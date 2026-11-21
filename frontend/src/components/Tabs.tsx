import { useRef, type KeyboardEvent, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";

import styles from "./Tabs.module.css";

export interface TabItem {
  id: string;
  label: ReactNode;
}

interface TabsProps {
  tabs: TabItem[];
  active: string;
  onChange: (id: string) => void;
  /** Accessible name of the tab list. */
  label: string;
  /** Right-aligned next to the tabs (e.g. the active tab's primary action). */
  actions?: ReactNode;
}

/** WAI-ARIA tabs (2026-10-04): roving tabindex, arrow keys / Home / End
 * move between tabs. Pair each tab with a <TabPanel id=...>. */
export function Tabs({ tabs, active, onChange, label, actions }: TabsProps) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const last = tabs.length - 1;
    const target =
      event.key === "ArrowRight" ? (index === last ? 0 : index + 1)
      : event.key === "ArrowLeft" ? (index === 0 ? last : index - 1)
      : event.key === "Home" ? 0
      : event.key === "End" ? last
      : null;
    if (target === null) return;
    event.preventDefault();
    onChange(tabs[target].id);
    refs.current[tabs[target].id]?.focus();
  };

  return (
    <div className={styles.bar}>
      <div role="tablist" aria-label={label} className={styles.list}>
        {tabs.map((tab, index) => {
          const selected = tab.id === active;
          return (
            <button
              key={tab.id}
              ref={(el) => {
                refs.current[tab.id] = el;
              }}
              type="button"
              role="tab"
              id={`tab-${tab.id}`}
              aria-selected={selected}
              aria-controls={`tabpanel-${tab.id}`}
              tabIndex={selected ? 0 : -1}
              className={selected ? `${styles.tab} ${styles.tabActive}` : styles.tab}
              onClick={() => onChange(tab.id)}
              onKeyDown={(event) => onKeyDown(event, index)}
            >
              {tab.label}
            </button>
          );
        })}
      </div>
      {actions && <div className={styles.actions}>{actions}</div>}
    </div>
  );
}

export function TabPanel({ id, children }: { id: string; children: ReactNode }) {
  return (
    <div role="tabpanel" id={`tabpanel-${id}`} aria-labelledby={`tab-${id}`} className={styles.panel}>
      {children}
    </div>
  );
}

/** Keeps the selected tab in the URL (`?tab=`), so a tab can be linked to
 * and survives a reload. Unknown values fall back to the first tab. */
export function useTabParam(ids: string[]): [string, (id: string) => void] {
  const [params, setParams] = useSearchParams();
  const requested = params.get("tab");
  const active = requested && ids.includes(requested) ? requested : ids[0];
  const setActive = (id: string) => {
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set("tab", id);
        return next;
      },
      { replace: true },
    );
  };
  return [active, setActive];
}
