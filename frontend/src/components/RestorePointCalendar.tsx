import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import type { BackupSnapshotOut } from "../api/types";
import { formatBytes } from "../lib/formatBytes";
import { verifyStatusPresentation } from "../lib/statusPresentation";
import { ChevronDownIcon } from "./icons";
import { StatusBadge } from "./StatusBadge";
import styles from "./RestorePointCalendar.module.css";

export type RestorePointStatus = "indexed" | "indexable";

interface RestorePointCalendarProps {
  snapshots: BackupSnapshotOut[];
  selectedSnapshotId: string;
  onSelect: (id: string) => void;
  /** When given, each day's dot (and each row in a multi-snapshot day's
   * picklist) is colored by index status instead of the plain neutral
   * dot -- raised directly by the user: green for a snapshot with a
   * completed index (instant browse/search), yellow (amber) for one
   * that would fall back to live on-demand browsing. Omitted entirely
   * for every other caller (SnapshotPicker, RestoreCenterPage) -- exact
   * same binary-dot behavior as before, zero risk to them. */
  statusForSnapshot?: (snapshot: BackupSnapshotOut) => RestorePointStatus;
}

function dayKey(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

/** Which month/day this calendar should open on: the selected
 * snapshot's own month/day if there is one, otherwise the most recent
 * snapshot's, otherwise today. Shared by the initial-mount state and
 * the reset effect below (`SnapshotPicker` keeps one `RestorePointCalendar`
 * instance mounted across a backup-group switch, so `snapshots` changing
 * to a different group's restore points must reset the view the same
 * way freshly mounting with that data would).
 */
function mostRecent(snapshots: BackupSnapshotOut[]): BackupSnapshotOut | undefined {
  // Real bug found live (2026-09-28): callers (SnapshotPicker,
  // FileWorkspacePage) pass `snapshots` straight from the API, which
  // orders by `snapshot_time` ascending -- `snapshots[0]` is the
  // *oldest* restore point, not the most recent. A group spanning
  // several months opened on the oldest one's month instead of today's.
  return snapshots.reduce<BackupSnapshotOut | undefined>(
    (latest, s) => (!latest || s.snapshot_time > latest.snapshot_time ? s : latest),
    undefined,
  );
}

function initialView(snapshots: BackupSnapshotOut[], selectedSnapshotId: string): { viewMonth: Date; openDay: string | null } {
  const selected = snapshots.find((s) => s.id === selectedSnapshotId);
  const reference = selected ?? mostRecent(snapshots);
  const referenceDate = reference ? new Date(reference.snapshot_time) : new Date();
  return {
    viewMonth: new Date(referenceDate.getFullYear(), referenceDate.getMonth(), 1),
    openDay: selected ? dayKey(new Date(selected.snapshot_time)) : null,
  };
}

/** A month-grid calendar over a backup group's restore points — raised
 * directly by the user alongside `RestorePointList` as an alternative,
 * more visual way to browse a group's snapshots than a flat dropdown.
 * Days are grouped by *local* calendar day (matching every other
 * timestamp in this app, which is already shown via `toLocaleString()`).
 * A day with more than one restore point expands into a picklist below
 * the grid rather than guessing which one the operator means.
 */
export function RestorePointCalendar({
  snapshots,
  selectedSnapshotId,
  onSelect,
  statusForSnapshot,
}: RestorePointCalendarProps) {
  const { t, i18n } = useTranslation();

  const byDay = useMemo(() => {
    const map = new Map<string, BackupSnapshotOut[]>();
    for (const snapshot of snapshots) {
      const key = dayKey(new Date(snapshot.snapshot_time));
      const bucket = map.get(key) ?? [];
      bucket.push(snapshot);
      map.set(key, bucket);
    }
    for (const bucket of map.values()) bucket.sort((a, b) => b.snapshot_time.localeCompare(a.snapshot_time));
    return map;
  }, [snapshots]);

  const selectedSnapshot = snapshots.find((s) => s.id === selectedSnapshotId);
  const [viewMonth, setViewMonth] = useState(() => initialView(snapshots, selectedSnapshotId).viewMonth);
  const [openDay, setOpenDay] = useState<string | null>(() => initialView(snapshots, selectedSnapshotId).openDay);

  useEffect(() => {
    const view = initialView(snapshots, selectedSnapshotId);
    setViewMonth(view.viewMonth);
    setOpenDay(view.openDay);
    // Only `snapshots` changing means the underlying group changed (a
    // structurally-unchanged refetch keeps the same array reference,
    // per react-query's default structural sharing) -- re-running this
    // just because the user picked a different day (changing
    // `selectedSnapshotId` without a new `snapshots` array) would fight
    // their own in-month navigation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snapshots]);

  const monthLabel = new Intl.DateTimeFormat(i18n.language, { month: "long", year: "numeric" }).format(viewMonth);
  const weekdayLabels = useMemo(() => {
    // A fixed reference week (Mon 2024-01-01) formatted in the current
    // locale, so headers stay correct across language switches without
    // hardcoding English day names.
    const formatter = new Intl.DateTimeFormat(i18n.language, { weekday: "short" });
    return Array.from({ length: 7 }, (_, i) => formatter.format(new Date(2024, 0, 1 + i)));
  }, [i18n.language]);

  const firstOfMonth = new Date(viewMonth.getFullYear(), viewMonth.getMonth(), 1);
  const daysInMonth = new Date(viewMonth.getFullYear(), viewMonth.getMonth() + 1, 0).getDate();
  // Monday-first week layout (0 = Monday .. 6 = Sunday).
  const leadingBlanks = (firstOfMonth.getDay() + 6) % 7;

  const cells: (Date | null)[] = [
    ...Array.from({ length: leadingBlanks }, () => null),
    ...Array.from({ length: daysInMonth }, (_, i) => new Date(viewMonth.getFullYear(), viewMonth.getMonth(), i + 1)),
  ];

  const changeMonth = (delta: number) => setViewMonth(new Date(viewMonth.getFullYear(), viewMonth.getMonth() + delta, 1));

  const openDaySnapshots = openDay ? byDay.get(openDay) ?? [] : [];

  return (
    <div className={styles.calendar}>
      <div className={styles.header}>
        <button type="button" className={`button buttonGhost ${styles.navButton}`} onClick={() => changeMonth(-1)} aria-label={t("restorePointCalendar.previousMonth")}>
          <ChevronDownIcon className={styles.chevronLeft} />
        </button>
        <span className={styles.monthLabel}>{monthLabel}</span>
        <button type="button" className={`button buttonGhost ${styles.navButton}`} onClick={() => changeMonth(1)} aria-label={t("restorePointCalendar.nextMonth")}>
          <ChevronDownIcon className={styles.chevronRight} />
        </button>
      </div>

      <div className={styles.weekdayRow}>
        {weekdayLabels.map((label, i) => (
          <span key={i} className={styles.weekdayLabel}>
            {label}
          </span>
        ))}
      </div>

      <div className={styles.grid}>
        {cells.map((date, i) => {
          if (!date) return <span key={i} className={styles.cellBlank} />;
          const key = dayKey(date);
          const daySnapshots = byDay.get(key);
          const hasSnapshots = (daySnapshots?.length ?? 0) > 0;
          const isSelectedDay = selectedSnapshot ? dayKey(new Date(selectedSnapshot.snapshot_time)) === key : false;
          const isOpen = openDay === key;

          if (!hasSnapshots) {
            return (
              <span key={i} className={styles.cellEmpty}>
                {date.getDate()}
              </span>
            );
          }

          // A day with more than one snapshot shows an optimistic
          // summary: green if *any* of them is indexed (there's at
          // least one instant option today), yellow otherwise -- the
          // real per-snapshot color is only knowable once the
          // day-picklist below is expanded.
          const dotClass = statusForSnapshot
            ? daySnapshots!.some((s) => statusForSnapshot(s) === "indexed")
              ? styles.cellDotIndexed
              : styles.cellDotIndexable
            : "";

          return (
            <button
              key={i}
              type="button"
              className={`${styles.cell} ${isSelectedDay ? styles.cellSelected : ""} ${isOpen ? styles.cellOpen : ""}`}
              onClick={() => {
                if (daySnapshots!.length === 1) {
                  onSelect(daySnapshots![0].id);
                  setOpenDay(null);
                } else {
                  setOpenDay(isOpen ? null : key);
                }
              }}
            >
              <span className={styles.cellDayNumber}>{date.getDate()}</span>
              <span className={`${styles.cellDot} ${dotClass}`} aria-hidden="true" />
            </button>
          );
        })}
      </div>

      {statusForSnapshot && (
        <div className={styles.legend}>
          <span className={styles.legendItem}>
            <span className={`${styles.legendDot} ${styles.cellDotIndexed}`} aria-hidden="true" />
            {t("restorePointCalendar.legendIndexed")}
          </span>
          <span className={styles.legendItem}>
            <span className={`${styles.legendDot} ${styles.cellDotIndexable}`} aria-hidden="true" />
            {t("restorePointCalendar.legendIndexable")}
          </span>
        </div>
      )}

      {openDay && openDaySnapshots.length > 1 && (
        <div className={styles.dayPicklist}>
          <p className="label">{t("restorePointCalendar.restorePointsOnDay", { date: new Date(openDaySnapshots[0].snapshot_time).toLocaleDateString(i18n.language) })}</p>
          <ul className={styles.dayPicklistItems}>
            {openDaySnapshots.map((snapshot) => {
              const { tone, label } = verifyStatusPresentation(snapshot.verify_status, t);
              const rowStatus = statusForSnapshot?.(snapshot);
              return (
                <li key={snapshot.id}>
                  <button
                    type="button"
                    className={`${styles.dayPicklistRow} ${snapshot.id === selectedSnapshotId ? styles.dayPicklistRowSelected : ""}`}
                    onClick={() => onSelect(snapshot.id)}
                  >
                    {statusForSnapshot && (
                      <span
                        className={`${styles.dayPicklistDot} ${rowStatus === "indexed" ? styles.cellDotIndexed : styles.cellDotIndexable}`}
                        aria-hidden="true"
                      />
                    )}
                    <span>{new Date(snapshot.snapshot_time).toLocaleTimeString(i18n.language)}</span>
                    <StatusBadge tone={tone} label={label} />
                    <span className="mutedText">{formatBytes(snapshot.size_bytes)}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}
