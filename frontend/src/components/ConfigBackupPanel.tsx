import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { ApiError, downloadBlob } from "../api/client";
import { configBackupApi } from "../api/resources";
import type {
  ConfigBackupDocument,
  ConfigImportAction,
  ConfigImportResult,
  ConfigImportSectionResult,
} from "../api/types";
import { InlineAlert } from "./InlineAlert";
import { Panel } from "./Panel";
import { StatusBadge, type StatusTone } from "./StatusBadge";
import styles from "./ConfigBackupPanel.module.css";

const ACTION_TONE: Record<ConfigImportAction, StatusTone> = {
  create: "ok",
  update: "info",
  skip: "unknown",
  conflict: "danger",
};

/** `resurgam-config-<instance>-<YYYY-MM-DD>.json` -- same shape as the
 * backend's Content-Disposition name, built from the document itself. */
export function configBackupFilename(doc: Pick<ConfigBackupDocument, "exported_at" | "instance_name">): string {
  const instance = (doc.instance_name || "resurgam").replace(/[^A-Za-z0-9_-]/g, "-");
  const date = (doc.exported_at || new Date().toISOString()).slice(0, 10);
  return `resurgam-config-${instance}-${date}.json`;
}

function errorText(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    const details = error.details as { errors?: { field: string; message: string }[] } | undefined;
    const fields = details?.errors?.slice(0, 5).map((e) => `${e.field}: ${e.message}`);
    return fields && fields.length > 0 ? `${error.message} (${fields.join("; ")})` : error.message;
  }
  return fallback;
}

function readFileText(file: File): Promise<string> {
  if (typeof file.text === "function") return file.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(file);
  });
}

