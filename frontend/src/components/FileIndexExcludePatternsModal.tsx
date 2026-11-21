import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { inventoryApi, pveClustersApi } from "../api/resources";
import type { BackupGroupOut, FileIndexMethod } from "../api/types";
import { InlineAlert } from "./InlineAlert";
import { Modal } from "./Modal";
import styles from "./FileIndexExcludePatternsModal.module.css";

/** Guest-exec File Indexing (ADR 0021) -- VM-only in practice (the
 * backend accepts the field for any backup type, but a CT/host's own
 * catalog dump has no boot/encryption problem `guest_exec_*` exists to
 * solve, so the picker itself only ever offers these for a VM group).
 */
function FileIndexMethodSection({ group, onSaved }: { group: BackupGroupOut; onSaved: (group: BackupGroupOut) => void }) {
  const { t } = useTranslation();
  const [method, setMethod] = useState<FileIndexMethod>(group.file_index_method);
  const [clusterId, setClusterId] = useState(group.guest_exec_live_pve_cluster_id ?? "");
  const [error, setError] = useState<string | null>(null);

  const clustersQuery = useQuery({
    queryKey: ["pve-clusters"],
    queryFn: pveClustersApi.list,
    enabled: method === "guest_exec_live",
  });

  const saveMutation = useMutation({
    mutationFn: () => inventoryApi.setFileIndexMethod(group.id, method, method === "guest_exec_live" ? clusterId : null),
    onSuccess: (updated) => {
      setError(null);
      onSaved(updated);
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  if (group.backup_type !== "vm") {
    // external_mount is the only method that works for a CT/host group
    // at all -- no picker to show, matching the backend's own "VM-only
    // in practice" framing rather than offering options that would just
    // fail.
    return null;
  }

  return (
    <div className={styles.methodSection}>
      {error && (
        <div className={styles.alert}>
          <InlineAlert>{error}</InlineAlert>
        </div>
      )}
      <div className="field">
        <label className="label" htmlFor="file-index-method">
          {t("fileIndex.method")}
        </label>
        <select
          id="file-index-method"
          className="select"
          value={method}
          onChange={(e) => setMethod(e.target.value as FileIndexMethod)}
        >
          <option value="external_mount">{t("fileIndex.methodType.external_mount")}</option>
          <option value="guest_exec_live">{t("fileIndex.methodType.guest_exec_live")}</option>
          <option value="guest_exec_ephemeral">{t("fileIndex.methodType.guest_exec_ephemeral")}</option>
        </select>
        <p className="mutedText">{t(`fileIndex.methodHint.${method}`)}</p>
      </div>
      {method === "guest_exec_live" && (
        <div className="field">
          <label className="label" htmlFor="file-index-method-cluster">
            {t("fileIndex.guestExecCluster")}
          </label>
          <select
            id="file-index-method-cluster"
            className="select"
            value={clusterId}
            onChange={(e) => setClusterId(e.target.value)}
            required
          >
            <option value="">{t("fileIndex.guestExecClusterNotSet")}</option>
            {clustersQuery.data?.map((cluster) => (
              <option key={cluster.id} value={cluster.id}>
                {cluster.display_name}
              </option>
            ))}
          </select>
          <p className="mutedText">{t("fileIndex.guestExecLiveClusterRequired")}</p>
        </div>
      )}
      <div className={styles.formActions}>
        <button
          type="button"
          className="button buttonPrimary"
          onClick={() => saveMutation.mutate()}
          disabled={saveMutation.isPending || (method === "guest_exec_live" && !clusterId)}
        >
          {t("fileIndex.saveMethod")}
        </button>
      </div>
    </div>
  );
}

interface FileIndexExcludePatternsModalProps {
  group: BackupGroupOut;
  onClose: () => void;
  /** The list of groups this modal edits is fetched per-datastore
   * elsewhere (`ProtectedWorkloadsPage`, `FileRecoveryPage`) -- the
   * caller owns invalidating whichever query keys it used to fetch this
   * group, this component only knows how to save. */
  onSaved: (group: BackupGroupOut) => void;
}

function patternsToText(patterns: string[]): string {
  return patterns.join("\n");
}

function textToPatterns(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
}

/** Manifest §13 (ADR 0016) per-VM regex exclusions, raised directly by
 * the user after watching a real VM's index job spend most of its time
 * walking an irrelevant kernel-headers tree. One pattern per line --
 * matched against each discovered entry's path during indexing; a match
 * skips the entry (and, for VM disks, its whole subtree) entirely. The
 * backend rejects an invalid pattern outright rather than silently
 * ignoring it later, so the only client-side validation needed here is
 * surfacing that rejection.
 *
 * A newly synced group already starts with a sensible default list
 * (`BackupGroup.file_index_exclude_patterns`'s server-side default) --
 * this modal doesn't need to apply anything automatically. The "Reset to
 * recommended defaults" button here is only for a group whose list was
 * since cleared or customized: reapplying the default is exactly as
 * intentional an action as any other edit, just pre-filled instead of
 * typed by hand. The list itself is fetched from the backend (the same
 * source of truth the server-side default uses), never duplicated here.
 */
export function FileIndexExcludePatternsModal({ group, onClose, onSaved }: FileIndexExcludePatternsModalProps) {
  const { t } = useTranslation();
  const [text, setText] = useState(patternsToText(group.file_index_exclude_patterns));
  const [error, setError] = useState<string | null>(null);

  const defaultsQuery = useQuery({
    queryKey: ["file-index-default-exclude-patterns"],
    queryFn: inventoryApi.defaultFileIndexExcludePatterns,
  });

  const saveMutation = useMutation({
    mutationFn: () => inventoryApi.setFileIndexExcludePatterns(group.id, textToPatterns(text)),
    onSuccess: (updated) => {
      setError(null);
      onSaved(updated);
      onClose();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  return (
    <Modal open title={t("fileIndex.excludePatternsTitle")} onClose={onClose}>
      <FileIndexMethodSection group={group} onSaved={onSaved} />
      <p className={styles.hint}>{t("fileIndex.excludePatternsHint")}</p>
      {error && (
        <div className={styles.alert}>
          <InlineAlert>{error}</InlineAlert>
        </div>
      )}
      <div className="field">
        <div className={styles.labelRow}>
          <label className="label" htmlFor="file-index-exclude-patterns">
            {t("fileIndex.excludePatternsLabel")}
          </label>
          <button
            type="button"
            className="button buttonGhost"
            disabled={!defaultsQuery.data}
            onClick={() => {
              if (!defaultsQuery.data) return;
              const current = textToPatterns(text);
              const toAdd = defaultsQuery.data.patterns.filter((p) => !current.includes(p));
              setText(patternsToText([...current, ...toAdd]));
            }}
          >
            {t("fileIndex.insertRecommendedDefaults")}
          </button>
        </div>
        <textarea
          id="file-index-exclude-patterns"
          className={`input ${styles.textarea}`}
          rows={8}
          spellCheck={false}
          placeholder={"^/usr/src(/|$)\n(?i)/windows($|/)"}
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
      </div>
      <div className={`${styles.formActions} ${styles.modalFooterActions}`}>
        <button type="button" className="button" onClick={onClose} disabled={saveMutation.isPending}>
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
    </Modal>
  );
}
