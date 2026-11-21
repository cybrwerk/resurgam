import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { updateSourceApi, updatesApi } from "../api/resources";
import type {
  UpdateBackupOut,
  UpdateBundleOut,
  UpdateHistoryEntryOut,
  UpdateRequestStatusOut,
  UpdateSourceDownloadOut,
  UpdateState,
} from "../api/types";
import { formatBytes } from "../lib/formatBytes";
import { DataTable, type DataTableColumn } from "./DataTable";
import { InlineAlert } from "./InlineAlert";
import { Panel } from "./Panel";
import { StatusBadge, type StatusTone } from "./StatusBadge";
import { UpdateSourcePanel } from "./UpdateSourcePanel";
import styles from "./UpdatesPanel.module.css";

const TERMINAL_STATES: ReadonlySet<UpdateState> = new Set(["succeeded", "failed", "rolled_back"]);
/** The apply steps in the order the host updater runs them (ADR 0024). */
export const APPLY_STEPS = ["verify", "backup", "stop", "load_images", "install", "host_setup", "migrate", "start", "health", "done"];

const STATE_TONE: Record<UpdateState, StatusTone> = {
  queued: "unknown",
  running: "info",
  succeeded: "ok",
  failed: "danger",
  rolled_back: "warning",
};

/** A request failure that just means "the backend is (re)starting": the
 * updater restarts the backend during an apply, so the status poll fails
 * for a while -- that must read as "restarting", not as an error. */
export function isTransientPollError(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true; // fetch() itself failed (TypeError)
  return error.status === 0 || error.status === 502 || error.status === 503 || error.status === 504;
}

function formatTime(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString() : "—";
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

/** Reads the first tar header block to reject an obviously wrong file
 * before uploading gigabytes (the backend checks again). */
async function looksLikeTar(file: Blob): Promise<boolean> {
  const head = file.slice(0, 512);
  let buffer: ArrayBuffer;
  try {
    buffer =
      typeof head.arrayBuffer === "function"
        ? await head.arrayBuffer()
        : await new Promise<ArrayBuffer>((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result as ArrayBuffer);
            reader.onerror = () => reject(reader.error);
            reader.readAsArrayBuffer(head);
          });
  } catch {
    return true; // can't read it here -- let the backend decide
  }
  const bytes = new Uint8Array(buffer);
  if (bytes.length < 262) return false;
  return String.fromCharCode(...bytes.slice(257, 262)) === "ustar";
}

interface PollState {
  status: UpdateRequestStatusOut | null;
  /** The last poll failed transiently (backend restarting / unreachable). */
  reconnecting: boolean;
  error: string | null;
}

/** Polls one request's status until it is terminal, riding out the
 * backend restart that happens during an apply. */
function useRequestPolling(
  requestId: string | null,
  intervalMs: number,
  onFinished: (status: UpdateRequestStatusOut) => void,
): PollState {
  const [state, setState] = useState<PollState>({ status: null, reconnecting: false, error: null });
  const onFinishedRef = useRef(onFinished);
  useEffect(() => {
    onFinishedRef.current = onFinished;
  }, [onFinished]);

  useEffect(() => {
    setState({ status: null, reconnecting: false, error: null });
    if (!requestId) return undefined;
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      try {
        const status = await updatesApi.requestStatus(requestId);
        if (cancelled) return;
        setState({ status, reconnecting: false, error: null });
        if (TERMINAL_STATES.has(status.state)) {
          onFinishedRef.current(status);
          return;
        }
      } catch (error) {
        if (cancelled) return;
        if (isTransientPollError(error)) {
          setState((previous) => ({ ...previous, reconnecting: true, error: null }));
        } else {
          setState((previous) => ({ ...previous, reconnecting: false, error: errorMessage(error, String(error)) }));
          if (error instanceof ApiError && error.status === 404) return;
        }
      }
      timer = window.setTimeout(tick, intervalMs);
    };
    void tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [requestId, intervalMs]);

  return state;
}

function StateBadge({ state }: { state: string | null | undefined }) {
  const { t } = useTranslation();
  const key = (state ?? "queued") as UpdateState;
  return <StatusBadge tone={STATE_TONE[key] ?? "unknown"} label={t(`updates.state.${key}`, { defaultValue: key })} />;
}

