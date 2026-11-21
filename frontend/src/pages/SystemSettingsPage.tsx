import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { brandingApi, instanceSettingsApi, mailSettingsApi } from "../api/resources";
import { ConfirmActionDialog } from "../components/ConfirmActionDialog";
import { InlineAlert } from "../components/InlineAlert";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { QueryState } from "../components/QueryState";
import { ConfigBackupPanel } from "../components/ConfigBackupPanel";
import { TabPanel, Tabs, useTabParam } from "../components/Tabs";
import { UpdatesPanel } from "../components/UpdatesPanel";
import styles from "./SystemSettingsPage.module.css";

const DEFAULT_PRIMARY = "#005f6b";
const DEFAULT_ACCENT = "#b08d57";

/** Part 3 of the branding+reports plan: the RESURGAM-wide floor a
 * customer's own `report_retention_days` can never undercut. Own query/
 * mutation, own edit-mode state -- a separate singleton model
 * (`InstanceSettings`) from branding, deliberately not folded into the
 * same form since they're independent settings that happen to share a
 * page.
 *
 * Also hosts the Collector scheduler's poll interval -- same singleton
 * row, one PUT for both fields -- raised directly by the user after
 * finding the Collector had no scheduler at all (only the manual "Sync"
 * button); the scheduler (`worker/collector_scheduler.py`) re-reads this
 * value every tick, so a change here takes effect without a restart.
 */
