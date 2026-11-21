import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { restorePlansApi } from "../api/resources";
import type { RestorePlanExecutionStatus, RestorePlanOut } from "../api/types";
import { formatBytes } from "../lib/formatBytes";
import { ConfirmActionDialog } from "./ConfirmActionDialog";
import { InlineAlert } from "./InlineAlert";
import { StatusBadge, type StatusTone } from "./StatusBadge";
import styles from "./InstantRecoveryPanel.module.css";

/** A heartbeat older than this means the agent is not reporting (the VM may still run). */
const HEARTBEAT_STALE_MS = 5 * 60 * 1000;

export const INSTANT_RECOVERY_TERMINAL: RestorePlanExecutionStatus[] = ["completed", "discarded", "failed"];

export function instantRecoveryTone(status: RestorePlanExecutionStatus | null): StatusTone {
  switch (status) {
    case "running_from_backup":
    case "moving_disks":
      return "warning";
    case "completed":
      return "ok";
    case "failed":
      return "danger";
    case "discarded":
      return "unknown";
    default:
      return "info";
  }
}

interface InstantRecoveryPanelProps {
  plan: RestorePlanOut;
  /** Called with the updated plan after Move / Discard. */
  onChange?: (plan: RestorePlanOut) => void;
}

/** A VM running straight from its PBS backup (ADR 0030): how long, how much it
 * wrote, how full the overlay storage is, and the two ways out -- Move to
 * storage (live) or Discard. */
