import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { pveClustersApi, restorePlansApi } from "../api/resources";
import type {
  BackupGroupOut,
  RestorePlanCheckEntry,
  RestorePlanExecutionStatus,
  RestorePlanMethod,
  RestorePlanNetworkMode,
  RestorePlanOut,
  RestorePlanVmidIntent,
} from "../api/types";
import { useAuth } from "../contexts/AuthContext";
import { InlineAlert } from "../components/InlineAlert";
import {
  INSTANT_RECOVERY_TERMINAL,
  InstantRecoveryPanel,
} from "../components/InstantRecoveryPanel";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { SnapshotPicker } from "../components/SnapshotPicker";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { Wizard, type WizardStep } from "../components/Wizard";
import styles from "./RestoreCenterPage.module.css";

const VMID_DEBOUNCE_MS = 400;

function checkTone(status: RestorePlanCheckEntry["status"]): StatusTone {
  switch (status) {
    case "pass":
      return "ok";
    case "warn":
      return "warning";
    case "fail":
      return "danger";
    default:
      return "unknown";
  }
}

/** Manifest EPIC 8/§11.1 "MVP: geführter Restore" — a 5-step wizard that
 * ends in a reviewed, human-runnable qmrestore/pct restore command.
 * Nothing here executes a restore; see restore/command.py's docstring
 * and ADR 0012. Only supports vm/ct snapshots (not host, which restores
 * through a different mechanism entirely, unrelated to PVE VMIDs).
 */