function ReportRetentionSection() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [minDays, setMinDays] = useState("365");
  const [pollIntervalMinutes, setPollIntervalMinutes] = useState("15");
  const [error, setError] = useState<string | null>(null);

  const settingsQuery = useQuery({ queryKey: ["instance-settings"], queryFn: instanceSettingsApi.get });

  const startEditing = (data: typeof settingsQuery.data) => {
    if (!data) return;
    setMinDays(String(data.min_report_retention_days));
    setPollIntervalMinutes(String(data.collector_poll_interval_minutes));
    setEditing(true);
  };

  const saveMutation = useMutation({
    mutationFn: () => instanceSettingsApi.update(Number(minDays), Number(pollIntervalMinutes)),
    onSuccess: () => {
      setError(null);
      setEditing(false);
      queryClient.invalidateQueries({ queryKey: ["instance-settings"] });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  return (
    <QueryState query={settingsQuery}>
      {(data) => (
        <Panel
          testId="report-retention-settings"
          title={t("systemSettings.reportRetentionTitle")}
          actions={
            !editing && (
              <button type="button" className="button buttonPrimary" onClick={() => startEditing(data)}>
                {t("common.edit")}
              </button>
            )
          }
        >
          <p className="panelIntro">{t("systemSettings.reportRetentionIntro")}</p>
          {!editing ? (
            <dl className="detailGrid">
              <dt>{t("systemSettings.minReportRetentionDays")}</dt>
              <dd>{data.min_report_retention_days}</dd>
              <dt>{t("systemSettings.collectorPollIntervalMinutes")}</dt>
              <dd>{data.collector_poll_interval_minutes}</dd>
            </dl>
          ) : (
            <>
              {error && (
                <div className={styles.alert}>
                  <InlineAlert>{error}</InlineAlert>
                </div>
              )}
              <div className="formGrid">
                <div className="field">
                  <label className="label" htmlFor="min-report-retention-days">
                    {t("systemSettings.minReportRetentionDays")}
                  </label>
                  <input
                    id="min-report-retention-days"
                    type="number"
                    min={1}
                    className="input"
                    value={minDays}
                    onChange={(e) => setMinDays(e.target.value)}
                  />
                </div>
                <div className="field">
                  <label className="label" htmlFor="collector-poll-interval-minutes">
                    {t("systemSettings.collectorPollIntervalMinutes")}
                  </label>
                  <input
                    id="collector-poll-interval-minutes"
                    type="number"
                    min={1}
                    className="input"
                    value={pollIntervalMinutes}
                    onChange={(e) => setPollIntervalMinutes(e.target.value)}
                  />
                  <p className="mutedText">{t("systemSettings.collectorPollIntervalHint")}</p>
                </div>
              </div>
              <div className="formActions">
                <button type="button" className="button" onClick={() => setEditing(false)} disabled={saveMutation.isPending}>
                  {t("common.cancel")}
                </button>
                <button type="button" className="button buttonPrimary" onClick={() => saveMutation.mutate()} disabled={saveMutation.isPending}>
                  {t("common.save")}
                </button>
              </div>
            </>
          )}
        </Panel>
      )}
    </QueryState>
  );
}

/** Item 1 of the mailer+scheduler build order the user set: the SMTP
 * adapter itself, admin-configurable and testable, before any
 * notification scheduling gets built on top of it. "Simply using MTA as
 * SMTP" (the user's own words) -- host/port/optional-auth/from-address,
 * not a third-party email API. Own query/mutation/edit-mode state, a
 * third independent section on this page.
 */
function MailAdapterSection() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [smtpHost, setSmtpHost] = useState("");
  const [smtpPort, setSmtpPort] = useState("587");
  const [smtpUsername, setSmtpUsername] = useState("");
  const [smtpPassword, setSmtpPassword] = useState("");
  const [useTls, setUseTls] = useState(true);
  const [fromAddress, setFromAddress] = useState("");
  const [fromName, setFromName] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testTo, setTestTo] = useState("");
  const [testResult, setTestResult] = useState<{ sent: boolean; error: string | null } | null>(null);

  const settingsQuery = useQuery({ queryKey: ["mail-settings"], queryFn: mailSettingsApi.get });

  const startEditing = (data: typeof settingsQuery.data) => {
    if (!data) return;
    setSmtpHost(data.smtp_host);
    setSmtpPort(String(data.smtp_port));
    setSmtpUsername(data.smtp_username ?? "");
    setSmtpPassword("");
    setUseTls(data.use_tls);
    setFromAddress(data.from_address);
    setFromName(data.from_name ?? "");
    setEnabled(data.enabled);
    setEditing(true);
  };

  const saveMutation = useMutation({
    mutationFn: () =>
      mailSettingsApi.update({
        smtp_host: smtpHost, smtp_port: Number(smtpPort), smtp_username: smtpUsername.trim() === "" ? null : smtpUsername,
        // Blank password field means "don't change it" -- only include
        // the key at all when the admin actually typed something, so an
        // untouched field never accidentally clears a stored password.
        ...(smtpPassword !== "" ? { smtp_password: smtpPassword } : {}),
        use_tls: useTls, from_address: fromAddress, from_name: fromName.trim() === "" ? null : fromName, enabled,
      }),
    onSuccess: () => {
      setError(null);
      setEditing(false);
      setSmtpPassword("");
      queryClient.invalidateQueries({ queryKey: ["mail-settings"] });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const testMutation = useMutation({
    mutationFn: () => mailSettingsApi.sendTest(testTo),
    onSuccess: (result) => setTestResult(result),
  });

  return (
    <QueryState query={settingsQuery}>
      {(data) => (
        <div className="stack">
          <Panel
            testId="mail-settings"
            title={t("systemSettings.mailTitle")}
            actions={
              !editing && (
                <button type="button" className="button buttonPrimary" onClick={() => startEditing(data)}>
                  {t("common.edit")}
                </button>
              )
            }
          >
            <p className="panelIntro">{t("systemSettings.mailIntro")}</p>
            {!editing ? (
              <dl className="detailGrid">
                <dt>{t("systemSettings.mailStatus")}</dt>
                <dd>{data.enabled ? t("common.statusActive") : t("common.statusDisabled")}</dd>
                <dt>{t("systemSettings.smtpHost")}</dt>
                <dd>{data.smtp_host || t("systemSettings.mailNotConfigured")}</dd>
                {data.smtp_host && (
                  <>
                    <dt>{t("systemSettings.smtpPort")}</dt>
                    <dd>{data.smtp_port}</dd>
                    <dt>{t("systemSettings.fromAddress")}</dt>
                    <dd>{data.from_address}</dd>
                  </>
                )}
              </dl>
            ) : (
              <>
                {error && (
                  <div className={styles.alert}>
                    <InlineAlert>{error}</InlineAlert>
                  </div>
                )}
                <div className="formGrid">
                  <div className="field">
                    <label className="label" htmlFor="smtp-host">
                      {t("systemSettings.smtpHost")}
                    </label>
                    <input id="smtp-host" className="input" value={smtpHost} onChange={(e) => setSmtpHost(e.target.value)} required />
                  </div>
                  <div className="field">
                    <label className="label" htmlFor="smtp-port">
                      {t("systemSettings.smtpPort")}
                    </label>
                    <input id="smtp-port" type="number" min={1} max={65535} className="input" value={smtpPort} onChange={(e) => setSmtpPort(e.target.value)} required />
                  </div>
                  <div className="field">
                    <label className="label" htmlFor="smtp-username">
                      {t("systemSettings.smtpUsername")}
                    </label>
                    <input id="smtp-username" className="input" value={smtpUsername} onChange={(e) => setSmtpUsername(e.target.value)} />
                  </div>
                  <div className="field">
                    <label className="label" htmlFor="smtp-password">
                      {t("systemSettings.smtpPassword")}
                    </label>
                    <input
                      id="smtp-password"
                      type="password"
                      autoComplete="off"
                      className="input"
                      placeholder={data?.smtp_password_configured ? t("common.leaveBlankToKeep") : ""}
                      value={smtpPassword}
                      onChange={(e) => setSmtpPassword(e.target.value)}
                    />
                  </div>
                  <div className="field">
                    <span className="label" aria-hidden="true" />
                    <label className="checkboxField">
                      <input type="checkbox" checked={useTls} onChange={(e) => setUseTls(e.target.checked)} />
                      {" "}
                      {t("systemSettings.useTls")}
                    </label>
                  </div>
                  <div className="field">
                    <label className="label" htmlFor="mail-from-address">
                      {t("systemSettings.fromAddress")}
                    </label>
                    <input id="mail-from-address" type="email" className="input" value={fromAddress} onChange={(e) => setFromAddress(e.target.value)} required />
                  </div>
                  <div className="field">
                    <label className="label" htmlFor="mail-from-name">
                      {t("systemSettings.fromName")}
                    </label>
                    <input id="mail-from-name" className="input" value={fromName} onChange={(e) => setFromName(e.target.value)} />
                  </div>
                  <div className="field">
                    <span className="label">{t("systemSettings.mailStatus")}</span>
                    <label className="checkboxField">
                      <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
                      {" "}
                      {t("systemSettings.mailEnabled")}
                    </label>
                  </div>
                </div>
                <div className="formActions">
                  <button type="button" className="button" onClick={() => setEditing(false)} disabled={saveMutation.isPending}>
                    {t("common.cancel")}
                  </button>
                  <button type="button" className="button buttonPrimary" onClick={() => saveMutation.mutate()} disabled={saveMutation.isPending}>
                    {t("common.save")}
                  </button>
                </div>
              </>
            )}
          </Panel>

          {!editing && data.smtp_host && (
            <Panel title={t("systemSettings.sendTestEmail")} testId="mail-test-email">
              <div className="toolbar">
                <div className="field">
                  <label className="label" htmlFor="mail-test-to">
                    {t("systemSettings.sendTestEmailTo")}
                  </label>
                  <input
                    id="mail-test-to"
                    type="email"
                    className={`input ${styles.testInput}`}
                    value={testTo}
                    onChange={(e) => setTestTo(e.target.value)}
                  />
                </div>
                <button type="button" className="button" onClick={() => testMutation.mutate()} disabled={testMutation.isPending || testTo === ""}>
                  {t("systemSettings.sendTestEmail")}
                </button>
              </div>
              {testResult && (
                <InlineAlert tone={testResult.sent ? "info" : "danger"}>
                  {testResult.sent ? t("systemSettings.testEmailSent") : (testResult.error ?? t("common.loadError"))}
                </InlineAlert>
              )}
            </Panel>
          )}
        </div>
      )}
    </QueryState>
  );
}

/** Administration -> System Settings, first real content for this
 * long-blocked nav placeholder. Hosts the white-label branding section
 * (manifest §6.3 follow-up) and the platform-wide report retention
 * floor (Part 3 of the branding+reports plan), as two separate
 * sections on one page.
 *
 * Mirrors `IntegrationsPage.tsx`'s edit-mode-seeded-on-click pattern: the
 * form is seeded only when the admin clicks Edit, never via an effect
 * reacting to a background refetch, so an in-progress color/logo edit is
 * never silently clobbered.
 */
export function SystemSettingsPage() {
  const { t } = useTranslation();
  const [tab, setTab] = useTabParam(["branding", "reports", "mail", "updates", "backup"]);
  const queryClient = useQueryClient();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [editing, setEditing] = useState(false);
  const [logoFile, setLogoFile] = useState<File | null>(null);
  const [primaryColor, setPrimaryColor] = useState(DEFAULT_PRIMARY);
  const [accentColor, setAccentColor] = useState(DEFAULT_ACCENT);
  const [appName, setAppName] = useState("");
  // "Remove logo" / "Load defaults" in the edit form -- applied on Save.
  const [removeLogo, setRemoveLogo] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const settingsQuery = useQuery({ queryKey: ["branding"], queryFn: brandingApi.get });

  const startEditing = (data: typeof settingsQuery.data) => {
    if (!data) return;
    setLogoFile(null);
    setRemoveLogo(false);
    setPrimaryColor(data.primary_color ?? DEFAULT_PRIMARY);
    setAccentColor(data.accent_color ?? DEFAULT_ACCENT);
    setAppName(data.app_name ?? "");
    setEditing(true);
  };

  /** Fills the form with RESURGAM's own look; nothing changes until Save. */
  const loadDefaults = () => {
    setLogoFile(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
    // Only a logo that exists can be removed (no spurious "logo removed" audit event).
    setRemoveLogo(Boolean(settingsQuery.data?.logo_configured));
    setPrimaryColor(DEFAULT_PRIMARY);
    setAccentColor(DEFAULT_ACCENT);
    setAppName("");
  };

  const saveMutation = useMutation({
    mutationFn: async () => {
      // The form shows exactly the defaults: store "no customization" rather
      // than the default colors as custom values.
      if (
        (removeLogo || !settingsQuery.data?.logo_configured) &&
        !logoFile &&
        primaryColor === DEFAULT_PRIMARY &&
        accentColor === DEFAULT_ACCENT &&
        appName.trim() === ""
      ) {
        return brandingApi.reset();
      }
      if (removeLogo && !logoFile) await brandingApi.removeLogo();
      return brandingApi.update({ logo: logoFile ?? undefined, primaryColor, accentColor, appName });
    },
    onSuccess: () => {
      setError(null);
      setEditing(false);
      setLogoFile(null);
      setRemoveLogo(false);
      queryClient.invalidateQueries({ queryKey: ["branding"] });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const resetMutation = useMutation({
    mutationFn: () => brandingApi.reset(),
    onSuccess: () => {
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["branding"] });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  return (
    <div>
      <PageHeader title={t("systemSettings.title")} description={t("systemSettings.description")} />

      <Tabs
        label={t("systemSettings.title")}
        active={tab}
        onChange={setTab}
        tabs={[
          { id: "branding", label: t("systemSettings.brandingTitle") },
          { id: "reports", label: t("systemSettings.reportRetentionTitle") },
          { id: "mail", label: t("systemSettings.mailTitle") },
          { id: "updates", label: t("systemSettings.updatesTab") },
          { id: "backup", label: t("systemSettings.backupTab") },
        ]}
      />

      {tab === "branding" && (
        <TabPanel id="branding">
          <QueryState query={settingsQuery}>
            {(data) => (
              <Panel
                testId="branding-settings"
                title={t("systemSettings.brandingTitle")}
                actions={
                  !editing && (
                    <>
                      {(data.logo_configured || data.primary_color || data.accent_color || data.app_name) && (
                        <button
                          type="button"
                          className="button"
                          disabled={resetMutation.isPending}
                          onClick={() => setConfirmReset(true)}
                        >
                          {t("systemSettings.restoreDefaults")}
                        </button>
                      )}
                      <button type="button" className="button buttonPrimary" onClick={() => startEditing(data)}>
                        {t("common.edit")}
                      </button>
                    </>
                  )
                }
              >
                <ConfirmActionDialog
                  open={confirmReset}
                  title={t("systemSettings.restoreDefaultsTitle")}
                  body={t("systemSettings.restoreDefaultsBody")}
                  confirmLabel={t("systemSettings.restoreDefaults")}
                  onCancel={() => setConfirmReset(false)}
                  onConfirm={() => {
                    setConfirmReset(false);
                    resetMutation.mutate();
                  }}
                />
                {!editing && error && (
                  <div className={styles.alert}>
                    <InlineAlert>{error}</InlineAlert>
                  </div>
                )}
                {!editing ? (
                  <dl className="detailGrid">
                    <dt>{t("systemSettings.appName")}</dt>
                    <dd>{data.app_name ?? t("systemSettings.appNameDefault")}</dd>
                    <dt>{t("systemSettings.logo")}</dt>
                    <dd>
                      {data.logo_configured ? (
                        <img src={brandingApi.logoUrl()} alt="" className={styles.logoPreview} />
                      ) : (
                        t("systemSettings.logoNotConfigured")
                      )}
                    </dd>
                    <dt>{t("systemSettings.primaryColor")}</dt>
                    <dd>
                      <span className={styles.swatch} style={{ backgroundColor: data.primary_color ?? DEFAULT_PRIMARY }} />
                      {data.primary_color ?? t("systemSettings.usingDefault")}
                    </dd>
                    <dt>{t("systemSettings.accentColor")}</dt>
                    <dd>
                      <span className={styles.swatch} style={{ backgroundColor: data.accent_color ?? DEFAULT_ACCENT }} />
                      {data.accent_color ?? t("systemSettings.usingDefault")}
                    </dd>
                  </dl>
                ) : (
                  <>
                    {error && (
                      <div className={styles.alert}>
                        <InlineAlert>{error}</InlineAlert>
                      </div>
                    )}
                    <div className="formGrid">
                      <div className="field">
                        <label className="label" htmlFor="branding-app-name">
                          {t("systemSettings.appName")}
                        </label>
                        <input
                          id="branding-app-name"
                          className="input"
                          maxLength={60}
                          placeholder="RESURGAM"
                          value={appName}
                          onChange={(e) => setAppName(e.target.value)}
                        />
                        <p className="mutedText">{t("systemSettings.appNameHint")}</p>
                      </div>
                      <div className="field">
                        <label className="label" htmlFor="branding-logo">
                          {t("systemSettings.logo")}
                        </label>
                        <input
                          id="branding-logo"
                          ref={fileInputRef}
                          type="file"
                          accept="image/png,image/jpeg,image/webp"
                          className="input"
                          onChange={(e) => {
                            setLogoFile(e.target.files?.[0] ?? null);
                            if (e.target.files?.[0]) setRemoveLogo(false);
                          }}
                        />
                        <p className="mutedText">{t("systemSettings.logoHint")}</p>
                        {data.logo_configured && !logoFile && (
                          <label className="checkboxField">
                            <input
                              type="checkbox"
                              checked={removeLogo}
                              onChange={(e) => setRemoveLogo(e.target.checked)}
                            />{" "}
                            {t("systemSettings.removeLogo")}
                          </label>
                        )}
                      </div>
                      <div className="field">
                        <label className="label" htmlFor="branding-primary-color">
                          {t("systemSettings.primaryColor")}
                        </label>
                        <span className={styles.colorRow}>
                          <input
                            id="branding-primary-color"
                            type="color"
                            className={styles.colorInput}
                            value={primaryColor}
                            onChange={(e) => setPrimaryColor(e.target.value)}
                          />
                          <span className="mono" data-testid="branding-primary-color-hex">
                            {primaryColor.toUpperCase()}
                          </span>
                        </span>
                      </div>
                      <div className="field">
                        <label className="label" htmlFor="branding-accent-color">
                          {t("systemSettings.accentColor")}
                        </label>
                        <span className={styles.colorRow}>
                          <input
                            id="branding-accent-color"
                            type="color"
                            className={styles.colorInput}
                            value={accentColor}
                            onChange={(e) => setAccentColor(e.target.value)}
                          />
                          <span className="mono" data-testid="branding-accent-color-hex">
                            {accentColor.toUpperCase()}
                          </span>
                        </span>
                      </div>
                    </div>
                    <div className="formActions">
                      <button type="button" className="button" onClick={loadDefaults} disabled={saveMutation.isPending}>
                        {t("systemSettings.loadDefaults")}
                      </button>
                      <button type="button" className="button" onClick={() => setEditing(false)} disabled={saveMutation.isPending}>
                        {t("common.cancel")}
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
            )}
          </QueryState>
        </TabPanel>
      )}

      {tab === "reports" && (
        <TabPanel id="reports">
          <ReportRetentionSection />
        </TabPanel>
      )}

      {tab === "mail" && (
        <TabPanel id="mail">
          <MailAdapterSection />
        </TabPanel>
      )}

      {tab === "updates" && (
        <TabPanel id="updates">
          <UpdatesPanel />
        </TabPanel>
      )}

      {tab === "backup" && (
        <TabPanel id="backup">
          <ConfigBackupPanel />
        </TabPanel>
      )}
    </div>
  );
}