function StepList({ status }: { status: UpdateRequestStatusOut }) {
  const { t } = useTranslation();
  const steps = status.state === "rolled_back" || status.step === "rollback" ? [...APPLY_STEPS.slice(0, -1), "rollback"] : APPLY_STEPS;
  const finished = TERMINAL_STATES.has(status.state);
  const currentIndex = status.step ? steps.indexOf(status.step) : -1;
  return (
    <ol className={styles.steps} aria-label={t("updates.progress.stepsLabel")}>
      {steps.map((step, index) => {
        let tone: StatusTone = "unknown";
        let label = t("updates.progress.pending");
        if (currentIndex >= 0 && index < currentIndex) {
          tone = "ok";
          label = t("updates.progress.stepDone");
        } else if (index === currentIndex) {
          if (finished) {
            tone = status.state === "succeeded" ? "ok" : status.state === "rolled_back" ? "warning" : "danger";
            label = t(`updates.state.${status.state}`);
          } else {
            tone = "info";
            label = t("updates.progress.stepRunning");
          }
        }
        return (
          <li key={step} className={styles.step} data-testid={`update-step-${step}`}>
            <StatusBadge tone={tone} label={label} />
            <span>{t(`updates.steps.${step}`)}</span>
          </li>
        );
      })}
    </ol>
  );
}

function LogLines({ lines }: { lines: string[] }) {
  const { t } = useTranslation();
  if (lines.length === 0) return null;
  return (
    <details className={styles.log} open>
      <summary>{t("updates.progress.log")}</summary>
      <pre className="mono" data-testid="update-log">
        {lines.join("\n")}
      </pre>
    </details>
  );
}

interface UpdatesPanelProps {
  /** Status poll interval; tests shorten it. */
  pollIntervalMs?: number;
}

/** System Settings → Updates (ADR 0024): current version, upload a signed
 * bundle, verify it, install it with live progress, history and the
 * pre-update database backups. */