function SectionTable({ sections }: { sections: ConfigImportSectionResult[] }) {
  const { t } = useTranslation();
  const visible = sections.filter((s) => s.items.length > 0);
  if (visible.length === 0) return <p className="mutedText">{t("configBackup.summary.empty")}</p>;
  return (
    <table className="table" data-testid="config-import-summary">
      <thead>
        <tr>
          <th>{t("configBackup.summary.section")}</th>
          <th className="end">{t("configBackup.actions.create")}</th>
          <th className="end">{t("configBackup.actions.update")}</th>
          <th className="end">{t("configBackup.actions.skip")}</th>
          <th className="end">{t("configBackup.actions.conflict")}</th>
        </tr>
      </thead>
      <tbody>
        {visible.map((section) => (
          <tr key={section.section} data-testid={`config-section-${section.section}`}>
            <td>
              <details className={styles.details}>
                <summary>{t(`configBackup.sections.${section.section}`, { defaultValue: section.section })}</summary>
                <ul className={styles.itemList}>
                  {section.items.map((item) => (
                    <li key={item.key} className={styles.item}>
                      <StatusBadge tone={ACTION_TONE[item.action]} label={t(`configBackup.actions.${item.action}`)} />
                      <span className="mono">{item.key}</span>
                      {item.reason && <span className="mutedText">{item.reason}</span>}
                    </li>
                  ))}
                </ul>
              </details>
            </td>
            <td className="end">{section.create}</td>
            <td className="end">{section.update}</td>
            <td className="end">{section.skip}</td>
            <td className="end">
              {section.conflict > 0 ? (
                <StatusBadge tone="danger" label={String(section.conflict)} />
              ) : (
                section.conflict
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ReEnterList({ result }: { result: ConfigImportResult }) {
  const { t } = useTranslation();
  if (result.to_reenter.length === 0) return null;
  return (
    <div data-testid="config-reenter">
      <h3 className={styles.subTitle}>
        {result.applied ? t("configBackup.reenter.titleApplied") : t("configBackup.reenter.titleDryRun")}
      </h3>
      <ul className={styles.reenterList}>
        {result.to_reenter.map((item, index) => (
          <li key={`${item.kind}-${item.target}-${index}`} className={styles.reenterItem}>
            <span className={styles.reenterKind}>{t(`configBackup.reenter.kinds.${item.kind}`)}</span>
            <span className="mono">{item.target}</span>
            <span className="mutedText">{item.detail}</span>
            {result.applied && (
              <Link to={item.page} className={styles.reenterLink}>
                {t("configBackup.reenter.open")}
              </Link>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** System Settings -> Configuration backup (ADR 0024): export the
 * configuration (never secrets) as JSON, and import such a file into this
 * instance -- always a dry run first, then an explicit, in-page confirmed
 * apply. Placed as a System Settings tab by the page. */
export function ConfigBackupPanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement | null>(null);

  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  const [fileName, setFileName] = useState<string | null>(null);
  const [importDoc, setImportDoc] = useState<unknown>(null);
  const [checking, setChecking] = useState(false);
  const [dryRun, setDryRun] = useState<ConfigImportResult | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [applying, setApplying] = useState(false);
  const [result, setResult] = useState<ConfigImportResult | null>(null);
  const [importError, setImportError] = useState<string | null>(null);

  const exportConfig = async () => {
    setExporting(true);
    setExportError(null);
    try {
      const doc = await configBackupApi.export();
      downloadBlob(
        new Blob([JSON.stringify(doc, null, 2)], { type: "application/json" }),
        configBackupFilename(doc),
      );
    } catch (error) {
      setExportError(errorText(error, t("configBackup.export.failed")));
    } finally {
      setExporting(false);
    }
  };

  const reset = () => {
    setImportDoc(null);
    setDryRun(null);
    setResult(null);
    setConfirming(false);
    setImportError(null);
  };

  const chooseFile = async (file: File | undefined) => {
    reset();
    setFileName(file?.name ?? null);
    if (!file) return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(await readFileText(file));
    } catch {
      setImportError(t("configBackup.import.notJson"));
      return;
    }
    setImportDoc(parsed);
    setChecking(true);
    try {
      setDryRun(await configBackupApi.import(parsed, true));
    } catch (error) {
      setImportError(errorText(error, t("configBackup.import.failed")));
    } finally {
      setChecking(false);
    }
  };

  const applyImport = async () => {
    setConfirming(false);
    setApplying(true);
    setImportError(null);
    try {
      const applied = await configBackupApi.import(importDoc, false);
      setResult(applied);
      setDryRun(null);
      void queryClient.invalidateQueries();
    } catch (error) {
      setImportError(errorText(error, t("configBackup.import.failed")));
    } finally {
      setApplying(false);
    }
  };

  const startOver = () => {
    reset();
    setFileName(null);
    if (fileInput.current) fileInput.current.value = "";
  };

  const conflicts = dryRun ? dryRun.sections.reduce((sum, s) => sum + s.conflict, 0) : 0;
  const changes = dryRun ? dryRun.sections.reduce((sum, s) => sum + s.create + s.update, 0) : 0;
  const shown = result ?? dryRun;

  return (
    <div className={styles.stack}>
      <Panel title={t("configBackup.export.title")}>
        <p className="panelIntro">{t("configBackup.export.description")}</p>
        {exportError && <InlineAlert>{exportError}</InlineAlert>}
        <div className="formActions">
          <button type="button" className="button" onClick={exportConfig} disabled={exporting}>
            {exporting ? t("configBackup.export.running") : t("configBackup.export.button")}
          </button>
        </div>
      </Panel>

      <Panel title={t("configBackup.import.title")}>
        <p className="panelIntro">{t("configBackup.import.description")}</p>
        <div className="formGrid">
          <label className="field">
            <span className="label">{t("configBackup.import.fileLabel")}</span>
            <input
              ref={fileInput}
              type="file"
              accept="application/json,.json"
              className="input"
              data-testid="config-import-file"
              onChange={(event) => void chooseFile(event.target.files?.[0])}
            />
          </label>
        </div>
        {checking && <p className="mutedText">{t("configBackup.import.checking")}</p>}
        {importError && <InlineAlert>{importError}</InlineAlert>}

        {shown && (
          <div className={styles.result}>
            <p className="mutedText" data-testid="config-import-source">
              {t("configBackup.import.source", {
                file: fileName ?? "",
                instance: shown.source_instance_name ?? "—",
                version: shown.source_resurgam_version,
                date: new Date(shown.source_exported_at).toLocaleString(),
              })}
            </p>
            {result ? (
              <InlineAlert tone="info">{t("configBackup.import.applied")}</InlineAlert>
            ) : (
              <InlineAlert tone="info">
                {conflicts > 0
                  ? t("configBackup.import.dryRunConflicts", { count: conflicts })
                  : t("configBackup.import.dryRunOk", { count: changes })}
              </InlineAlert>
            )}
            <SectionTable sections={shown.sections} />
            <ReEnterList result={shown} />
            {shown.notes.length > 0 && (
              <ul className={styles.notes}>
                {shown.notes.map((note) => (
                  <li key={note} className="mutedText">
                    {note}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {dryRun && !result && !confirming && (
          <div className="formActions">
            <button type="button" className="button" onClick={startOver}>
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="button buttonPrimary"
              onClick={() => setConfirming(true)}
              disabled={applying}
            >
              {t("configBackup.import.button")}
            </button>
          </div>
        )}
        {dryRun && confirming && (
          <div className={styles.confirm} role="group" aria-label={t("configBackup.import.confirmTitle")}>
            <p className={styles.confirmText}>
              <strong>{t("configBackup.import.confirmTitle")}</strong>{" "}
              {t("configBackup.import.confirmText", { count: changes })}
            </p>
            <div className="formActions">
              <button type="button" className="button" onClick={() => setConfirming(false)}>
                {t("common.cancel")}
              </button>
              <button type="button" className="button buttonPrimary" onClick={applyImport} disabled={applying}>
                {applying ? t("configBackup.import.applying") : t("configBackup.import.confirmButton")}
              </button>
            </div>
          </div>
        )}
        {result && (
          <div className="formActions">
            <button type="button" className="button" onClick={startOver}>
              {t("configBackup.import.another")}
            </button>
          </div>
        )}
      </Panel>
    </div>
  );
}
