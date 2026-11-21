import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { formatBytes } from "../lib/formatBytes";
import { DownloadIcon, FileIcon, FolderIcon } from "./icons";
import styles from "./SnapshotEntryList.module.css";

export interface SnapshotEntryRow {
  key: string;
  /** Path inside the snapshot -- what download/archive jobs take. */
  path: string;
  name: string;
  isDirectory: boolean;
  size: number | null;
  /** A muted second piece of text, e.g. the full path of a search hit. */
  secondary?: string;
}

interface SnapshotEntryListProps {
  rows: SnapshotEntryRow[];
  /** Clears the selection whenever it changes (the directory or search shown). */
  selectionKey: string;
  isDownloading: (path: string) => boolean;
  /** Row click: a directory opens, a file downloads (single-file job). */
  onOpen: (row: SnapshotEntryRow) => void;
  /** Folder / multi-selection download as one ZIP. */
  onDownloadZip: (rows: SnapshotEntryRow[]) => void;
  zipDisabled: boolean;
}

/** A snapshot directory listing (or search result) with a checkbox per entry, "select all", a
 * "Download (ZIP)" action for the selection and a ZIP action on every folder row. Shared by the
 * live PBS browser (`FileBrowserPanel`) and the File Index browser (`FileIndexBrowserPanel`), so
 * both download folders and selections the same way. Checkboxes are separate controls from the
 * row button, so selecting never opens or downloads anything. */
export function SnapshotEntryList({
  rows,
  selectionKey,
  isDownloading,
  onOpen,
  onDownloadZip,
  zipDisabled,
}: SnapshotEntryListProps) {
  const { t } = useTranslation();
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const selectAllRef = useRef<HTMLInputElement>(null);

  useEffect(() => setSelected(new Set()), [selectionKey]);

  const selectedRows = rows.filter((row) => selected.has(row.path));
  const allSelected = rows.length > 0 && selectedRows.length === rows.length;

  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = selectedRows.length > 0 && !allSelected;
  }, [selectedRows.length, allSelected]);

  const toggle = (path: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(rows.map((row) => row.path)));
  const downloadSelection = () => {
    if (selectedRows.length === 0) return;
    onDownloadZip(selectedRows);
    setSelected(new Set());
  };

  return (
    <>
      <div className={styles.selectionBar}>
        <label className={styles.selectAll}>
          <input ref={selectAllRef} type="checkbox" checked={allSelected} onChange={toggleAll} />
          {t("fileRestore.selectAll")}
        </label>
        {selectedRows.length > 0 && (
          <span className="mutedText" aria-live="polite">
            {t("fileRestore.selectedCount", { count: selectedRows.length })}
          </span>
        )}
        <span className={styles.selectionActions}>
          {selectedRows.length > 0 && (
            <button type="button" className="button buttonGhost" onClick={() => setSelected(new Set())}>
              {t("fileRestore.clearSelection")}
            </button>
          )}
          <button
            type="button"
            className="button buttonPrimary"
            onClick={downloadSelection}
            disabled={selectedRows.length === 0 || zipDisabled}
          >
            {t("fileRestore.archive.downloadSelection")}
          </button>
        </span>
      </div>
      <ul className={styles.list}>
        {rows.map((row) => {
          const downloading = !row.isDirectory && isDownloading(row.path);
          return (
            <li key={row.key} className={styles.item}>
              <input
                type="checkbox"
                className={styles.check}
                checked={selected.has(row.path)}
                onChange={() => toggle(row.path)}
                aria-label={t("fileRestore.selectEntry", { name: row.name })}
              />
              <button type="button" className={styles.row} onClick={() => onOpen(row)} disabled={downloading}>
                {row.isDirectory ? <FolderIcon className={styles.rowIcon} /> : <FileIcon className={styles.rowIcon} />}
                <span className={styles.rowName}>{row.name}</span>
                {row.secondary && <span className={styles.rowPath}>{row.secondary}</span>}
                {!row.isDirectory && (
                  <span className={styles.rowMeta}>
                    {downloading ? (
                      <span className="mutedText">{t("fileRestore.restoring")}</span>
                    ) : (
                      <>
                        <span className="mutedText">{formatBytes(row.size)}</span>
                        <DownloadIcon className={styles.rowIcon} />
                      </>
                    )}
                  </span>
                )}
              </button>
              {row.isDirectory && (
                <button
                  type="button"
                  className={styles.zipButton}
                  onClick={() => onDownloadZip([row])}
                  disabled={zipDisabled}
                  aria-label={t("fileRestore.archive.downloadFolderLabel", { name: row.name })}
                  title={t("fileRestore.archive.downloadFolderLabel", { name: row.name })}
                >
                  <DownloadIcon className={styles.rowIcon} />
                  {t("fileRestore.archive.zip")}
                </button>
              )}
            </li>
          );
        })}
      </ul>
    </>
  );
}