export function UpdatesPanel({ pollIntervalMs = 2000 }: UpdatesPanelProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);

  const [file, setFile] = useState<File | null>(null);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [bundle, setBundle] = useState<UpdateBundleOut | null>(null);
  const [verifyId, setVerifyId] = useState<string | null>(null);
  const [applyId, setApplyId] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);

  const overviewQuery = useQuery({ queryKey: ["updates-overview"], queryFn: updatesApi.overview });
  // Same query (and cache) as UpdateSourcePanel -- only read here for the
  // "x.y.z available" badge next to the installed version.
  const sourceQuery = useQuery({ queryKey: ["update-source"], queryFn: updateSourceApi.get });
  const latestAvailable = sourceQuery.data?.available_releases[0]?.version ?? null;
  const overview = overviewQuery.data;
  const available = overview?.available === true;
  const bundlesQuery = useQuery({ queryKey: ["updates-bundles"], queryFn: updatesApi.bundles, enabled: available });
  const historyQuery = useQuery({ queryKey: ["updates-history"], queryFn: updatesApi.history, enabled: available });
  const backupsQuery = useQuery({ queryKey: ["updates-backups"], queryFn: updatesApi.backups, enabled: available });

  // Resume showing an apply that is already in progress (page reload, or a
  // second admin opening the page during an update).
  const activeApplyId = overview?.active_apply?.request_id ?? null;
  useEffect(() => {
    if (activeApplyId && !applyId) setApplyId(activeApplyId);
  }, [activeApplyId, applyId]);

  const refreshAll = () => {
    void queryClient.invalidateQueries({ queryKey: ["updates-overview"] });
    void queryClient.invalidateQueries({ queryKey: ["updates-bundles"] });
    void queryClient.invalidateQueries({ queryKey: ["updates-history"] });
    void queryClient.invalidateQueries({ queryKey: ["updates-backups"] });
    void queryClient.invalidateQueries({ queryKey: ["system-version"] });
  };

  const verify = useRequestPolling(verifyId, pollIntervalMs, () => undefined);
  const apply = useRequestPolling(applyId, pollIntervalMs, refreshAll);

  const startVerify = async (target: UpdateBundleOut) => {
    setActionError(null);
    setBundle(target);
    setConfirming(false);
    setApplyId(null);
    try {
      const request = await updatesApi.createRequest("verify", target.name);
      setVerifyId(request.request_id);
    } catch (error) {
      setActionError(errorMessage(error, t("updates.errors.requestFailed")));
    }
  };

  const upload = async () => {
    if (!file || !overview) return;
    setUploadError(null);
    setActionError(null);
    setVerifyId(null);
    setBundle(null);
    if (file.size > overview.max_bundle_bytes) {
      setUploadError(t("updates.upload.tooLarge", { limit: formatBytes(overview.max_bundle_bytes) }));
      return;
    }
    if (!(await looksLikeTar(file))) {
      setUploadError(t("updates.upload.notABundle"));
      return;
    }
    setUploadProgress(0);
    try {
      const uploaded = await updatesApi.uploadBundle(file, (loaded, total) =>
        setUploadProgress(total > 0 ? Math.round((loaded / total) * 100) : 0),
      );
      setUploadProgress(null);
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
      void queryClient.invalidateQueries({ queryKey: ["updates-bundles"] });
      await startVerify(uploaded);
    } catch (error) {
      setUploadProgress(null);
      setUploadError(errorMessage(error, t("updates.upload.failed")));
    }
  };

  const startApply = async () => {
    if (!bundle) return;
    setConfirming(false);
    setActionError(null);
    try {
      const request = await updatesApi.createRequest("apply", bundle.name);
      setApplyId(request.request_id);
    } catch (error) {
      setActionError(errorMessage(error, t("updates.errors.requestFailed")));
    }
  };

  /** A bundle downloaded from the update source (ADR 0026) is in
   * incoming/ -- verify it exactly like an uploaded one. */
  const verifyDownloaded = async (download: UpdateSourceDownloadOut) => {
    if (!download.bundle) return;
    void queryClient.invalidateQueries({ queryKey: ["updates-bundles"] });
    await startVerify({
      name: download.bundle,
      size_bytes: download.bytes_done,
      uploaded_at: download.finished_at ?? new Date().toISOString(),
      version: download.version,
      problem: null,
      signature_prechecked: download.signature_prechecked,
    });
  };

  const deleteBundle = async (name: string) => {
    setDeleteConfirm(null);
    setActionError(null);
    try {
      await updatesApi.deleteBundle(name);
      if (bundle?.name === name) {
        setBundle(null);
        setVerifyId(null);
      }
      void queryClient.invalidateQueries({ queryKey: ["updates-bundles"] });
    } catch (error) {
      setActionError(errorMessage(error, t("updates.errors.requestFailed")));
    }
  };

  if (overviewQuery.isLoading) return <p className="mutedText">{t("common.loading")}</p>;
  if (overviewQuery.isError || !overview) {
    return <InlineAlert>{errorMessage(overviewQuery.error, t("common.loadError"))}</InlineAlert>;
  }

  const verifyStatus = verify.status;
  const verified = verifyStatus?.state === "succeeded";
  const applyStatus = apply.status;
  const applyRunning = applyId !== null && (!applyStatus || !TERMINAL_STATES.has(applyStatus.state));
  const manifest = verifyStatus?.manifest;

  const versionPanel = (
    <Panel title={t("updates.current.title")}>
      <dl className="detailGrid">
        <dt>{t("updates.current.version")}</dt>
        <dd className={styles.versionLine}>
          <span className="mono" data-testid="updates-current-version">
            {overview.current_version}
          </span>
          {latestAvailable && (
            <span data-testid="updates-newer-available">
              <StatusBadge tone="info" label={t("updates.current.newerAvailable", { version: latestAvailable })} />
            </span>
          )}
        </dd>
      </dl>
    </Panel>
  );

  if (!available) {
    return (
      <div className={styles.stack}>
        {versionPanel}
        <InlineAlert tone="info">
          {t("updates.unavailable")} {overview.unavailable_reason}
        </InlineAlert>
        <UpdateSourcePanel canDownload={false} onBundleReady={() => undefined} />
      </div>
    );
  }

  const bundleColumns: DataTableColumn<UpdateBundleOut>[] = [
    {
      key: "name",
      header: t("updates.bundles.file"),
      render: (row) => (
        <>
          <span className="mono">{row.name}</span>
          {row.problem && <div className="mutedText">{row.problem}</div>}
        </>
      ),
    },
    { key: "version", header: t("updates.bundles.version"), render: (row) => row.version ?? "—" },
    { key: "size", header: t("updates.size"), render: (row) => <span title={`${row.size_bytes} B`}>{formatBytes(row.size_bytes)}</span> },
    { key: "uploaded", header: t("updates.bundles.uploaded"), render: (row) => formatTime(row.uploaded_at) },
    {
      key: "actions",
      header: t("common.actions"),
      render: (row) =>
        deleteConfirm === row.name ? (
          <span className={styles.inlineActions}>
            <span>{t("updates.bundles.deleteConfirm")}</span>
            <button type="button" className="button" onClick={() => setDeleteConfirm(null)}>
              {t("common.cancel")}
            </button>
            <button type="button" className="button buttonDanger" onClick={() => void deleteBundle(row.name)}>
              {t("updates.bundles.deleteNow")}
            </button>
          </span>
        ) : (
          <span className={styles.inlineActions}>
            <button type="button" className="button" disabled={applyRunning || !!row.problem} onClick={() => void startVerify(row)}>
              {t("updates.bundles.verify")}
            </button>
            <button type="button" className="button buttonDanger" disabled={applyRunning} onClick={() => setDeleteConfirm(row.name)}>
              {t("common.delete")}
            </button>
          </span>
        ),
    },
  ];

  const historyColumns: DataTableColumn<UpdateHistoryEntryOut>[] = [
    { key: "state", header: t("common.status"), render: (row) => <StateBadge state={row.state} /> },
    {
      key: "versions",
      header: t("updates.history.versions"),
      render: (row) => (
        <span className="mono">
          {row.from_version ?? "?"} → {row.to_version ?? "?"}
        </span>
      ),
    },
    {
      key: "message",
      header: t("updates.history.message"),
      render: (row) => (
        <>
          {row.message ?? "—"}
          {row.requested_by && <div className="mutedText">{row.requested_by}</div>}
        </>
      ),
    },
    { key: "finished", header: t("updates.history.finished"), render: (row) => formatTime(row.finished_at ?? row.started_at) },
  ];

  const backupColumns: DataTableColumn<UpdateBackupOut>[] = [
    { key: "name", header: t("updates.backups.file"), render: (row) => <span className="mono">{row.name}</span> },
    { key: "size", header: t("updates.size"), render: (row) => <span title={`${row.size_bytes} B`}>{formatBytes(row.size_bytes)}</span> },
    { key: "created", header: t("common.createdAt"), render: (row) => formatTime(row.created_at) },
  ];

  return (
    <div className={styles.stack}>
      {versionPanel}

      {applyId && (
        <Panel
          title={t("updates.progress.title", { version: applyStatus?.to_version ?? bundle?.version ?? "" })}
          actions={applyStatus && <StateBadge state={applyStatus.state} />}
          testId="update-progress"
        >
          {apply.reconnecting && (
            <div className={styles.alert}>
              <InlineAlert tone="info">{t("updates.progress.restarting")}</InlineAlert>
            </div>
          )}
          {apply.error && (
            <div className={styles.alert}>
              <InlineAlert>{apply.error}</InlineAlert>
            </div>
          )}
          {applyStatus?.state === "queued" && <p className="mutedText">{t("updates.progress.queued")}</p>}
          {applyStatus && <StepList status={applyStatus} />}
          {applyStatus?.state === "succeeded" && (
            <p data-testid="update-result">
              {t("updates.result.succeeded", { version: applyStatus.to_version ?? "" })}
            </p>
          )}
          {applyStatus?.state === "rolled_back" && (
            <div className={styles.alert} data-testid="update-result">
              <InlineAlert>
                {t("updates.result.rolledBack", { version: applyStatus.from_version ?? "" })} {applyStatus.message}
              </InlineAlert>
            </div>
          )}
          {applyStatus?.state === "failed" && (
            <div className={styles.alert} data-testid="update-result">
              <InlineAlert>
                {t("updates.result.failed")} {applyStatus.message}
              </InlineAlert>
            </div>
          )}
          {applyStatus?.backup_file && (
            <p className="mutedText">
              {t("updates.result.backupFile")} <span className="mono">{applyStatus.backup_file}</span>
            </p>
          )}
          {applyStatus && <LogLines lines={applyStatus.log} />}
        </Panel>
      )}

      <UpdateSourcePanel
        canDownload
        busy={applyRunning}
        onBundleReady={(download) => void verifyDownloaded(download)}
        pollIntervalMs={pollIntervalMs}
      />

      <Panel title={t("updates.upload.title")}>
        <p className="mutedText">{t("updates.upload.description", { limit: formatBytes(overview.max_bundle_bytes) })}</p>
        <div className="formGrid">
          <label className="field">
            <span className="label">{t("updates.upload.file")}</span>
            <input
              ref={fileInput}
              type="file"
              accept=".bundle"
              className="input"
              disabled={uploadProgress !== null || applyRunning}
              onChange={(event) => {
                setFile(event.target.files?.[0] ?? null);
                setUploadError(null);
              }}
            />
          </label>
        </div>
        {uploadProgress !== null && (
          <div className={styles.progressRow}>
            <progress max={100} value={uploadProgress} aria-label={t("updates.upload.progress")} />
            <span className="mutedText">{t("updates.upload.percent", { percent: uploadProgress })}</span>
          </div>
        )}
        {uploadError && (
          <div className={styles.alert}>
            <InlineAlert>{uploadError}</InlineAlert>
          </div>
        )}
        <div className="formActions">
          <button
            type="button"
            className="button"
            disabled={!file || uploadProgress !== null || applyRunning}
            onClick={() => void upload()}
          >
            {t("updates.upload.submit")}
          </button>
        </div>
      </Panel>

      {(verifyId || actionError) && (
        <Panel
          title={t("updates.verify.title")}
          actions={verifyStatus && <StateBadge state={verifyStatus.state} />}
          testId="update-verify"
        >
          {actionError && (
            <div className={styles.alert}>
              <InlineAlert>{actionError}</InlineAlert>
            </div>
          )}
          {verify.reconnecting && (
            <div className={styles.alert}>
              <InlineAlert tone="info">{t("updates.verify.reconnecting")}</InlineAlert>
            </div>
          )}
          {verify.error && (
            <div className={styles.alert}>
              <InlineAlert>{verify.error}</InlineAlert>
            </div>
          )}
          {verifyId && (!verifyStatus || !TERMINAL_STATES.has(verifyStatus.state)) && (
            <p className="mutedText">{t("updates.verify.waiting")}</p>
          )}
          {verifyStatus && TERMINAL_STATES.has(verifyStatus.state) && (
            <>
              <dl className="detailGrid">
                <dt>{t("updates.verify.signature")}</dt>
                <dd data-testid="update-signature">
                  {verified ? (
                    <StatusBadge tone="ok" label={t("updates.verify.signatureOk")} />
                  ) : (
                    <StatusBadge tone="danger" label={t("updates.verify.rejected")} />
                  )}
                </dd>
                <dt>{t("updates.verify.version")}</dt>
                <dd className="mono">{manifest?.version ?? verifyStatus.to_version ?? bundle?.version ?? "—"}</dd>
                <dt>{t("updates.verify.minUpgradeFrom")}</dt>
                <dd className="mono">{manifest?.min_upgrade_from ?? "—"}</dd>
              </dl>
              {!verified && (
                <div className={styles.alert} data-testid="update-reject-reason">
                  <InlineAlert>
                    {t("updates.verify.rejectedReason")} {verifyStatus.message ?? ""}
                  </InlineAlert>
                </div>
              )}
              {manifest?.changelog && (
                <>
                  <h3 className={styles.subTitle}>{t("updates.verify.changelog")}</h3>
                  <pre className={styles.changelog} data-testid="update-changelog">
                    {manifest.changelog}
                  </pre>
                </>
              )}
              {verified && !applyId && (
                <div className="formActions">
                  {confirming ? (
                    <div className={styles.confirmBox} role="group" aria-label={t("updates.apply.confirmTitle")}>
                      <p>
                        {t("updates.apply.confirmText", {
                          from: overview.current_version,
                          to: manifest?.version ?? bundle?.version ?? "",
                        })}
                      </p>
                      <div className={styles.inlineActions}>
                        <button type="button" className="button" onClick={() => setConfirming(false)}>
                          {t("common.cancel")}
                        </button>
                        <button type="button" className="button buttonPrimary" onClick={() => void startApply()}>
                          {t("updates.apply.confirmButton", { version: manifest?.version ?? bundle?.version ?? "" })}
                        </button>
                      </div>
                    </div>
                  ) : (
                    <button type="button" className="button buttonPrimary" onClick={() => setConfirming(true)}>
                      {t("updates.apply.button")}
                    </button>
                  )}
                </div>
              )}
              <LogLines lines={verifyStatus.log} />
            </>
          )}
        </Panel>
      )}

      <Panel title={t("updates.bundles.title")} padded={false}>
        {bundlesQuery.data && bundlesQuery.data.length > 0 ? (
          <DataTable columns={bundleColumns} rows={bundlesQuery.data} rowKey={(row) => row.name} />
        ) : (
          <p className={`mutedText ${styles.empty}`}>{t("updates.bundles.empty")}</p>
        )}
      </Panel>

      <Panel title={t("updates.history.title")} padded={false}>
        {historyQuery.data && historyQuery.data.length > 0 ? (
          <DataTable
            columns={historyColumns}
            rows={historyQuery.data}
            rowKey={(row) => `${row.request_id ?? ""}-${row.finished_at ?? row.started_at ?? ""}`}
          />
        ) : (
          <p className={`mutedText ${styles.empty}`}>{t("updates.history.empty")}</p>
        )}
      </Panel>

      <Panel title={t("updates.backups.title")} padded={false}>
        <p className={`mutedText ${styles.empty}`}>{t("updates.backups.description")}</p>
        {backupsQuery.data && backupsQuery.data.length > 0 ? (
          <DataTable columns={backupColumns} rows={backupsQuery.data} rowKey={(row) => row.name} />
        ) : (
          <p className={`mutedText ${styles.empty}`}>{t("updates.backups.empty")}</p>
        )}
      </Panel>
    </div>
  );
}
