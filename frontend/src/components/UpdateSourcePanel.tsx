import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { updateSourceApi } from "../api/resources";
import type {
  UpdateSourceDownloadOut,
  UpdateSourceOut,
  UpdateSourceProvider,
  UpdateSourceRelease,
  UpdateSourceTestOut,
} from "../api/types";
import { useBranding } from "../contexts/BrandingContext";
import { formatBytes } from "../lib/formatBytes";
import { ConfirmActionDialog } from "./ConfirmActionDialog";
import { DataTable, type DataTableColumn } from "./DataTable";
import { InlineAlert } from "./InlineAlert";
import { Panel } from "./Panel";
import { StatusBadge } from "./StatusBadge";
import styles from "./UpdatesPanel.module.css";

/** "github"/"gitlab" when the host name says so -- mirrors the backend's
 * `guess_provider`; a neutral self-hosted host keeps the current choice. */
export function guessProvider(url: string): UpdateSourceProvider | null {
  let host = "";
  try {
    host = new URL(url.trim()).host.toLowerCase();
  } catch {
    return null;
  }
  if (host.includes("github")) return "github";
  if (host.includes("gitlab")) return "gitlab";
  return null;
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

function formatTime(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString() : "—";
}

interface UpdateSourcePanelProps {
  /** False outside the appliance: checking works, downloading does not. */
  canDownload: boolean;
  /** Disable downloads while an update is being applied. */
  busy?: boolean;
  /** A downloaded bundle is in incoming/ -- hand it to the verify step. */
  onBundleReady: (download: UpdateSourceDownloadOut) => void;
  pollIntervalMs?: number;
}

/** System Settings → Updates → Update source (ADR 0026): the GitHub/GitLab
 * repository releases are published to, an optional access token, the
 * releases newer than the running version, and downloading one's bundle. */
export function UpdateSourcePanel({ canDownload, busy = false, onBundleReady, pollIntervalMs = 1500 }: UpdateSourcePanelProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { appName } = useBranding();
  const sourceQuery = useQuery({ queryKey: ["update-source"], queryFn: updateSourceApi.get });
  const source = sourceQuery.data;

  const [provider, setProvider] = useState<UpdateSourceProvider>("github");
  const [repositoryUrl, setRepositoryUrl] = useState("");
  const [token, setToken] = useState("");
  const [removeToken, setRemoveToken] = useState(false);
  const [tlsVerify, setTlsVerify] = useState(true);
  const [includePrereleases, setIncludePrereleases] = useState(false);
  const [autoCheck, setAutoCheck] = useState(true);
  const [intervalHours, setIntervalHours] = useState(24);
  const [loadedFrom, setLoadedFrom] = useState<UpdateSourceOut | null>(null);

  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [tokenClearedNotice, setTokenClearedNotice] = useState(false);
  const [editing, setEditing] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<UpdateSourceTestOut | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState<string | null>(null);
  const [download, setDownload] = useState<UpdateSourceDownloadOut | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  // Fill the form once from the server (and again after a save), never
  // while the admin is typing.
  useEffect(() => {
    if (!source || source === loadedFrom) return;
    if (loadedFrom === null || !loadedFrom.configured || saving) {
      setProvider(source.provider);
      setRepositoryUrl(source.repository_url);
      setTlsVerify(source.tls_verify);
      setIncludePrereleases(source.include_prereleases);
      setAutoCheck(source.auto_check);
      setIntervalHours(source.check_interval_hours);
    }
    setLoadedFrom(source);
    if (source.active_download && !download) setDownload(source.active_download);
  }, [source, loadedFrom, saving, download]);

  // Poll a running download until it finishes.
  const onReadyRef = useRef(onBundleReady);
  useEffect(() => {
    onReadyRef.current = onBundleReady;
  }, [onBundleReady]);
  const downloadId = download?.state === "running" ? download.download_id : null;
  useEffect(() => {
    if (!downloadId) return undefined;
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      try {
        const status = await updateSourceApi.download(downloadId);
        if (cancelled) return;
        setDownload(status);
        if (status.state === "succeeded") {
          onReadyRef.current(status);
          return;
        }
        if (status.state === "failed") return;
      } catch (error) {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 404) {
          setDownloadError(error.message);
          return;
        }
      }
      timer = window.setTimeout(tick, pollIntervalMs);
    };
    timer = window.setTimeout(tick, pollIntervalMs);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [downloadId, pollIntervalMs]);

  if (sourceQuery.isLoading) return <p className="mutedText">{t("common.loading")}</p>;
  if (sourceQuery.isError || !source) {
    return <InlineAlert>{errorMessage(sourceQuery.error, t("common.loadError"))}</InlineAlert>;
  }

  const tokenField = (): { token?: string | null } => {
    if (removeToken) return { token: null };
    return token.trim() ? { token: token.trim() } : {};
  };

  const fillForm = (from: UpdateSourceOut) => {
    setProvider(from.provider);
    setRepositoryUrl(from.repository_url);
    setTlsVerify(from.tls_verify);
    setIncludePrereleases(from.include_prereleases);
    setAutoCheck(from.auto_check);
    setIntervalHours(from.check_interval_hours);
    setToken("");
    setRemoveToken(false);
  };

  const startEditing = () => {
    fillForm(source);
    setTestResult(null);
    setFormError(null);
    setSaveMessage(null);
    setTokenClearedNotice(false);
    setEditing(true);
  };

  const cancelEditing = () => {
    fillForm(source);
    setTestResult(null);
    setFormError(null);
    setEditing(false);
  };

  const removeSource = async () => {
    setConfirmRemove(false);
    setRemoving(true);
    setFormError(null);
    setSaveMessage(null);
    try {
      await updateSourceApi.remove();
      setLoadedFrom(null);
      setEditing(false);
      await queryClient.invalidateQueries({ queryKey: ["update-source"] });
      setSaveMessage(t("updateSource.removed"));
    } catch (error) {
      setFormError(errorMessage(error, t("updateSource.errors.requestFailed")));
    } finally {
      setRemoving(false);
    }
  };

  const changeUrl = (value: string) => {
    setRepositoryUrl(value);
    const guessed = guessProvider(value);
    if (guessed) setProvider(guessed);
    setTestResult(null);
  };

  const runTest = async () => {
    setTesting(true);
    setTestResult(null);
    setFormError(null);
    try {
      setTestResult(await updateSourceApi.test({ provider, repository_url: repositoryUrl, tls_verify: tlsVerify, ...tokenField() }));
    } catch (error) {
      setFormError(errorMessage(error, t("updateSource.errors.requestFailed")));
    } finally {
      setTesting(false);
    }
  };

  const save = async () => {
    setSaving(true);
    setSaveMessage(null);
    setTokenClearedNotice(false);
    setFormError(null);
    try {
      const saved = await updateSourceApi.save({
        provider,
        repository_url: repositoryUrl,
        tls_verify: tlsVerify,
        include_prereleases: includePrereleases,
        auto_check: autoCheck,
        check_interval_hours: intervalHours,
        ...tokenField(),
      });
      queryClient.setQueryData(["update-source"], saved);
      setToken("");
      setRemoveToken(false);
      setSaveMessage(t("updateSource.saved"));
      setTokenClearedNotice(Boolean(saved.token_cleared));
      setEditing(false);
    } catch (error) {
      setFormError(errorMessage(error, t("updateSource.errors.requestFailed")));
    } finally {
      setSaving(false);
    }
  };

  const checkNow = async () => {
    setChecking(true);
    setCheckError(null);
    try {
      queryClient.setQueryData(["update-source"], await updateSourceApi.check());
    } catch (error) {
      setCheckError(errorMessage(error, t("updateSource.errors.requestFailed")));
    } finally {
      setChecking(false);
    }
  };

  const startDownload = async (release: UpdateSourceRelease) => {
    setDownloadError(null);
    try {
      setDownload(await updateSourceApi.startDownload(release.version));
    } catch (error) {
      setDownloadError(errorMessage(error, t("updateSource.errors.requestFailed")));
    }
  };

  const downloading = download?.state === "running";
  const releaseColumns: DataTableColumn<UpdateSourceRelease>[] = [
    {
      key: "version",
      header: t("updateSource.releases.version"),
      render: (row) => (
        <div className={styles.releaseVersion}>
          <span className="mono">{row.version}</span>{" "}
          {row.prerelease && <StatusBadge tone="warning" label={t("updateSource.releases.prerelease")} />}
          {row.name && row.name !== row.version && <div className="mutedText">{row.name}</div>}
          <div className="mutedText">{formatTime(row.released_at)}</div>
        </div>
      ),
    },
    {
      key: "notes",
      header: t("updateSource.releases.notes"),
      render: (row) => (
        <>
          {row.notes ? (
            <details>
              <summary>{t("updateSource.releases.showNotes")}</summary>
              <pre className={styles.changelog}>{row.notes}</pre>
            </details>
          ) : (
            <span className="mutedText">{t("updateSource.releases.noNotes")}</span>
          )}
          {row.web_url && (
            <div>
              <a className={styles.releaseLink} href={row.web_url} target="_blank" rel="noreferrer">
                {t("updateSource.releases.openRelease")}
              </a>
            </div>
          )}
        </>
      ),
    },
    {
      key: "actions",
      header: t("common.actions"),
      render: (row) =>
        row.bundle_name ? (
          <div>
            <button
              type="button"
              className="button buttonPrimary"
              disabled={!canDownload || busy || downloading}
              title={!canDownload ? t("updateSource.releases.applianceOnly") : row.bundle_name}
              onClick={() => void startDownload(row)}
            >
              {t("updateSource.releases.download")}
            </button>
            {row.bundle_size !== null && (
              <span className="mutedText" title={row.bundle_name}>
                {formatBytes(row.bundle_size)}
              </span>
            )}
          </div>
        ) : (
          <span className="mutedText" title={t("updateSource.releases.noBundleHint", { version: row.version })}>
            {t("updateSource.releases.noBundle")}
          </span>
        ),
    },
  ];

  const percent =
    download && download.bytes_total ? Math.min(100, Math.round((download.bytes_done / download.bytes_total) * 100)) : null;

  return (
    <>
      {source.configured && (
        <Panel
          title={t("updateSource.releases.title")}
          padded={false}
          testId="update-source-releases"
          actions={
            <button type="button" className="button" disabled={checking} onClick={() => void checkNow()}>
              {checking ? t("updateSource.checking") : t("updateSource.checkNow")}
            </button>
          }
        >
          <div className={styles.empty}>
            <p className={`mutedText ${styles.flush}`}>
              {t("updateSource.lastChecked", { time: formatTime(source.last_checked_at), version: source.current_version })}
            </p>
            {(checkError || source.last_check_error) && (
              <div className={styles.alert}>
                <InlineAlert>{checkError ?? source.last_check_error}</InlineAlert>
              </div>
            )}
            {downloadError && (
              <div className={styles.alert}>
                <InlineAlert>{downloadError}</InlineAlert>
              </div>
            )}
            {download && (
              <div className={styles.alert} data-testid="update-source-download">
                {download.state === "running" && (
                  <div className={styles.progressRow}>
                    <span>{t("updateSource.download.running", { version: download.version })}</span>
                    {percent !== null && <progress max={100} value={percent} aria-label={t("updateSource.download.progress")} />}
                    <span className="mutedText">
                      {formatBytes(download.bytes_done)}
                      {download.bytes_total ? ` / ${formatBytes(download.bytes_total)}` : ""}
                    </span>
                  </div>
                )}
                {download.state === "succeeded" && (
                  <InlineAlert tone="success">{t("updateSource.download.succeeded", { version: download.version })}</InlineAlert>
                )}
                {download.state === "failed" && (
                  <InlineAlert>
                    {t("updateSource.download.failed", { version: download.version })} {download.message}
                  </InlineAlert>
                )}
              </div>
            )}
            {!canDownload && source.available_releases.length > 0 && (
              <div className={styles.alert}>
                <InlineAlert tone="info">{t("updateSource.releases.applianceOnly")}</InlineAlert>
              </div>
            )}
          </div>
          {source.available_releases.length > 0 ? (
            <DataTable columns={releaseColumns} rows={source.available_releases} rowKey={(row) => row.version} />
          ) : (
            <p className={`mutedText ${styles.empty}`} data-testid="update-source-up-to-date">
              {source.last_checked_at && !source.last_check_error
                ? t("updateSource.releases.upToDate", { appName: appName ?? "RESURGAM" })
                : t("updateSource.releases.notChecked")}
            </p>
          )}
        </Panel>
      )}
      <Panel
        title={t("updateSource.title")}
        testId="update-source"
        actions={
          source.configured && !editing ? (
            <span className={styles.inlineActions}>
              <button type="button" className="button" onClick={startEditing}>
                {t("common.edit")}
              </button>
              <button type="button" className="button buttonDanger" disabled={removing} onClick={() => setConfirmRemove(true)}>
                {t("updateSource.remove")}
              </button>
            </span>
          ) : undefined
        }
      >
        {saveMessage && (
          <div className={styles.alert}>
            <InlineAlert tone="success">{saveMessage}</InlineAlert>
          </div>
        )}
        {tokenClearedNotice && (
          <div className={styles.alert}>
            <InlineAlert tone="info">{t("updateSource.tokenCleared")}</InlineAlert>
          </div>
        )}
        {source.configured && !editing ? (
          <>
            {formError && (
              <div className={styles.alert}>
                <InlineAlert>{formError}</InlineAlert>
              </div>
            )}
            <dl className="detailGrid" data-testid="update-source-summary">
              <dt>{t("updateSource.repositoryUrl")}</dt>
              <dd className={`mono ${styles.wrapAnywhere}`}>{source.repository_url}</dd>
              <dt>{t("updateSource.provider")}</dt>
              <dd>{source.provider === "github" ? "GitHub" : "GitLab"}</dd>
              <dt>{t("updateSource.token")}</dt>
              <dd>{source.token_configured ? t("updateSource.summary.tokenStored") : t("updateSource.summary.tokenNone")}</dd>
              <dt>{t("updateSource.summary.autoCheck")}</dt>
              <dd>
                {source.auto_check
                  ? t("updateSource.summary.autoCheckEvery", { count: source.check_interval_hours })
                  : t("updateSource.summary.autoCheckOff")}
              </dd>
              <dt>{t("updateSource.includePrereleases")}</dt>
              <dd>{source.include_prereleases ? t("common.yes") : t("common.no")}</dd>
              {!source.tls_verify && (
                <>
                  <dt>{t("updateSource.tlsVerify")}</dt>
                  <dd>{t("common.no")}</dd>
                </>
              )}
              <dt>{t("updateSource.summary.lastChecked")}</dt>
              <dd>{formatTime(source.last_checked_at)}</dd>
            </dl>
          </>
        ) : (
          <>
            <p className="mutedText">{t("updateSource.description")}</p>
            <div className="formGrid">
              <label className="field">
                <span className="label">{t("updateSource.repositoryUrl")}</span>
                <input
                  className="input"
                  type="url"
                  value={repositoryUrl}
                  placeholder="https://github.com/owner/repository"
                  onChange={(e) => changeUrl(e.target.value)}
                />
              </label>
              <label className="field">
                <span className="label">{t("updateSource.provider")}</span>
                <select className="select" value={provider} onChange={(e) => setProvider(e.target.value as UpdateSourceProvider)}>
                  <option value="github">GitHub</option>
                  <option value="gitlab">GitLab</option>
                </select>
              </label>
              <label className="field">
                <span className="label">{t("updateSource.token")}</span>
                <input
                  className="input"
                  type="password"
                  autoComplete="off"
                  value={token}
                  disabled={removeToken}
                  placeholder={source.token_configured ? t("updateSource.tokenStored") : t("updateSource.tokenNone")}
                  onChange={(e) => setToken(e.target.value)}
                />
                <span className="mutedText">{t(provider === "github" ? "updateSource.tokenHintGithub" : "updateSource.tokenHintGitlab")}</span>
              </label>
              {source.token_configured && (
                <div className="field">
                  <span className="label" aria-hidden="true" />
                  <label className="checkboxField">
                    <input type="checkbox" checked={removeToken} onChange={(e) => setRemoveToken(e.target.checked)} />{" "}
                    {t("updateSource.removeToken")}
                  </label>
                </div>
              )}
              <div className="field">
                <span className="label">{t("updateSource.options")}</span>
                <label className="checkboxField">
                  <input type="checkbox" checked={autoCheck} onChange={(e) => setAutoCheck(e.target.checked)} />{" "}
                  {t("updateSource.autoCheck")}
                </label>
                <label className="checkboxField">
                  <input type="checkbox" checked={includePrereleases} onChange={(e) => setIncludePrereleases(e.target.checked)} />{" "}
                  {t("updateSource.includePrereleases")}
                </label>
                <label className="checkboxField">
                  <input type="checkbox" checked={tlsVerify} onChange={(e) => setTlsVerify(e.target.checked)} />{" "}
                  {t("updateSource.tlsVerify")}
                </label>
              </div>
              <label className="field">
                <span className="label">{t("updateSource.interval")}</span>
                <input
                  className="input"
                  type="number"
                  min={1}
                  max={720}
                  value={intervalHours}
                  disabled={!autoCheck}
                  onChange={(e) => setIntervalHours(Number(e.target.value))}
                />
              </label>
            </div>
            {!tlsVerify && (
              <div className={styles.alert}>
                <InlineAlert tone="info">{t("updateSource.tlsWarning")}</InlineAlert>
              </div>
            )}
            {formError && (
              <div className={styles.alert}>
                <InlineAlert>{formError}</InlineAlert>
              </div>
            )}
            {testResult && (
              <div className={styles.alert} data-testid="update-source-test-result">
                {testResult.ok ? (
                  <InlineAlert tone="success">
                    {t("updateSource.testOk", {
                      repository: testResult.repository,
                      count: testResult.releases_found ?? 0,
                      latest: testResult.latest_release ?? "—",
                    })}
                  </InlineAlert>
                ) : (
                  <InlineAlert>{testResult.error}</InlineAlert>
                )}
                {testResult.stored_token_withheld && (
                  <div className={styles.alert}>
                    <InlineAlert tone="info">{t("updateSource.storedTokenWithheld")}</InlineAlert>
                  </div>
                )}
              </div>
            )}
            <div className="formActions">
              <button type="button" className="button" disabled={testing || !repositoryUrl.trim()} onClick={() => void runTest()}>
                {testing ? t("updateSource.testing") : t("updateSource.test")}
              </button>
              {source.configured && (
                <button type="button" className="button" disabled={saving} onClick={cancelEditing}>
                  {t("common.cancel")}
                </button>
              )}
              <button type="button" className="button buttonPrimary" disabled={saving || !repositoryUrl.trim()} onClick={() => void save()}>
                {t("common.save")}
              </button>
            </div>
          </>
        )}
      </Panel>
      <ConfirmActionDialog
        open={confirmRemove}
        title={t("updateSource.removeConfirmTitle")}
        body={t("updateSource.removeConfirmBody")}
        confirmLabel={t("updateSource.remove")}
        danger
        onConfirm={() => void removeSource()}
        onCancel={() => setConfirmRemove(false)}
      />
    </>
  );
}
