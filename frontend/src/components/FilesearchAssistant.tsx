import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { fileIndexApi } from "../api/resources";
import type { BackupSnapshotOut, FileIndexEntryOut } from "../api/types";
import { formatBytes } from "../lib/formatBytes";
import { FileIcon, SearchIcon } from "./icons";
import styles from "./FilesearchAssistant.module.css";

function parentDirOf(path: string): string {
  const lastSlash = path.lastIndexOf("/");
  return lastSlash <= 0 ? "/" : path.slice(0, lastSlash);
}

interface FilesearchAssistantProps {
  customerId: string;
  groupId: string;
  snapshots: BackupSnapshotOut[];
  onResultSelect: (snapshotId: string, path: string) => void;
}

/** A prominent, group-wide filename search, docked above the content
 * browser -- raised directly by the user ("Filesearch-Assistent") as a
 * faster way to find a file than browsing snapshot by snapshot. Not a
 * natural-language assistant (that stays a parked, later-release idea) --
 * this is a strong search UI on top of the *existing* group-wide,
 * cross-snapshot `/file-index/search` endpoint. Scoping to "indexed
 * (green) snapshots" needs no explicit filtering here: the endpoint only
 * ever queries `FileIndexEntry` rows, and a not-yet-indexed (yellow)
 * snapshot has none by construction. Deliberately separate from
 * `FileIndexBrowserPanel`'s own "search from here" box, which is a
 * different, narrower feature (recursive under the currently-browsed
 * directory of one snapshot).
 */
export function FilesearchAssistant({ customerId, groupId, snapshots, onResultSelect }: FilesearchAssistantProps) {
  const { t } = useTranslation();
  const [input, setInput] = useState("");
  const [term, setTerm] = useState("");
  const isSearching = term.trim() !== "";

  const searchQuery = useQuery({
    queryKey: ["filesearch-assistant", customerId, groupId, term],
    queryFn: () => fileIndexApi.search({ customerId, backupGroupId: groupId, filename: term, limit: 50 }),
    enabled: isSearching,
  });

  const snapshotTime = (entry: FileIndexEntryOut) =>
    snapshots.find((s) => s.id === entry.backup_snapshot_id)?.snapshot_time ?? null;

  return (
    <div className={styles.assistant}>
      <div className={styles.searchRow}>
        <SearchIcon className={styles.searchIcon} />
        <input
          type="text"
          className={`input ${styles.searchInput}`}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") setTerm(input.trim());
          }}
          onBlur={() => setTerm(input.trim())}
          placeholder={t("fileWorkspace.assistantPlaceholder")}
          aria-label={t("fileWorkspace.assistantPlaceholder")}
        />
        {isSearching && (
          <button
            type="button"
            className="button buttonGhost"
            onClick={() => {
              setInput("");
              setTerm("");
            }}
          >
            {t("fileIndex.clearSearch")}
          </button>
        )}
      </div>

      {isSearching && (
        <div className={styles.results}>
          {searchQuery.isLoading && <p className="mutedText">{t("common.loading")}</p>}
          {searchQuery.isError && <p className="mutedText">{t("common.loadError")}</p>}
          {searchQuery.isSuccess && searchQuery.data.items.length === 0 && (
            <p className="mutedText">{t("fileWorkspace.assistantNoResults")}</p>
          )}
          {searchQuery.isSuccess && searchQuery.data.items.length > 0 && (
            <ul className={styles.resultList}>
              {searchQuery.data.items.map((entry) => {
                const time = snapshotTime(entry);
                return (
                  <li key={entry.id}>
                    <button
                      type="button"
                      className={styles.resultRow}
                      onClick={() => onResultSelect(entry.backup_snapshot_id, parentDirOf(entry.path))}
                    >
                      <FileIcon className={styles.resultIcon} />
                      <span className={styles.resultPath}>{entry.path}</span>
                      <span className="mutedText">{formatBytes(entry.size_bytes)}</span>
                      {time && <span className="mutedText">{new Date(time).toLocaleString()}</span>}
                    </button>
                  </li>
                );
              })}
              {searchQuery.data.has_more && <li className="mutedText">{t("fileIndex.searchTruncated")}</li>}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
