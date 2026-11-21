import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { alertMailerApi } from "../api/resources";
import type { AlarmMinSeverity, AlertMailerSettingsOut, SendTestAlertResponse } from "../api/types";
import { useAuth } from "../contexts/AuthContext";
import { InlineAlert } from "./InlineAlert";
import { Panel } from "./Panel";
import { StatusBadge } from "./StatusBadge";

/** "a@x.ch, b@y.ch" / one per line -> ["a@x.ch", "b@y.ch"]. */
export function splitRecipients(value: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const part of value.split(/[,;\s]+/)) {
    const address = part.trim();
    if (address && !seen.has(address.toLowerCase())) {
      seen.add(address.toLowerCase());
      out.push(address);
    }
  }
  return out;
}

const formatTime = (value: string | null) => (value ? new Date(value).toLocaleString() : "—");

/** Per-customer alert mailer: who gets this customer's alarm mails
 * (overdue backups, datastore capacity -- the Dashboard's alarms) and
 * when. The backend sends one summary mail per change (new, escalated,
 * resolved, reminder), never one per alarm or per check. Editable by a
 * platform admin or the customer's own customer admin, same rule as the
 * backend. */
export function AlertMailerPanel({ customerId }: { customerId: string }) {
  const { t } = useTranslation();
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const queryKey = ["alert-mailer", customerId];
  const query = useQuery({ queryKey, queryFn: () => alertMailerApi.get(customerId) });

  const canEdit =
    user?.assignments.some(
      (a) => a.role === "platform_admin" || (a.role === "customer_admin" && a.scope_id === customerId),
    ) ?? false;

  const [editing, setEditing] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [recipientsText, setRecipientsText] = useState("");
  const [minSeverity, setMinSeverity] = useState<AlarmMinSeverity>("warning");
  const [notifyResolved, setNotifyResolved] = useState(true);
  const [reminderHours, setReminderHours] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [testResult, setTestResult] = useState<SendTestAlertResponse | null>(null);

  const startEditing = (data: AlertMailerSettingsOut) => {
    setEnabled(data.enabled);
    setRecipientsText(data.recipients.join("\n"));
    setMinSeverity(data.min_severity);
    setNotifyResolved(data.notify_resolved);
    setReminderHours(data.reminder_hours === null ? "" : String(data.reminder_hours));
    setError(null);
    setSaved(false);
    setTestResult(null);
    setEditing(true);
  };

  const errorText = (err: unknown) => (err instanceof ApiError ? err.message : t("common.loadError"));

  const saveMutation = useMutation({
    mutationFn: () =>
      alertMailerApi.update(customerId, {
        enabled,
        recipients: splitRecipients(recipientsText),
        min_severity: minSeverity,
        notify_resolved: notifyResolved,
        reminder_hours: reminderHours.trim() === "" ? null : Number(reminderHours),
      }),
    onSuccess: (data) => {
      queryClient.setQueryData(queryKey, data);
      queryClient.invalidateQueries({ queryKey: ["notification-settings", customerId] });
      setError(null);
      setEditing(false);
      setSaved(true);
    },
    onError: (err) => setError(errorText(err)),
  });

  const testMutation = useMutation({
    mutationFn: (recipients: string[] | undefined) => alertMailerApi.sendTest(customerId, recipients),
    onSuccess: (data) => {
      setError(null);
      setTestResult(data);
    },
    onError: (err) => {
      setTestResult(null);
      setError(errorText(err));
    },
  });

  const data = query.data;
  const severityLabel = (value: AlarmMinSeverity) =>
    value === "critical" ? t("alertMailer.severityCritical") : t("alertMailer.severityWarning");

  return (
    <Panel
      testId="alert-mailer"
      title={t("alertMailer.title")}
      actions={
        data &&
        canEdit &&
        !editing && (
          <>
            {data.recipients.length > 0 && (
              <button
                type="button"
                className="button"
                onClick={() => testMutation.mutate(undefined)}
                disabled={testMutation.isPending}
              >
                {t("alertMailer.sendTest")}
              </button>
            )}
            <button type="button" className="button buttonPrimary" onClick={() => startEditing(data)}>
              {t("common.edit")}
            </button>
          </>
        )
      }
    >
      <p className="panelIntro">{t("alertMailer.intro")}</p>

      {error && <InlineAlert>{error}</InlineAlert>}
      {saved && !editing && <InlineAlert tone="success">{t("alertMailer.saved")}</InlineAlert>}
      {testResult &&
        (testResult.sent ? (
          <InlineAlert tone="success">
            {t("alertMailer.testSent", { recipients: testResult.recipients.join(", ") })}
          </InlineAlert>
        ) : (
          <InlineAlert>{t("alertMailer.testFailed", { error: testResult.error ?? "" })}</InlineAlert>
        ))}

      {data && !editing && (
        <>
          {data.enabled && data.last_error && (
            <InlineAlert>{t("alertMailer.lastError", { error: data.last_error })}</InlineAlert>
          )}
          <dl className="detailGrid">
            <dt>{t("common.status")}</dt>
            <dd>
              <StatusBadge
                tone={data.enabled ? (data.last_error ? "danger" : "ok") : "unknown"}
                label={data.enabled ? t("common.statusActive") : t("common.statusDisabled")}
              />
            </dd>
            <dt>{t("alertMailer.recipients")}</dt>
            <dd>{data.recipients.length > 0 ? data.recipients.join(", ") : t("customers.notificationNoEmail")}</dd>
            <dt>{t("alertMailer.minSeverity")}</dt>
            <dd>{severityLabel(data.min_severity)}</dd>
            <dt>{t("alertMailer.notifyResolved")}</dt>
            <dd>{data.notify_resolved ? t("common.yes") : t("common.no")}</dd>
            <dt>{t("alertMailer.reminder")}</dt>
            <dd>
              {data.reminder_hours === null
                ? t("alertMailer.reminderOff")
                : t("alertMailer.reminderEvery", { count: data.reminder_hours })}
            </dd>
            <dt>{t("alertMailer.openAlarms")}</dt>
            <dd>
              {t("alertMailer.openAlarmsValue", { open: data.open_alarm_count, notified: data.notified_alarm_count })}
            </dd>
            <dt>{t("alertMailer.lastSent")}</dt>
            <dd>{formatTime(data.last_sent_at)}</dd>
          </dl>
        </>
      )}

      {editing && (
        <>
          <div className="formGrid">
            <div className="field">
              <label className="checkboxField">
                <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />{" "}
                {t("alertMailer.enable")}
              </label>
            </div>
            <div className="field">
              <label className="label" htmlFor={`alert-mailer-recipients-${customerId}`}>
                {t("alertMailer.recipients")}
              </label>
              <textarea
                id={`alert-mailer-recipients-${customerId}`}
                className="input"
                rows={3}
                value={recipientsText}
                placeholder="ops@customer.example"
                onChange={(e) => setRecipientsText(e.target.value)}
              />
              <span className="mutedText">{t("alertMailer.recipientsHint")}</span>
            </div>
            <div className="field">
              <label className="label" htmlFor={`alert-mailer-severity-${customerId}`}>
                {t("alertMailer.minSeverity")}
              </label>
              <select
                id={`alert-mailer-severity-${customerId}`}
                className="select"
                value={minSeverity}
                onChange={(e) => setMinSeverity(e.target.value as AlarmMinSeverity)}
              >
                <option value="warning">{t("alertMailer.severityWarning")}</option>
                <option value="critical">{t("alertMailer.severityCritical")}</option>
              </select>
            </div>
            <div className="field">
              <label className="label" htmlFor={`alert-mailer-reminder-${customerId}`}>
                {t("alertMailer.reminder")}
              </label>
              <input
                id={`alert-mailer-reminder-${customerId}`}
                type="number"
                min={1}
                max={720}
                className="input"
                value={reminderHours}
                placeholder={t("alertMailer.reminderOff")}
                onChange={(e) => setReminderHours(e.target.value)}
              />
              <span className="mutedText">{t("alertMailer.reminderHint")}</span>
            </div>
            <div className="field">
              <label className="checkboxField">
                <input
                  type="checkbox"
                  checked={notifyResolved}
                  onChange={(e) => setNotifyResolved(e.target.checked)}
                />{" "}
                {t("alertMailer.notifyResolved")}
              </label>
            </div>
          </div>
          <div className="formActions">
            <button type="button" className="button" onClick={() => setEditing(false)} disabled={saveMutation.isPending}>
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="button"
              onClick={() => testMutation.mutate(splitRecipients(recipientsText))}
              disabled={testMutation.isPending || splitRecipients(recipientsText).length === 0}
            >
              {t("alertMailer.sendTest")}
            </button>
            <button
              type="button"
              className="button buttonPrimary"
              onClick={() => saveMutation.mutate()}
              disabled={saveMutation.isPending}
            >
              {t("common.save")}
            </button>
          </div>
        </>
      )}
    </Panel>
  );
}
