import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { BackupSnapshotOut } from "../api/types";
import { RestorePointCalendar } from "./RestorePointCalendar";

function snapshot(overrides: Partial<BackupSnapshotOut>): BackupSnapshotOut {
  return {
    id: "snap-1",
    backup_group_id: "group-1",
    snapshot_time: "2026-03-15T10:00:00Z",
    verify_status: "ok",
    size_bytes: 1024,
    last_synced_at: "2026-03-15T10:00:00Z",
    ...overrides,
  };
}

describe("RestorePointCalendar", () => {
  it("selects the single restore point on a day directly, with no picklist", async () => {
    const snapshots = [snapshot({ id: "s1", snapshot_time: "2026-03-15T10:00:00Z" })];
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<RestorePointCalendar snapshots={snapshots} selectedSnapshotId="" onSelect={onSelect} />);

    const dayCell = screen.getAllByText("15", { selector: "span" }).map((el) => el.closest("button")).find(Boolean);
    expect(dayCell).toBeTruthy();
    await user.click(dayCell!);

    expect(onSelect).toHaveBeenCalledWith("s1");
  });

  it("opens a picklist when a day has more than one restore point, resolved only by an explicit choice", async () => {
    const snapshots = [
      snapshot({ id: "s1", snapshot_time: "2026-03-15T02:00:00Z" }),
      snapshot({ id: "s2", snapshot_time: "2026-03-15T14:00:00Z" }),
    ];
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<RestorePointCalendar snapshots={snapshots} selectedSnapshotId="" onSelect={onSelect} />);

    const dayCell = screen.getAllByText("15", { selector: "span" }).map((el) => el.closest("button")).find(Boolean);
    await user.click(dayCell!);

    expect(onSelect).not.toHaveBeenCalled();
    const times = [
      new Date("2026-03-15T02:00:00Z").toLocaleTimeString("en"),
      new Date("2026-03-15T14:00:00Z").toLocaleTimeString("en"),
    ];
    expect(screen.getByText(times[0])).toBeInTheDocument();
    expect(screen.getByText(times[1])).toBeInTheDocument();

    await user.click(screen.getByText(times[1]));
    expect(onSelect).toHaveBeenCalledWith("s2");
  });

  it("navigates between months without losing the day-with-restore-points marker", async () => {
    const snapshots = [snapshot({ id: "s1", snapshot_time: "2026-03-15T10:00:00Z" })];
    const user = userEvent.setup();
    render(<RestorePointCalendar snapshots={snapshots} selectedSnapshotId="s1" onSelect={vi.fn()} />);

    expect(screen.getByText(/March 2026|März 2026/)).toBeInTheDocument();

    await user.click(screen.getByLabelText("Previous month"));
    expect(screen.getByText(/February 2026|Februar 2026/)).toBeInTheDocument();
    expect(screen.queryByText("15", { selector: "span" })?.closest("button")).toBeFalsy();

    await user.click(screen.getByLabelText("Next month"));
    expect(screen.getByText(/March 2026|März 2026/)).toBeInTheDocument();
  });

  it("jumps to the new group's month when snapshots change on an already-mounted instance", () => {
    // Regression test: SnapshotPicker keeps one RestorePointCalendar
    // instance mounted across a backup-group switch -- switching from a
    // group with March restore points to one with only January restore
    // points must show January, not keep showing the empty March grid.
    const marchSnapshots = [snapshot({ id: "s1", snapshot_time: "2026-03-15T10:00:00Z" })];
    const januarySnapshots = [snapshot({ id: "s2", snapshot_time: "2026-01-10T10:00:00Z" })];
    const { rerender } = render(<RestorePointCalendar snapshots={marchSnapshots} selectedSnapshotId="" onSelect={vi.fn()} />);
    expect(screen.getByText(/March 2026|März 2026/)).toBeInTheDocument();

    rerender(<RestorePointCalendar snapshots={januarySnapshots} selectedSnapshotId="" onSelect={vi.fn()} />);

    expect(screen.getByText(/January 2026|Januar 2026/)).toBeInTheDocument();
  });

  it("opens on the most recent snapshot's month even when the array is ordered oldest-first", () => {
    // Real bug found live (2026-09-28): the API orders snapshots by
    // snapshot_time ascending, so snapshots[0] is the oldest one, not
    // the most recent -- a group spanning several months must still
    // open on the most recent month, not the oldest.
    const snapshots = [
      snapshot({ id: "s1", snapshot_time: "2026-01-10T10:00:00Z" }),
      snapshot({ id: "s2", snapshot_time: "2026-03-20T10:00:00Z" }),
    ];
    render(<RestorePointCalendar snapshots={snapshots} selectedSnapshotId="" onSelect={vi.fn()} />);

    expect(screen.getByText(/March 2026|März 2026/)).toBeInTheDocument();
  });

  it("marks the selected snapshot's day distinctly", () => {
    const snapshots = [
      snapshot({ id: "s1", snapshot_time: "2026-03-05T10:00:00Z" }),
      snapshot({ id: "s2", snapshot_time: "2026-03-15T10:00:00Z" }),
    ];
    render(<RestorePointCalendar snapshots={snapshots} selectedSnapshotId="s2" onSelect={vi.fn()} />);

    const selectedCell = screen.getAllByText("15", { selector: "span" }).map((el) => el.closest("button")).find(Boolean);
    expect(selectedCell?.className).toMatch(/cellSelected/);
  });

  it("colors a single-snapshot day's dot by its real status, and shows no legend when statusForSnapshot is omitted", () => {
    const snapshots = [snapshot({ id: "s1", snapshot_time: "2026-03-05T10:00:00Z" })];
    render(<RestorePointCalendar snapshots={snapshots} selectedSnapshotId="" onSelect={vi.fn()} />);

    const cell = screen.getAllByText("5", { selector: "span" }).map((el) => el.closest("button")).find(Boolean);
    const dot = cell?.querySelector("span:last-child");
    expect(dot?.className).not.toMatch(/cellDotIndexed|cellDotIndexable/);
    expect(screen.queryByText("Indexed — instant browse & search")).not.toBeInTheDocument();
  });

  it("colors a single-snapshot day green when indexed, yellow when not, and shows the legend", () => {
    const snapshots = [
      snapshot({ id: "s1", snapshot_time: "2026-03-05T10:00:00Z" }),
      snapshot({ id: "s2", snapshot_time: "2026-03-20T10:00:00Z" }),
    ];
    render(
      <RestorePointCalendar
        snapshots={snapshots}
        selectedSnapshotId=""
        onSelect={vi.fn()}
        statusForSnapshot={(s) => (s.id === "s1" ? "indexed" : "indexable")}
      />,
    );

    const day5 = screen.getAllByText("5", { selector: "span" }).map((el) => el.closest("button")).find(Boolean);
    const day20 = screen.getAllByText("20", { selector: "span" }).map((el) => el.closest("button")).find(Boolean);
    expect(day5?.querySelector("span:last-child")?.className).toMatch(/cellDotIndexed/);
    expect(day20?.querySelector("span:last-child")?.className).toMatch(/cellDotIndexable/);
    expect(screen.getByText("Indexed — instant browse & search")).toBeInTheDocument();
    expect(screen.getByText("Not indexed yet — live browsing")).toBeInTheDocument();
  });

  it("shows a mixed-status day's dot as green (optimistic) if any snapshot that day is indexed", async () => {
    const snapshots = [
      snapshot({ id: "s1", snapshot_time: "2026-03-15T02:00:00Z" }),
      snapshot({ id: "s2", snapshot_time: "2026-03-15T14:00:00Z" }),
    ];
    const user = userEvent.setup();
    render(
      <RestorePointCalendar
        snapshots={snapshots}
        selectedSnapshotId=""
        onSelect={vi.fn()}
        statusForSnapshot={(s) => (s.id === "s2" ? "indexed" : "indexable")}
      />,
    );

    const day15 = screen.getAllByText("15", { selector: "span" }).map((el) => el.closest("button")).find(Boolean);
    expect(day15?.querySelector("span:last-child")?.className).toMatch(/cellDotIndexed/);

    // Expanding the picklist reveals the real per-snapshot color.
    await user.click(day15!);
    const times = [
      new Date("2026-03-15T02:00:00Z").toLocaleTimeString("en"),
      new Date("2026-03-15T14:00:00Z").toLocaleTimeString("en"),
    ];
    const row1 = screen.getByText(times[0]).closest("button");
    const row2 = screen.getByText(times[1]).closest("button");
    expect(row1?.querySelector("span")?.className).toMatch(/cellDotIndexable/);
    expect(row2?.querySelector("span")?.className).toMatch(/cellDotIndexed/);
  });
});
