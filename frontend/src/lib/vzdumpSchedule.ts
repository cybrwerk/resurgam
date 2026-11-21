// PVE's vzdump job `schedule` field is a "calendar event" string (based
// on, but not identical to, systemd's OnCalendar grammar -- PBS's own
// docs describe it as "mostly compatible"). Every shape this module
// generates is taken verbatim from Proxmox's own primary documentation,
// not just systemd's general grammar (checked 2026-09-29 against
// https://raw.githubusercontent.com/proxmox/pve-docs/master/calendar-events.adoc's
// own Schedule Examples table):
//   - daily "12:05"                              -> table row "12:05"
//   - weekly "mon,wed,fri 02:30"                  -> table row "mon,wed,fri" (day list) + the
//                                                     general "[WEEKDAY] [HOURS:MINUTES]" grammar
//   - every-N-minutes "*/15"                      -> table row "*/5" ("Every five minutes"),
//                                                     the doc's own *primary* column, not its
//                                                     "0/5" alternative
//   - every-N-hours "0/6:00"                      -> same shape as the doc's "12/2:5" alternative
//                                                     (hour-step "start/step", minute offset after ':')
// PBS's own calendar-events docs use the same grammar for prune/sync/
// verify job schedules, so this module is reusable there too if that
// ever gets its own assistant.
//
// This module only recognizes/generates a deliberately small, common
// subset (fixed daily time, weekly on chosen days, a fixed-step
// interval) -- anything else (named aliases like "daily"/"hourly", day
// ranges like "mon..fri", multi-part date specs) falls through to
// "advanced", where the raw string is edited directly rather than risk
// misparsing or mangling it.

export const WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
export type Weekday = (typeof WEEKDAYS)[number];

export type ScheduleMode = "daily" | "weekly" | "interval" | "advanced";
export type IntervalUnit = "minutes" | "hours";

export type ParsedSchedule =
  | { mode: "daily"; time: string }
  | { mode: "weekly"; days: Weekday[]; time: string }
  | { mode: "interval"; unit: IntervalUnit; value: number }
  | { mode: "advanced"; raw: string };

const TIME_RE = /^([01]\d|2[0-3]):([0-5]\d)$/;
const DAY_TOKEN = "(?:mon|tue|wed|thu|fri|sat|sun)";
const WEEKLY_RE = new RegExp(`^(${DAY_TOKEN}(?:,${DAY_TOKEN})*) ([01]\\d|2[0-3]):([0-5]\\d)$`);
// Recognizes every form the docs list as equivalent ("*/5" | "0/5"),
// plus the more explicit "*:0/5" this module generated before this was
// checked against the docs -- generation always uses "*/N" (see below),
// this stays deliberately liberal only for *parsing* an existing value.
const INTERVAL_MINUTES_RE = /^(?:\*|0)(?::0)?\/([1-9]\d*)$/;
const INTERVAL_HOURS_RE = /^0\/([1-9]\d*):00$/;

export function parseVzdumpSchedule(value: string): ParsedSchedule {
  const trimmed = value.trim();
  if (TIME_RE.test(trimmed)) return { mode: "daily", time: trimmed };

  const weeklyMatch = trimmed.match(WEEKLY_RE);
  if (weeklyMatch) {
    return { mode: "weekly", days: weeklyMatch[1].split(",") as Weekday[], time: `${weeklyMatch[2]}:${weeklyMatch[3]}` };
  }

  const minutesMatch = trimmed.match(INTERVAL_MINUTES_RE);
  if (minutesMatch) return { mode: "interval", unit: "minutes", value: Number(minutesMatch[1]) };

  const hoursMatch = trimmed.match(INTERVAL_HOURS_RE);
  if (hoursMatch) return { mode: "interval", unit: "hours", value: Number(hoursMatch[1]) };

  return { mode: "advanced", raw: value };
}

export function formatDailySchedule(time: string): string {
  return time;
}

/** Empty `days` has no valid representation -- callers must gate on
 * `days.length > 0` themselves (e.g. disabling Save) rather than get a
 * silently-wrong string back. */
export function formatWeeklySchedule(days: Weekday[], time: string): string {
  return days.length === 0 ? "" : `${days.join(",")} ${time}`;
}

export function formatIntervalSchedule(unit: IntervalUnit, value: number): string {
  return unit === "minutes" ? `*/${value}` : `0/${value}:00`;
}