export function InstantRecoveryPanel({ plan, onChange }: InstantRecoveryPanelProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [bwlimitMib, setBwlimitMib] = useState("");
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const state = plan.instant_recovery;
  const status = plan.execution_status;

  const done = (updated: RestorePlanOut) => {
    onChange?.(updated);
    queryClient.invalidateQueries({ queryKey: ["restore-plans"] });
  };
  const moveMutation = useMutation({
    mutationFn: () => {
      const mib = Number(bwlimitMib);
      return restorePlansApi.moveInstantRecovery(plan.id, bwlimitMib.trim() !== "" && mib > 0 ? Math.round(mib * 1024) : null);
    },
    onSuccess: done,
  });
  const discardMutation = useMutation({
    mutationFn: () => restorePlansApi.discardInstantRecovery(plan.id),
    onSuccess: (updated) => {
      setConfirmDiscard(false);
      done(updated);
    },
  });

  if (!state) return null;

  const storageUsedPercent =
    state.overlay_storage_total_bytes && state.overlay_storage_free_bytes !== null
      ? Math.round(
          ((state.overlay_storage_total_bytes - state.overlay_storage_free_bytes) / state.overlay_storage_total_bytes) * 100,
        )
      : null;
  const runningFromBackup = status === "running_from_backup" || status === "moving_disks";
  const heartbeatStale =
    runningFromBackup &&
    state.last_heartbeat_at !== null &&
    Date.now() - new Date(state.last_heartbeat_at).getTime() > HEARTBEAT_STALE_MS;
  const leftovers = state.disks.length > 0 || state.imported_volumes.length > 0;
  const canDiscard = status === "running_from_backup" || (status === "failed" && leftovers);
  const actionError = moveMutation.error ?? discardMutation.error;

  return (
    <div className={styles.panel} data-testid="instant-recovery-panel">
      <div className={styles.headline}>
        <StatusBadge tone={instantRecoveryTone(status)} label={t(`instantRecovery.status_${status ?? "none"}`)} />
        <span className={styles.vm}>
          {plan.temp_name} · VM {plan.target_vmid} · {plan.target_node}
        </span>
      </div>

      {runningFromBackup && <InlineAlert tone="info">{t("instantRecovery.riskNote")}</InlineAlert>}
      {heartbeatStale && <InlineAlert tone="danger">{t("instantRecovery.agentSilent")}</InlineAlert>}

      <dl className="detailGrid">
        {state.running_since && (
          <>
            <dt>{t("instantRecovery.runningSince")}</dt>
            <dd>{new Date(state.running_since).toLocaleString()}</dd>
          </>
        )}
        <dt>{t("instantRecovery.disks")}</dt>
        <dd>{state.disks.length > 0 ? state.disks.join(", ") : "—"}</dd>
        {state.overlay_bytes !== null && (
          <>
            <dt>{t("instantRecovery.written")}</dt>
            <dd>{formatBytes(state.overlay_bytes)}</dd>
          </>
        )}
        {storageUsedPercent !== null && (
          <>
            <dt>{t("instantRecovery.overlayStorage")}</dt>
            <dd>
              <StatusBadge
                tone={storageUsedPercent >= 80 ? "danger" : storageUsedPercent >= 60 ? "warning" : "ok"}
                label={t("instantRecovery.overlayStorageValue", {
                  percent: storageUsedPercent,
                  free: formatBytes(state.overlay_storage_free_bytes),
                })}
              />
            </dd>
          </>
        )}
        <dt>{t("instantRecovery.macAddresses")}</dt>
        <dd>{state.keep_mac ? t("instantRecovery.macKept") : t("instantRecovery.macNew")}</dd>
        {state.original_vm && (
          <>
            <dt>{t("instantRecovery.originalVm")}</dt>
            <dd>
              {t(`instantRecovery.original_${state.original_vm.status}`)}
              {state.original_vm.cluster_name ? ` (${state.original_vm.cluster_name} / ${state.original_vm.node})` : ""}
            </dd>
          </>
        )}
      </dl>

      {state.skipped_devices.length > 0 && (
        <InlineAlert tone="info">
          {t("instantRecovery.skippedDevices", { devices: state.skipped_devices.join(", ") })}
        </InlineAlert>
      )}

      {state.move_disks.length > 0 && (
        <ul className={styles.moveList} aria-label={t("instantRecovery.moveProgress")}>
          {state.move_disks.map((disk) => (
            <li key={disk.slot}>
              <StatusBadge
                tone={disk.status === "done" ? "ok" : disk.status === "failed" ? "danger" : disk.status === "running" ? "info" : "unknown"}
                label={`${disk.slot}: ${t(`instantRecovery.move_${disk.status}`)}`}
              />
              {disk.error && <span className="mutedText"> {disk.error}</span>}
            </li>
          ))}
        </ul>
      )}

      {(plan.execution_error || state.last_error) && (
        <InlineAlert tone="danger">{plan.execution_error ?? state.last_error}</InlineAlert>
      )}

      {status === "running_from_backup" && (
        <div className={styles.actions}>
          <div className="field">
            <label className="label" htmlFor={`ir-bwlimit-${plan.id}`}>
              {t("instantRecovery.bwlimit")}
            </label>
            <input
              id={`ir-bwlimit-${plan.id}`}
              type="number"
              min={1}
              className="input"
              value={bwlimitMib}
              placeholder={t("instantRecovery.bwlimitPlaceholder")}
              onChange={(e) => setBwlimitMib(e.target.value)}
            />
          </div>
          <button
            type="button"
            className="button buttonPrimary"
            disabled={moveMutation.isPending}
            onClick={() => moveMutation.mutate()}
          >
            {t("instantRecovery.move", { storage: plan.target_storage })}
          </button>
        </div>
      )}
      {canDiscard && (
        <div className={styles.actions}>
          <button
            type="button"
            className="button buttonDanger"
            disabled={discardMutation.isPending}
            onClick={() => setConfirmDiscard(true)}
          >
            {t("instantRecovery.discard")}
          </button>
        </div>
      )}
      {actionError && (
        <InlineAlert tone="danger">{actionError instanceof ApiError ? actionError.message : t("common.loadError")}</InlineAlert>
      )}

      <ConfirmActionDialog
        open={confirmDiscard}
        danger
        title={t("instantRecovery.discardTitle", { name: plan.temp_name })}
        body={t("instantRecovery.discardBody", { vmid: plan.target_vmid })}
        confirmLabel={t("instantRecovery.discard")}
        onConfirm={() => discardMutation.mutate()}
        onCancel={() => setConfirmDiscard(false)}
      />
    </div>
  );
}
