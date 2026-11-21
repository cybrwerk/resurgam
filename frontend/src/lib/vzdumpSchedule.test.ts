import { describe, expect, it } from "vitest";

import {
  formatDailySchedule,
  formatIntervalSchedule,
  formatWeeklySchedule,
  parseVzdumpSchedule,
} from "./vzdumpSchedule";

describe("parseVzdumpSchedule", () => {
  it("recognizes a fixed daily time", () => {
    expect(parseVzdumpSchedule("21:00")).toEqual({ mode: "daily", time: "21:00" });
    expect(parseVzdumpSchedule("02:30")).toEqual({ mode: "daily", time: "02:30" });
  });

  it("recognizes a weekly day list with a time", () => {
    expect(parseVzdumpSchedule("mon,wed,fri 02:30")).toEqual({
      mode: "weekly",
      days: ["mon", "wed", "fri"],
      time: "02:30",
    });
    expect(parseVzdumpSchedule("sun 00:00")).toEqual({ mode: "weekly", days: ["sun"], time: "00:00" });
  });

  it("recognizes a fixed-step minute interval in any of PVE's documented equivalent forms", () => {
    // "*/5" is the docs' own primary/canonical column, "0/5" its listed
    // alternative; "*:0/5" is a third, more explicit but equally valid
    // form under the same grammar.
    expect(parseVzdumpSchedule("*/15")).toEqual({ mode: "interval", unit: "minutes", value: 15 });
    expect(parseVzdumpSchedule("0/15")).toEqual({ mode: "interval", unit: "minutes", value: 15 });
    expect(parseVzdumpSchedule("*:0/15")).toEqual({ mode: "interval", unit: "minutes", value: 15 });
  });

  it("recognizes a fixed-step hour interval", () => {
    expect(parseVzdumpSchedule("0/6:00")).toEqual({ mode: "interval", unit: "hours", value: 6 });
  });

  it("falls back to advanced for named aliases and day ranges rather than misparsing them", () => {
    expect(parseVzdumpSchedule("daily")).toEqual({ mode: "advanced", raw: "daily" });
    expect(parseVzdumpSchedule("hourly")).toEqual({ mode: "advanced", raw: "hourly" });
    expect(parseVzdumpSchedule("mon..fri 21:00")).toEqual({ mode: "advanced", raw: "mon..fri 21:00" });
    expect(parseVzdumpSchedule("")).toEqual({ mode: "advanced", raw: "" });
  });
});

describe("format*Schedule", () => {
  it("formats a daily schedule as just the time", () => {
    expect(formatDailySchedule("03:00")).toBe("03:00");
  });

  it("formats a weekly schedule as a comma day list plus time", () => {
    expect(formatWeeklySchedule(["mon", "wed", "fri"], "02:30")).toBe("mon,wed,fri 02:30");
  });

  it("returns an empty string for a weekly schedule with no days chosen", () => {
    expect(formatWeeklySchedule([], "02:30")).toBe("");
  });

  it("formats a minute interval as PVE docs' own canonical form", () => {
    // Verbatim from https://raw.githubusercontent.com/proxmox/pve-docs/master/calendar-events.adoc's
    // Schedule Examples table: "*/5" (primary column) -> "Every five minutes".
    expect(formatIntervalSchedule("minutes", 15)).toBe("*/15");
  });

  it("formats an hour interval as PVE's step syntax", () => {
    expect(formatIntervalSchedule("hours", 6)).toBe("0/6:00");
  });

  it("round-trips every format through parse", () => {
    expect(parseVzdumpSchedule(formatDailySchedule("14:45"))).toEqual({ mode: "daily", time: "14:45" });
    expect(parseVzdumpSchedule(formatWeeklySchedule(["tue", "thu"], "05:00"))).toEqual({
      mode: "weekly",
      days: ["tue", "thu"],
      time: "05:00",
    });
    expect(parseVzdumpSchedule(formatIntervalSchedule("minutes", 30))).toEqual({
      mode: "interval",
      unit: "minutes",
      value: 30,
    });
    expect(parseVzdumpSchedule(formatIntervalSchedule("hours", 12))).toEqual({
      mode: "interval",
      unit: "hours",
      value: 12,
    });
  });
});
