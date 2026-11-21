import { useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";

import { ConfirmActionDialog } from "./ConfirmActionDialog";

/** Hours offered in the picker; "custom" allows any whole number up to the
 * backend's limit (one week). */
const PRESET_HOURS = [1, 2, 4, 8, 12, 24, 48, 72];
export const REENABLE_MAX_HOURS = 168;

type Choice = "never" | "custom" | `${number}`;

interface BackupJobDisableDialogProps {
  open: boolean;
  schedule: string | null | undefined;
  /** "disable" (default): disable the job, optionally with a timer.
   * "changeTimer": the job is already disabled with a timer; pick a new
   * duration counted from now. "Never" isn't offered there -- cancelling
   * the timer is its own action. */
  mode?: "disable" | "changeTimer";
  /** `hours` null = disable until someone enables it again (only in "disable" mode). */
  onConfirm: (hours: number | null) => void;
  onCancel: () => void;
}

/** "Disable backup job" confirmation with an optional automatic re-enable
 * ("disable for N hours", ADR 0018 addendum 2026-11-11): RESURGAM enables
 * the job again once the time is up, so a maintenance window can't leave
 * backups switched off by accident. */
export function BackupJobDisableDialog({
  open,
  schedule,
  mode = "disable",
  onConfirm,
  onCancel,
}: BackupJobDisableDialogProps) {
  const { t } = useTranslation();
  const selectId = useId();
  const customId = useId();
  const changing = mode === "changeTimer";
  const initialChoice: Choice = changing ? "24" : "never";
  const [choice, setChoice] = useState<Choice>(initialChoice);
  const [customHours, setCustomHours] = useState("6");

  // Every opening starts from the default ("until further notice" when
  // disabling, 24 hours when changing a timer), never from the previous
  // job's choice.
  useEffect(() => {
    if (open) {
      setChoice(initialChoice);
      setCustomHours("6");
    }
  }, [open, initialChoice]);

  const parsedCustom = Number(customHours);
  const customValid = Number.isInteger(parsedCustom) && parsedCustom >= 1 && parsedCustom <= REENABLE_MAX_HOURS;
  const hours = choice === "never" ? null : choice === "custom" ? (customValid ? parsedCustom : null) : Number(choice);
  const invalid = choice === "custom" && !customValid;

  return (
    <ConfirmActionDialog
      open={open}
      title={changing ? t("backupJobReenable.changeTitle") : t("pveClusters.confirmDisableBackupJobTitle")}
      body={
        changing
          ? t("backupJobReenable.changeBody")
          : t("pveClusters.confirmDisableBackupJobBody", { schedule: schedule ?? "" })
      }
      confirmLabel={changing ? t("common.save") : t("common.disable")}
      danger={!changing}
      confirmDisabled={invalid || (changing && hours === null)}
      onConfirm={() => onConfirm(hours)}
      onCancel={onCancel}
    >
      <div className="field">
        <label className="label" htmlFor={selectId}>
          {changing ? t("backupJobReenable.changeLabel") : t("backupJobReenable.label")}
        </label>
        <select
          id={selectId}
          className="select"
          value={choice}
          onChange={(e) => setChoice(e.target.value as Choice)}
        >
          {!changing && <option value="never">{t("backupJobReenable.never")}</option>}
          {PRESET_HOURS.map((h) => (
            <option key={h} value={String(h)}>
              {t("backupJobReenable.afterHours", { count: h })}
            </option>
          ))}
          <option value="custom">{t("backupJobReenable.custom")}</option>
        </select>
      </div>
      {choice === "custom" && (
        <div className="field">
          <label className="label" htmlFor={customId}>
            {t("backupJobReenable.customHours", { max: REENABLE_MAX_HOURS })}
          </label>
          <input
            id={customId}
            className="input"
            type="number"
            min={1}
            max={REENABLE_MAX_HOURS}
            step={1}
            value={customHours}
            onChange={(e) => setCustomHours(e.target.value)}
            aria-invalid={invalid}
          />
          {invalid && (
            <span className="mutedText" role="alert">
              {t("backupJobReenable.customInvalid", { max: REENABLE_MAX_HOURS })}
            </span>
          )}
        </div>
      )}
      <p className="mutedText">
        {hours === null && !invalid ? t("backupJobReenable.hintNever") : t("backupJobReenable.hintTimer")}
      </p>
    </ConfirmActionDialog>
  );
}