export function RestoreCenterPage() {
  const { t } = useTranslation();

  const [currentStep, setCurrentStep] = useState(0);

  // Step 1: snapshot
  const [customerId, setCustomerId] = useState("");
  const [groupId, setGroupId] = useState("");
  const [snapshotId, setSnapshotId] = useState("");

  // Step 2: restore target (and method: full restore or Instant Recovery, ADR 0030)
  const [method, setMethod] = useState<RestorePlanMethod>("full");
  const [pveClusterId, setPveClusterId] = useState("");
  const [targetNode, setTargetNode] = useState("");
  const [targetStorage, setTargetStorage] = useState("");
  const [vmidIntent, setVmidIntent] = useState<RestorePlanVmidIntent>("new");
  const [targetVmid, setTargetVmid] = useState("");
  const [debouncedVmid, setDebouncedVmid] = useState("");

  // Step 3: network isolation
  const [networkMode, setNetworkMode] = useState<RestorePlanNetworkMode>("none");
  const [networkBridge, setNetworkBridge] = useState("");

  // Step 4: restore options
  const [tempName, setTempName] = useState("");
  const [startAfterRestore, setStartAfterRestore] = useState(false);

  // Step 5: review & confirm
  const [networkDangerAcknowledged, setNetworkDangerAcknowledged] = useState(false);
  const [createdPlan, setCreatedPlan] = useState<RestorePlanOut | null>(null);
  const [copyFeedback, setCopyFeedback] = useState(false);

  // EPIC 9 (manifest §11.2): four-eyes approval + PVE-API execution, once
  // a plan exists.
  const { user: currentUser } = useAuth();
  const [rejectReason, setRejectReason] = useState("");
  const [showRejectInput, setShowRejectInput] = useState(false);
  const [overwriteConfirmVmid, setOverwriteConfirmVmid] = useState("");

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedVmid(targetVmid), VMID_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [targetVmid]);

  const pveClustersQuery = useQuery({ queryKey: ["pve-clusters"], queryFn: pveClustersApi.list });
  const nodesQuery = useQuery({
    queryKey: ["pve-nodes", pveClusterId],
    queryFn: () => pveClustersApi.nodes(pveClusterId),
    enabled: pveClusterId !== "",
  });
  const storagesQuery = useQuery({
    queryKey: ["pve-storages", pveClusterId, targetNode],
    queryFn: () => pveClustersApi.storages(pveClusterId, targetNode),
    enabled: pveClusterId !== "" && targetNode !== "",
  });
  const vmidCheckQuery = useQuery({
    queryKey: ["pve-vmid-check", pveClusterId, debouncedVmid],
    queryFn: () => pveClustersApi.checkVmid(pveClusterId, Number(debouncedVmid)),
    enabled: pveClusterId !== "" && debouncedVmid !== "" && Number(debouncedVmid) > 0,
  });

  const selectedCluster = pveClustersQuery.data?.find((c) => c.id === pveClusterId);
  const instant = method === "instant";
  const activeClusters =
    pveClustersQuery.data?.filter((c) => !c.disabled && (!instant || c.instant_recovery_enabled)) ?? [];

  // Every Instant Recovery that still has something on a node -- an operator
  // must find a VM running from its backup again after leaving the wizard.
  const instantPlansQuery = useQuery({
    queryKey: ["restore-plans"],
    queryFn: restorePlansApi.list,
    refetchInterval: 10000,
  });
  const openInstantPlans = (instantPlansQuery.data ?? []).filter(
    (p) =>
      p.method === "instant" &&
      p.execution_status !== null &&
      (!INSTANT_RECOVERY_TERMINAL.includes(p.execution_status) ||
        (p.execution_status === "failed" &&
          ((p.instant_recovery?.disks.length ?? 0) > 0 || (p.instant_recovery?.imported_volumes.length ?? 0) > 0))),
  );

  const createMutation = useMutation({
    mutationFn: () =>
      restorePlansApi.create({
        backup_snapshot_id: snapshotId,
        pve_cluster_id: pveClusterId,
        target_node: targetNode,
        target_storage: targetStorage,
        vmid_intent: vmidIntent,
        target_vmid: Number(targetVmid),
        temp_name: tempName || null,
        network_mode: networkMode,
        network_bridge: networkMode === "custom" ? networkBridge : null,
        start_after_restore: instant ? false : startAfterRestore,
        network_danger_acknowledged: networkDangerAcknowledged,
        method,
      }),
    onSuccess: (plan) => setCreatedPlan(plan),
  });

  const approveMutation = useMutation({
    mutationFn: () => restorePlansApi.approve(createdPlan!.id),
    onSuccess: setCreatedPlan,
  });
  const rejectMutation = useMutation({
    mutationFn: () => restorePlansApi.reject(createdPlan!.id, rejectReason),
    onSuccess: (plan) => {
      setCreatedPlan(plan);
      setShowRejectInput(false);
    },
  });
  const executeMutation = useMutation({
    mutationFn: () => restorePlansApi.execute(createdPlan!.id),
    onSuccess: setCreatedPlan,
  });

  const executionInFlight = (status: RestorePlanExecutionStatus | null) =>
    status !== null &&
    !["completed", "restore_failed", "finalize_failed", ...INSTANT_RECOVERY_TERMINAL].includes(status);

  const pollExecutionQuery = useQuery({
    queryKey: ["restore-plan-execution", createdPlan?.id],
    queryFn: () => restorePlansApi.pollExecution(createdPlan!.id),
    enabled: createdPlan !== null && executionInFlight(createdPlan.execution_status),
    refetchInterval: (query) => (executionInFlight(query.state.data?.execution_status ?? null) ? 2000 : false),
  });

  useEffect(() => {
    if (pollExecutionQuery.data) setCreatedPlan(pollExecutionQuery.data);
  }, [pollExecutionQuery.data]);

  const isPlanCreator = createdPlan !== null && createdPlan.created_by_user_id === currentUser?.user_id;
  const overwriteConfirmSatisfied = vmidIntent !== "existing" || overwriteConfirmVmid === targetVmid;
  const executeAllowed =
    createdPlan !== null &&
    createdPlan.execution_status === null &&
    (createdPlan.approval_status === null || createdPlan.approval_status === "approved") &&
    overwriteConfirmSatisfied;

  const steps: WizardStep[] = [
    { id: "snapshot", title: t("restoreCenter.stepSnapshot") },
    { id: "target", title: t("restoreCenter.stepTarget") },
    { id: "network", title: t("restoreCenter.stepNetwork") },
    { id: "options", title: t("restoreCenter.stepOptions") },
    { id: "review", title: t("restoreCenter.stepReview") },
  ];

  const canAdvance = (() => {
    switch (currentStep) {
      case 0:
        return snapshotId !== "";
      case 1:
        return pveClusterId !== "" && targetNode !== "" && targetStorage !== "" && Number(targetVmid) > 0;
      case 2:
        return networkMode !== "custom" || networkBridge.trim() !== "";
      case 3:
        return true;
      case 4: {
        if (createdPlan) return false; // once created, the wizard is done
        const networkRiskFlagged = networkMode === "original" || networkMode === "custom" || networkMode === "isolated";
        return !networkRiskFlagged || networkDangerAcknowledged;
      }
      default:
        return false;
    }
  })();

  const handleNext = () => {
    if (currentStep < steps.length - 1) {
      setCurrentStep((s) => s + 1);
      return;
    }
    createMutation.mutate();
  };

  const handleStepClick = (index: number) => {
    if (createdPlan) return; // wizard is finished, no more editing
    setCurrentStep(index);
  };

  const restartWizard = () => {
    setCurrentStep(0);
    setMethod("full");
    setCustomerId("");
    setGroupId("");
    setSnapshotId("");
    setPveClusterId("");
    setTargetNode("");
    setTargetStorage("");
    setVmidIntent("new");
    setTargetVmid("");
    setNetworkMode("none");
    setNetworkBridge("");
    setTempName("");
    setStartAfterRestore(false);
    setNetworkDangerAcknowledged(false);
    setCreatedPlan(null);
    createMutation.reset();
    setRejectReason("");
    setShowRejectInput(false);
    setOverwriteConfirmVmid("");
    approveMutation.reset();
    rejectMutation.reset();
    executeMutation.reset();
  };

  const errorChecks: RestorePlanCheckEntry[] | undefined = (() => {
    if (!(createMutation.error instanceof ApiError)) return undefined;
    const details = createMutation.error.details;
    if (typeof details !== "object" || details === null || !("checks" in details)) return undefined;
    const checks = (details as { checks: unknown }).checks;
    return Array.isArray(checks) ? (checks as RestorePlanCheckEntry[]) : undefined;
  })();

  const handleCopy = async () => {
    if (!createdPlan) return;
    try {
      await navigator.clipboard.writeText(createdPlan.generated_command);
      setCopyFeedback(true);
      setTimeout(() => setCopyFeedback(false), 2000);
    } catch {
      // clipboard access can be denied by the browser — the command is
      // still visible and selectable in the <pre> block either way.
    }
  };

  return (
    <div>
      <PageHeader title={t("restoreCenter.title")} description={t("restoreCenter.description")} />

      <Panel testId="restore-center-wizard">
        <Wizard
          steps={steps}
          currentStep={currentStep}
          onStepClick={handleStepClick}
          canAdvance={canAdvance}
          onBack={() => setCurrentStep((s) => Math.max(0, s - 1))}
          onNext={handleNext}
          nextLabel={t("restoreCenter.createPlan")}
        >
          {currentStep === 0 && (
            <SnapshotPicker
              customerId={customerId}
              onCustomerChange={setCustomerId}
              groupId={groupId}
              onGroupChange={setGroupId}
              snapshotId={snapshotId}
              onSnapshotChange={setSnapshotId}
              groupFilter={(group: BackupGroupOut) => group.backup_type === "vm" || group.backup_type === "ct"}
            />
          )}

          {currentStep === 1 && (
            <div className={styles.stepFields}>
              <div className="field">
                <label className="label">{t("instantRecovery.method")}</label>
                <div className={styles.radioColumn}>
                  {(["full", "instant"] as RestorePlanMethod[]).map((value) => (
                    <label key={value}>
                      <input
                        type="radio"
                        name="restore-method"
                        checked={method === value}
                        onChange={() => {
                          setMethod(value);
                          setPveClusterId("");
                          setTargetNode("");
                          setTargetStorage("");
                          if (value === "instant") setVmidIntent("new");
                        }}
                      />{" "}
                      {t(`instantRecovery.method_${value}`)}
                    </label>
                  ))}
                </div>
                {instant && <p className="mutedText">{t("instantRecovery.methodHint")}</p>}
                {instant && activeClusters.length === 0 && pveClustersQuery.isSuccess && (
                  <InlineAlert tone="info">{t("instantRecovery.noClusterAllowed")}</InlineAlert>
                )}
              </div>

              <div className="field">
                <label className="label" htmlFor="restore-center-cluster">
                  {t("restoreCenter.pveCluster")}
                </label>
                <select
                  id="restore-center-cluster"
                  className="select"
                  value={pveClusterId}
                  onChange={(e) => {
                    setPveClusterId(e.target.value);
                    const cluster = pveClustersQuery.data?.find((c) => c.id === e.target.value);
                    setTargetNode(instant && cluster?.lab_agent_node ? cluster.lab_agent_node : "");
                    setTargetStorage("");
                  }}
                >
                  <option value="">—</option>
                  {activeClusters.map((cluster) => (
                    <option key={cluster.id} value={cluster.id}>
                      {cluster.display_name}
                    </option>
                  ))}
                </select>
              </div>

              {pveClusterId !== "" && (
                <div className="field">
                  <label className="label" htmlFor="restore-center-node">
                    {t("restoreCenter.node")}
                  </label>
                  <select
                    id="restore-center-node"
                    className="select"
                    value={targetNode}
                    onChange={(e) => {
                      setTargetNode(e.target.value);
                      setTargetStorage("");
                    }}
                    disabled={nodesQuery.isLoading}
                  >
                    <option value="">—</option>
                    {nodesQuery.data?.map((node) => (
                      <option key={node.node} value={node.node}>
                        {node.node}
                      </option>
                    ))}
                  </select>
                  {nodesQuery.isError && <InlineAlert tone="danger">{t("common.loadError")}</InlineAlert>}
                  {instant && selectedCluster?.lab_agent_node && (
                    <p className="mutedText">{t("instantRecovery.nodeHint", { node: selectedCluster.lab_agent_node })}</p>
                  )}
                </div>
              )}

              {targetNode !== "" && (
                <div className="field">
                  <label className="label" htmlFor="restore-center-storage">
                    {t("restoreCenter.storage")}
                  </label>
                  <select
                    id="restore-center-storage"
                    className="select"
                    value={targetStorage}
                    onChange={(e) => setTargetStorage(e.target.value)}
                    disabled={storagesQuery.isLoading}
                  >
                    <option value="">—</option>
                    {storagesQuery.data
                      ?.filter((s) => s.type !== "pbs")
                      .map((storage) => (
                        <option key={storage.storage} value={storage.storage}>
                          {storage.storage}
                        </option>
                      ))}
                  </select>
                </div>
              )}

              {instant && targetStorage !== "" && (
                <p className="mutedText">{t("instantRecovery.storageHint", { storage: targetStorage })}</p>
              )}

              {!instant && (
              <div className="field">
                <label className="label">{t("restoreCenter.vmidIntent")}</label>
                <div className={styles.radioRow}>
                  <label>
                    <input
                      type="radio"
                      name="vmid-intent"
                      checked={vmidIntent === "new"}
                      onChange={() => setVmidIntent("new")}
                    />{" "}
                    {t("restoreCenter.vmidIntentNew")}
                  </label>
                  <label>
                    <input
                      type="radio"
                      name="vmid-intent"
                      checked={vmidIntent === "existing"}
                      onChange={() => setVmidIntent("existing")}
                    />{" "}
                    {t("restoreCenter.vmidIntentExisting")}
                  </label>
                </div>
              </div>
              )}

              <div className="field">
                <label className="label" htmlFor="restore-center-vmid">
                  {t("restoreCenter.targetVmid")}
                </label>
                <input
                  id="restore-center-vmid"
                  type="number"
                  min={1}
                  className="input"
                  value={targetVmid}
                  onChange={(e) => setTargetVmid(e.target.value)}
                />
                {vmidCheckQuery.data && pveClusterId !== "" && debouncedVmid === targetVmid && (
                  <p className={styles.vmidFeedback}>
                    {vmidIntent === "new" ? (
                      vmidCheckQuery.data.free ? (
                        <StatusBadge tone="ok" label={t("restoreCenter.vmidFree")} />
                      ) : (
                        <StatusBadge tone="danger" label={t("restoreCenter.vmidTaken")} />
                      )
                    ) : vmidCheckQuery.data.free ? (
                      <StatusBadge tone="danger" label={t("restoreCenter.vmidDoesNotExist")} />
                    ) : (
                      <StatusBadge tone="ok" label={t("restoreCenter.vmidExists")} />
                    )}
                  </p>
                )}
              </div>
            </div>
          )}

          {currentStep === 2 && (
            <div className={styles.stepFields}>
              <div className="field">
                <label className="label">{t("restoreCenter.networkMode")}</label>
                <div className={styles.radioColumn}>
                  {(["none", "isolated", "custom", "original"] as RestorePlanNetworkMode[]).map((mode) => (
                    <label key={mode}>
                      <input
                        type="radio"
                        name="network-mode"
                        checked={networkMode === mode}
                        onChange={() => setNetworkMode(mode)}
                      />{" "}
                      {t(`restoreCenter.networkMode${mode.charAt(0).toUpperCase()}${mode.slice(1)}`)}
                    </label>
                  ))}
                </div>
              </div>

              {networkMode === "original" && (
                <InlineAlert tone="danger">{t("restoreCenter.networkOriginalWarning")}</InlineAlert>
              )}

              {networkMode === "isolated" &&
                (selectedCluster?.restore_network_rules?.default_bridge ? (
                  <p className="mutedText">
                    {t("restoreCenter.networkIsolatedBridge", {
                      bridge: selectedCluster.restore_network_rules.default_bridge,
                    })}
                  </p>
                ) : (
                  <InlineAlert tone="info">{t("restoreCenter.networkNoRulesConfigured")}</InlineAlert>
                ))}

              {networkMode === "custom" && (
                <div className="field">
                  <label className="label" htmlFor="restore-center-bridge">
                    {t("restoreCenter.bridge")}
                  </label>
                  <input
                    id="restore-center-bridge"
                    className="input"
                    value={networkBridge}
                    onChange={(e) => setNetworkBridge(e.target.value)}
                  />
                </div>
              )}
            </div>
          )}

          {currentStep === 3 && (
            <div className={styles.stepFields}>
              <div className="field">
                <label className="label" htmlFor="restore-center-temp-name">
                  {t("restoreCenter.tempName")}
                </label>
                <input
                  id="restore-center-temp-name"
                  className="input"
                  value={tempName}
                  onChange={(e) => setTempName(e.target.value)}
                  placeholder={t("restoreCenter.tempNamePlaceholder")}
                />
              </div>

              {instant ? (
                <InlineAlert tone="info">{t("instantRecovery.startsAlways")}</InlineAlert>
              ) : (
              <div className="field">
                <label className="label" htmlFor="restore-center-start-after">
                  <input
                    id="restore-center-start-after"
                    type="checkbox"
                    checked={startAfterRestore}
                    onChange={(e) => setStartAfterRestore(e.target.checked)}
                  />{" "}
                  {t("restoreCenter.startAfterRestore")}
                </label>
              </div>
              )}

              <InlineAlert tone="info">{t("restoreCenter.ttlHonestyNote")}</InlineAlert>
            </div>
          )}

          {currentStep === 4 && !createdPlan && (
            <div className={styles.stepFields}>
              <dl className="detailGrid">
                <dt>{t("instantRecovery.method")}</dt>
                <dd>{t(`instantRecovery.method_${method}`)}</dd>
                <dt>{t("restoreCenter.pveCluster")}</dt>
                <dd>{selectedCluster?.display_name ?? "—"}</dd>
                <dt>{t("restoreCenter.node")}</dt>
                <dd>{targetNode}</dd>
                <dt>{t("restoreCenter.storage")}</dt>
                <dd>{targetStorage}</dd>
                <dt>{t("restoreCenter.targetVmid")}</dt>
                <dd>{targetVmid}</dd>
                <dt>{t("restoreCenter.networkMode")}</dt>
                <dd>{t(`restoreCenter.networkMode${networkMode.charAt(0).toUpperCase()}${networkMode.slice(1)}`)}</dd>
              </dl>

              {(networkMode === "original" || networkMode === "custom" || networkMode === "isolated") && (
                <div className="field">
                  <label>
                    <input
                      type="checkbox"
                      checked={networkDangerAcknowledged}
                      onChange={(e) => setNetworkDangerAcknowledged(e.target.checked)}
                    />{" "}
                    {t("restoreCenter.acknowledgeNetworkRisk")}
                  </label>
                </div>
              )}

              {createMutation.isError && (
                <InlineAlert tone="danger">
                  {createMutation.error instanceof ApiError ? createMutation.error.message : t("common.loadError")}
                </InlineAlert>
              )}

              {errorChecks && (
                <ul className={styles.checksList}>
                  {errorChecks.map((check) => (
                    <li key={check.check}>
                      <StatusBadge tone={checkTone(check.status)} label={check.message} className={styles.checkBadge} />
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {currentStep === 4 && createdPlan && (
            <div className={styles.stepFields}>
              <ul className={styles.checksList}>
                {createdPlan.checks_result.map((check) => (
                  <li key={check.check}>
                    <StatusBadge tone={checkTone(check.status)} label={check.message} className={styles.checkBadge} />
                  </li>
                ))}
              </ul>

              <p className="label">{t("restoreCenter.generatedCommand")}</p>
              <pre className={styles.commandBlock}>{createdPlan.generated_command}</pre>
              <div className={styles.commandActions}>
                <button type="button" className="button" onClick={handleCopy}>
                  {copyFeedback ? t("restoreCenter.copied") : t("restoreCenter.copyCommand")}
                </button>
                <button type="button" className="button" onClick={restartWizard}>
                  {t("restoreCenter.startNewPlan")}
                </button>
              </div>

              {/* EPIC 9 (manifest §11.2): four-eyes approval + PVE-API execution */}
              <div className={styles.executionSection}>
                {createdPlan.approval_status === "pending" &&
                  (isPlanCreator ? (
                    <InlineAlert tone="info">{t("restoreCenter.waitingForApproval")}</InlineAlert>
                  ) : (
                    <>
                      <StatusBadge tone="warning" label={t("restoreCenter.approvalRequired")} className={styles.checkBadge} />
                      <div className={styles.commandActions}>
                        <button
                          type="button"
                          className="button"
                          disabled={approveMutation.isPending}
                          onClick={() => approveMutation.mutate()}
                        >
                          {t("restoreCenter.approve")}
                        </button>
                        <button
                          type="button"
                          className="button buttonGhost"
                          onClick={() => setShowRejectInput(true)}
                        >
                          {t("restoreCenter.reject")}
                        </button>
                      </div>
                      {showRejectInput && (
                        <div className="field">
                          <label className="label" htmlFor="restore-center-reject-reason">
                            {t("restoreCenter.rejectReasonLabel")}
                          </label>
                          <input
                            id="restore-center-reject-reason"
                            className="input"
                            value={rejectReason}
                            onChange={(e) => setRejectReason(e.target.value)}
                          />
                          <button
                            type="button"
                            className="button"
                            disabled={!rejectReason.trim() || rejectMutation.isPending}
                            onClick={() => rejectMutation.mutate()}
                          >
                            {t("restoreCenter.confirmReject")}
                          </button>
                        </div>
                      )}
                    </>
                  ))}
                {createdPlan.approval_status === "approved" && (
                  <StatusBadge tone="ok" label={t("restoreCenter.approved")} />
                )}
                {createdPlan.approval_status === "rejected" && (
                  <StatusBadge tone="danger" label={t("restoreCenter.rejected")} />
                )}

                {createdPlan.execution_status === null && createdPlan.approval_status !== "rejected" && (
                  <>
                    {vmidIntent === "existing" && (
                      <div className="field">
                        <label className="label" htmlFor="restore-center-overwrite-confirm">
                          {t("restoreCenter.overwriteConfirmLabel", { vmid: targetVmid })}
                        </label>
                        <input
                          id="restore-center-overwrite-confirm"
                          className="input"
                          value={overwriteConfirmVmid}
                          onChange={(e) => setOverwriteConfirmVmid(e.target.value)}
                        />
                      </div>
                    )}
                    <button
                      type="button"
                      className="button"
                      disabled={!executeAllowed || executeMutation.isPending}
                      onClick={() => executeMutation.mutate()}
                    >
                      {createdPlan.method === "instant" ? t("instantRecovery.start") : t("restoreCenter.execute")}
                    </button>
                    {executeMutation.isError && (
                      <InlineAlert tone="danger">
                        {executeMutation.error instanceof ApiError ? executeMutation.error.message : t("common.loadError")}
                      </InlineAlert>
                    )}
                  </>
                )}

                {createdPlan.execution_status !== null && createdPlan.method === "instant" && (
                  <InstantRecoveryPanel plan={createdPlan} onChange={setCreatedPlan} />
                )}
                {createdPlan.execution_status !== null && createdPlan.method !== "instant" && (
                  <>
                    <p className="label">{t("restoreCenter.executionProgress")}</p>
                    <p>{t(`restoreCenter.executionStatus_${createdPlan.execution_status}`)}</p>
                    {createdPlan.upid && <p className="mutedText">{createdPlan.upid}</p>}
                    {createdPlan.execution_status === "completed" && (
                      <StatusBadge tone="ok" label={t("restoreCenter.executionCompleted")} className={styles.checkBadge} />
                    )}
                    {createdPlan.execution_error && <InlineAlert tone="danger">{createdPlan.execution_error}</InlineAlert>}
                  </>
                )}
              </div>
            </div>
          )}
        </Wizard>
      </Panel>

      {openInstantPlans.length > 0 && (
        <Panel title={t("instantRecovery.listTitle")} testId="restore-center-instant-recoveries">
          <p className="panelIntro">{t("instantRecovery.listIntro")}</p>
          {openInstantPlans
            .filter((p) => p.id !== createdPlan?.id)
            .map((p) => (
              <div key={p.id} className={styles.executionSection}>
                <InstantRecoveryPanel plan={p} />
              </div>
            ))}
        </Panel>
      )}
    </div>
  );
}
