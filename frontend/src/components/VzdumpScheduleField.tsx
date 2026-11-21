import { useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  formatDailySchedule,
  formatIntervalSchedule,
  formatWeeklySchedule,
  parseVzdumpSchedule,
  WEEKDAYS,
  type IntervalUnit,
  type ScheduleMode,
  type Weekday,
} from "../lib/vzdumpSchedule";
import styles from "./VzdumpScheduleField.module.css";

interface VzdumpScheduleFieldProps {
  value: string;
  onChange: (value: string) => void;
}

/** A friendly assistant over PVE's raw calendar-event schedule string --
 * raised directly by the user ("the normal notation and cron are not
 * that easy for weak techies... daily, weekly, days selector and also
 * do other cron like intervals like every-15-minutes or so"). Owns its
 * own editing state (mode + per-mode fields), seeded once from the incoming value
 * at mount, and pushes the computed raw string upward on every change --
 * deriving mode from the string on every render instead would snap the
 * picker back to "Advanced" the instant a weekly selection had zero
 * days checked (its formatted form doesn't round-trip through the
 * recognizer until a day is picked).
 */
export function VzdumpScheduleField({ value, onChange }: VzdumpScheduleFieldProps) {
  const { t } = useTranslation();
  const modeId = useId();

  const [initial] = useState(() => parseVzdumpSchedule(value));
  const [mode, setMode] = useState<ScheduleMode>(initial.mode);
  const [dailyTime, setDailyTime] = useState(initial.mode === "daily" ? initial.time : "00:00");
  const [weeklyDays, setWeeklyDays] = useState<Weekday[]>(initial.mode === "weekly" ? initial.days : []);
  const [weeklyTime, setWeeklyTime] = useState(initial.mode === "weekly" ? initial.time : "00:00");
  const [intervalUnit, setIntervalUnit] = useState<IntervalUnit>(initial.mode === "interval" ? initial.unit : "minutes");
  const [intervalValue, setIntervalValue] = useState(initial.mode === "interval" ? String(initial.value) : "15");
  const [advancedRaw, setAdvancedRaw] = useState(initial.mode === "advanced" ? initial.raw : value);

  useEffect(() => {
    const next =
      mode === "daily"
        ? formatDailySchedule(dailyTime)
        : mode === "weekly"
          ? formatWeeklySchedule(weeklyDays, weeklyTime)
          : mode === "interval"
            ? formatIntervalSchedule(intervalUnit, Math.max(1, Number(intervalValue) || 1))
            : advancedRaw;
    if (next !== value) onChange(next);
    // Only the fields that actually feed the computed string belong here --
    // `value`/`onChange` deliberately excluded (this effect drives them, not
    // the other way around; see the component doc above).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, dailyTime, weeklyDays, weeklyTime, intervalUnit, intervalValue, advancedRaw]);

  const toggleDay = (day: Weekday) =>
    setWeeklyDays((prev) => (prev.includes(day) ? prev.filter((d) => d !== day) : [...prev, day]));

  const handleModeChange = (nextMode: ScheduleMode) => {
    // Advanced's own raw text otherwise stays frozen at whatever it was
    // seeded with at mount -- switching Daily/Weekly/Interval -> Advanced
    // to fine-tune a detail must start from what was just configured,
    // not silently discard it.
    if (nextMode === "advanced") setAdvancedRaw(value);
    setMode(nextMode);
  };

  return (
    <div className={styles.field}>
      <label className="label" htmlFor={modeId}>
        {t("pveClusters.backupJobSchedule")}
      </label>
      <select id={modeId} className="select" value={mode} onChange={(e) => handleModeChange(e.target.value as ScheduleMode)}>
        <option value="daily">{t("pveClusters.scheduleModeDaily")}</option>
        <option value="weekly">{t("pveClusters.scheduleModeWeekly")}</option>
        <option value="interval">{t("pveClusters.scheduleModeInterval")}</option>
        <option value="advanced">{t("pveClusters.scheduleModeAdvanced")}</option>
      </select>

      {mode === "daily" && (
        <div className={styles.controls}>
          <label className={styles.inlineLabel}>
            {t("pveClusters.scheduleAt")}
            <input type="time" className={`input ${styles.narrowInput}`} value={dailyTime} onChange={(e) => setDailyTime(e.target.value)} required />
          </label>
        </div>
      )}

      {mode === "weekly" && (
        <div className={styles.controls}>
          <div className={styles.dayPicker}>
            {WEEKDAYS.map((day) => (
              <label key={day} className={styles.dayToggle}>
                <input type="checkbox" checked={weeklyDays.includes(day)} onChange={() => toggleDay(day)} />
                {t(`pveClusters.weekday_${day}`)}
              </label>
            ))}
          </div>
          <label className={styles.inlineLabel}>
            {t("pveClusters.scheduleAt")}
            <input type="time" className={`input ${styles.narrowInput}`} value={weeklyTime} onChange={(e) => setWeeklyTime(e.target.value)} required />
          </label>
          {weeklyDays.length === 0 && <p className={styles.hint}>{t("pveClusters.scheduleWeeklyPickDay")}</p>}
        </div>
      )}

      {mode === "interval" && (
        <div className={styles.controls}>
          <label className={styles.inlineLabel}>
            {t("pveClusters.scheduleEvery")}
            <input
              type="number"
              className={`input ${styles.narrowInput}`}
              min={1}
              value={intervalValue}
              onChange={(e) => setIntervalValue(e.target.value)}
              required
            />
            <select className={`select ${styles.narrowInput}`} value={intervalUnit} onChange={(e) => setIntervalUnit(e.target.value as IntervalUnit)}>
              <option value="minutes">{t("pveClusters.scheduleUnitMinutes")}</option>
              <option value="hours">{t("pveClusters.scheduleUnitHours")}</option>
            </select>
          </label>
        </div>
      )}

      {mode === "advanced" && (
        <div className={styles.controls}>
          <input
            className="input"
            value={advancedRaw}
            onChange={(e) => setAdvancedRaw(e.target.value)}
            placeholder="02:30"
            required
          />
          <p className={styles.hint}>{t("pveClusters.scheduleAdvancedHint")}</p>
        </div>
      )}

      {mode !== "advanced" && (
        <p className={styles.preview}>
          {t("pveClusters.schedulePreview")} <code>{value || "—"}</code>
        </p>
      )}
    </div>
  );
}
