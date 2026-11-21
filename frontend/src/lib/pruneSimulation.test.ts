import { describe, expect, it } from "vitest";

import { simulatePrune } from "./pruneSimulation";

function d(iso: string): Date {
  return new Date(iso);
}

// Local-time (not UTC) date/datetime keys, matching how the production
// algorithm reads dates -- `.toISOString()` converts to UTC first, which
// silently shifts the calendar day near local midnight and would make
// these assertions timezone-dependent for the wrong reason.
function localDate(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}
function localDateTime(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${localDate(date)}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

describe("simulatePrune", () => {
  it("keeps nothing when no keep-* option is set -- mirrors a real empty PBS prune job", () => {
    const snapshots = [d("2026-01-01T00:00:00"), d("2026-01-02T00:00:00")];
    const result = simulatePrune(snapshots, {});
    expect(result.every((r) => !r.kept)).toBe(true);
  });

  it("keep-last keeps exactly the N most recent snapshots, oldest excess pruned", () => {
    const snapshots = [
      d("2026-01-01T00:00:00"), d("2026-01-02T00:00:00"), d("2026-01-03T00:00:00"), d("2026-01-04T00:00:00"),
    ];
    const result = simulatePrune(snapshots, { keepLast: 2 });
    // Result is sorted newest-first.
    expect(result.map((r) => [localDate(r.timestamp), r.kept])).toEqual([
      ["2026-01-04", true],
      ["2026-01-03", true],
      ["2026-01-02", false],
      ["2026-01-01", false],
    ]);
  });

  it("keep-daily keeps only the newest snapshot per day, for the N most recent days", () => {
    const snapshots = [
      d("2026-01-05T08:00:00"), d("2026-01-05T20:00:00"), // same day, only the later one should survive
      d("2026-01-04T12:00:00"),
      d("2026-01-03T12:00:00"), // this day should be pruned if keepDaily=2
    ];
    const result = simulatePrune(snapshots, { keepDaily: 2 });
    const kept = result.filter((r) => r.kept).map((r) => localDateTime(r.timestamp));
    expect(kept).toEqual([localDateTime(d("2026-01-05T20:00:00")), localDateTime(d("2026-01-04T12:00:00"))]);
  });

  it("a snapshot kept by multiple rules reports every rule that kept it", () => {
    const snapshots = [d("2026-01-05T20:00:00")];
    const result = simulatePrune(snapshots, { keepLast: 1, keepDaily: 1, keepWeekly: 1 });
    expect(result[0].kept).toBe(true);
    expect(result[0].keptByRules.sort()).toEqual(["daily", "last", "weekly"]);
  });

  it("keep-weekly keeps the newest snapshot per ISO week, for the N most recent weeks", () => {
    // 2026-01-05 is a Monday, 2026-01-11 is the following Sunday (same ISO week);
    // 2026-01-12 is a Monday, starting the next ISO week.
    const snapshots = [d("2026-01-05T00:00:00"), d("2026-01-11T00:00:00"), d("2026-01-12T00:00:00")];
    const result = simulatePrune(snapshots, { keepWeekly: 1 });
    const kept = result.filter((r) => r.kept).map((r) => localDate(r.timestamp));
    // Only the most recent ISO week (containing 2026-01-12) survives, and
    // only its newest snapshot.
    expect(kept).toEqual(["2026-01-12"]);
  });

  it("a snapshot rejected by every rule is pruned", () => {
    const snapshots = [d("2026-01-01T00:00:00"), d("2026-06-01T00:00:00")];
    const result = simulatePrune(snapshots, { keepLast: 1 });
    const jan = result.find((r) => localDate(r.timestamp).startsWith("2026-01"))!;
    expect(jan.kept).toBe(false);
    expect(jan.keptByRules).toEqual([]);
  });

  it("returns results sorted newest-first regardless of input order", () => {
    const snapshots = [d("2026-01-01T00:00:00"), d("2026-03-01T00:00:00"), d("2026-02-01T00:00:00")];
    const result = simulatePrune(snapshots, {});
    expect(result.map((r) => localDate(r.timestamp).slice(0, 7))).toEqual(["2026-03", "2026-02", "2026-01"]);
  });
});
