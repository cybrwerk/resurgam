import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { customersApi, datastoresApi, pbsInstancesApi, pbsJobsApi, pveClustersApi } from "../api/resources";
import type {
  PveClusterOut,
  PveGuestOut,
  TestConnectionResponse,
  TlsTrustMode,
  VzdumpJobCreateRequest,
  VzdumpJobOut,
} from "../api/types";
import { BackupJobDisableDialog } from "../components/BackupJobDisableDialog";
import { BackupJobStatus } from "../components/BackupJobStatus";
import { ConfirmActionDialog } from "../components/ConfirmActionDialog";
import {
  ConnectionTestResult,
  HealthCell,
  RequiredPermissionsPanel,
  TokenFields,
  emptyTokenState,
  parsePort,
  tokenChanged,
  tokenPayload,
  type TokenFormState,
} from "../components/ConnectionOnboarding";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { LabComputeSetup, LabReadinessPill, canSetUpLab } from "../components/LabComputeSetup";
import { InlineAlert } from "../components/InlineAlert";
import { Modal } from "../components/Modal";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { PruneSimulationButton } from "../components/PruneSimulationButton";
import { QueryState } from "../components/QueryState";
import { StatusBadge } from "../components/StatusBadge";
import { VzdumpScheduleField } from "../components/VzdumpScheduleField";
import styles from "./PbsInstancesPage.module.css";
import localStyles from "./PveClustersPage.module.css";

const QUERY_KEY = ["pve-clusters"];

function CreatePveClusterForm({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [displayName, setDisplayName] = useState("");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("8006");
  const [trustMode, setTrustMode] = useState<TlsTrustMode>("ca");
  const [fingerprint, setFingerprint] = useState("");
  const [token, setToken] = useState<TokenFormState>(emptyTokenState);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<PveClusterOut | null>(null);

  const probeMutation = useMutation({
    mutationFn: () => pveClustersApi.probeTls(host, parsePort(port) ?? 8006),
    onSuccess: (result) => setFingerprint(result.fingerprint_sha256),
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  // Runs automatically right after the cluster was saved; the button in
  // the result view only re-runs it.
  const testMutation = useMutation({
    mutationFn: (id: string) => pveClustersApi.testConnection(id),
    onSettled: () => queryClient.invalidateQueries({ queryKey: QUERY_KEY }),
  });

  const createMutation = useMutation({
    mutationFn: (validPort: number) =>
      pveClustersApi.create({
        display_name: displayName,
        host,
        port: validPort,
        tls_trust_mode: trustMode,
        tls_fingerprint_sha256: trustMode === "fingerprint" ? fingerprint : null,
        ...tokenPayload(token, false),
      }),
    onSuccess: (cluster) => {
      setToken(emptyTokenState());
      setCreated(cluster);
      queryClient.invalidateQueries({ queryKey: QUERY_KEY });
      testMutation.mutate(cluster.id);
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  if (created) {
    return (
      <Panel title={t("connection.createdTitle", { name: created.display_name })} className={localStyles.createPanel}>
        <ConnectionTestResult
          result={testMutation.data ?? null}
          pending={testMutation.isPending}
          error={testMutation.error ? (testMutation.error as Error).message : null}
        />
        <div className="formActions">
          <button
            type="button"
            className="button"
            onClick={() => testMutation.mutate(created.id)}
            disabled={testMutation.isPending}
          >
            {t("connection.retest")}
          </button>
          <button type="button" className="button buttonPrimary" onClick={onDone}>
            {t("common.close")}
          </button>
        </div>
      </Panel>
    );
  }

  return (
    <Panel title={t("pveClusters.createTitle")} className={localStyles.createPanel}>
      <form
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          setError(null);
          const validPort = parsePort(port);
          if (validPort === null) {
            setError(t("connection.portInvalid"));
            return;
          }
          createMutation.mutate(validPort);
        }}
      >
        {error && (
          <div className={localStyles.alert}>
            <InlineAlert>{error}</InlineAlert>
          </div>
        )}
        <div className="formGrid">
          <div className="field">
            <label className="label" htmlFor="pve-display-name">
              {t("pbsInstances.displayName")}
            </label>
            <input
              id="pve-display-name"
              className="input"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="pve-host">
              {t("pveClusters.host")}
            </label>
            <input id="pve-host" className="input" value={host} onChange={(e) => setHost(e.target.value)} required />
          </div>
          <div className="field">
            <label className="label" htmlFor="pve-port">
              {t("pveClusters.port")}
            </label>
            <input
              id="pve-port"
              type="number"
              className="input"
              value={port}
              onChange={(e) => setPort(e.target.value)}
              aria-describedby="pve-port-hint"
              required
            />
            <p id="pve-port-hint" className="mutedText">
              {t("connection.portHint", { port: 8006 })}
            </p>
          </div>
          <div className="field">
            <label className="label" htmlFor="pve-trust-mode">
              {t("pbsInstances.trustMode")}
            </label>
            <select
              id="pve-trust-mode"
              className="select"
              value={trustMode}
              onChange={(e) => setTrustMode(e.target.value as TlsTrustMode)}
            >
              <option value="ca">{t("pbsInstances.trustModeCa")}</option>
              <option value="fingerprint">{t("pbsInstances.trustModeFingerprint")}</option>
            </select>
          </div>

          {trustMode === "fingerprint" && (
            <div className="field">
              <span className="label">{t("pbsInstances.fingerprint")}</span>
              <div className={localStyles.fingerprintRow}>
                <button
                  type="button"
                  className="button"
                  onClick={() => probeMutation.mutate()}
                  disabled={!host || parsePort(port) === null || probeMutation.isPending}
                >
                  {t("pbsInstances.probeFingerprint")}
                </button>
                {fingerprint && <span className={styles.fingerprintValue}>{fingerprint}</span>}
              </div>
            </div>
          )}

          <TokenFields product="pve" idPrefix="pve" state={token} onChange={setToken} />
          <RequiredPermissionsPanel product="pve" fetcher={pveClustersApi.requiredPrivileges} />
        </div>

        <div className="formActions">
          <button type="button" className="button" onClick={onDone}>
            {t("common.cancel")}
          </button>
          <button
            type="submit"
            className="button buttonPrimary"
            disabled={createMutation.isPending || (trustMode === "fingerprint" && !fingerprint)}
          >
            {token.mode === "credentials" ? t("connection.createWithCredentials") : t("common.create")}
          </button>
        </div>
      </form>
    </Panel>
  );
}

const VZDUMP_MODES = ["", "snapshot", "suspend", "stop"] as const;
const VZDUMP_COMPRESS = ["", "0", "gzip", "lzo", "zstd"] as const;

function toKeepInt(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === "") return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

/** Picks specific VMs/CTs to target instead of typing a comma-separated
 * vmid list by hand -- the PVE API already lists every real guest
 * cluster-wide (`list_guests`), no reason to make an operator guess IDs. */
function GuestPickerDialog({
  clusterId,
  initialVmids,
  onConfirm,
  onClose,
}: {
  clusterId: string;
  initialVmids: string[];
  onConfirm: (vmids: string[]) => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const guestsQuery = useQuery({ queryKey: ["pve-cluster-guests", clusterId], queryFn: () => pveClustersApi.guests(clusterId) });
  const [selected, setSelected] = useState<Set<string>>(new Set(initialVmids));
  const [search, setSearch] = useState("");

  const guests = guestsQuery.data ?? [];
  const term = search.trim().toLowerCase();
  const visibleGuests = term
    ? guests.filter((g) => `${g.vmid} ${g.name ?? ""} ${g.node}`.toLowerCase().includes(term))
    : guests;

  const toggle = (vmid: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      const key = String(vmid);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  return (
    <Modal open title={t("jobs.selectGuestsTitle")} onClose={onClose}>
      <div className="field">
        <label className="label" htmlFor="guest-picker-search">
          {t("jobs.selectGuestsSearchLabel")}
        </label>
        <input id="guest-picker-search" className="input" value={search} onChange={(e) => setSearch(e.target.value)} placeholder={t("jobs.selectGuestsSearchPlaceholder")} />
      </div>
      {guestsQuery.isLoading ? (
        <p className="mutedText">{t("common.loading")}</p>
      ) : visibleGuests.length === 0 ? (
        <p className="mutedText">{t("common.empty")}</p>
      ) : (
        <div style={{ maxHeight: "20rem", overflowY: "auto", border: "1px solid var(--border-default)", borderRadius: "0.35rem" }}>
          {visibleGuests.map((guest: PveGuestOut) => (
            <label
              key={guest.vmid}
              style={{ display: "flex", alignItems: "center", gap: "0.5rem", padding: "0.35rem 0.6rem", fontSize: "0.875rem", borderBottom: "1px solid var(--border-default)" }}
            >
              <input type="checkbox" checked={selected.has(String(guest.vmid))} onChange={() => toggle(guest.vmid)} />
              <span className="resurgam-mono">{guest.vmid}</span>
              <span>{guest.name ?? "—"}</span>
              <span className="mutedText">({guest.guest_type}, {guest.node})</span>
            </label>
          ))}
        </div>
      )}
      <p className="mutedText">{t("jobs.selectGuestsCount", { count: selected.size })}</p>
      <div className="formActions">
        <button type="button" className="button" onClick={onClose}>
          {t("common.cancel")}
        </button>
        <button type="button" className="button buttonPrimary" onClick={() => onConfirm([...selected])}>
          {t("common.save")}
        </button>
      </div>
    </Modal>
  );
}

/** Cross-checks a vzdump job's own (optional) retention against the
 * target storage's real PBS Prune Job(s), resolved via the PVE storage
 * config's own `pbs_datastore`/`pbs_namespace` (confirmed live -- see
 * `RemoteStorage`'s docstring) -- not a name-matching guess. Reads use
 * RESURGAM's own configured `PbsInstance` credential, a separate thing
 * from whatever PBS credential is stored *inside* PVE's own storage.cfg
 * for this storage; RESURGAM has no way to inspect the latter (PVE
 * redacts it, same as any other storage password/key -- ADR 0022's own
 * finding for `encryption-key` applies here too), so a job whose
 * retention looks consistent here can still fail to actually prune if
 * PVE's own stored credential lacks the right grant on the PBS side. */
function RetentionComparisonHint({
  clusterId,
  storage,
  hasVzdumpRetention,
  keepLast,
  keepHourly,
  keepDaily,
  keepWeekly,
  keepMonthly,
  keepYearly,
}: {
  clusterId: string;
  storage: string;
  hasVzdumpRetention: boolean;
  keepLast: string;
  keepHourly: string;
  keepDaily: string;
  keepWeekly: string;
  keepMonthly: string;
  keepYearly: string;
}) {
  const { t } = useTranslation();

  const nodesQuery = useQuery({ queryKey: ["pve-cluster-nodes", clusterId], queryFn: () => pveClustersApi.nodes(clusterId) });
  const firstNode = nodesQuery.data?.[0]?.node;
  const storagesQuery = useQuery({
    queryKey: ["pve-cluster-storages", clusterId, firstNode],
    queryFn: () => pveClustersApi.storages(clusterId, firstNode!),
    enabled: !!firstNode,
  });
  const matchedStorage = storagesQuery.data?.find((s) => s.storage === storage);
  const datastoresQuery = useQuery({
    queryKey: ["datastores"],
    queryFn: datastoresApi.list,
    enabled: !!matchedStorage?.pbs_datastore,
  });
  const datastore = datastoresQuery.data?.find((d) => d.name === matchedStorage?.pbs_datastore);
  const pruneJobsQuery = useQuery({
    queryKey: ["pbs-prune-jobs", datastore?.pbs_instance_id, datastore?.name],
    queryFn: () => pbsJobsApi.pruneJobs(datastore!.pbs_instance_id, datastore!.name),
    enabled: !!datastore,
  });

  if (!storage || matchedStorage?.type !== "pbs") return null;
  if (!matchedStorage.pbs_datastore || !datastore) {
    return <p className="mutedText">{t("pveClusters.retentionComparisonUnresolved")}</p>;
  }
  if (pruneJobsQuery.isLoading) return <p className="mutedText">{t("common.loading")}</p>;

  const relevantPruneJobs = (pruneJobsQuery.data ?? []).filter(
    (job) => (job.ns ?? "") === (matchedStorage.pbs_namespace ?? ""),
  );
  const hasPbsPruneJob = relevantPruneJobs.length > 0;

  return (
    <div style={{ gridColumn: "1 / -1" }}>
      <p className="mutedText">
        {t("pveClusters.retentionComparisonDatastore", { name: datastore.name, namespace: matchedStorage.pbs_namespace || "/" })}
      </p>
      {hasPbsPruneJob ? (
        <ul style={{ margin: "0 0 0.5rem", paddingLeft: "1.25rem", fontSize: "0.8rem" }}>
          {relevantPruneJobs.map((pruneJob) => (
            <li key={pruneJob.id} className="mutedText">
              {t("pveClusters.retentionComparisonPruneJob", {
                schedule: pruneJob.schedule ?? "—",
                keep: [
                  pruneJob.keep_last && `last=${pruneJob.keep_last}`,
                  pruneJob.keep_hourly && `hourly=${pruneJob.keep_hourly}`,
                  pruneJob.keep_daily && `daily=${pruneJob.keep_daily}`,
                  pruneJob.keep_weekly && `weekly=${pruneJob.keep_weekly}`,
                  pruneJob.keep_monthly && `monthly=${pruneJob.keep_monthly}`,
                  pruneJob.keep_yearly && `yearly=${pruneJob.keep_yearly}`,
                ]
                  .filter(Boolean)
                  .join(", "),
              })}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mutedText">{t("pveClusters.retentionComparisonNoPruneJob")}</p>
      )}
      {hasVzdumpRetention && hasPbsPruneJob && (
        <InlineAlert tone="danger">{t("pveClusters.retentionComparisonConflict")}</InlineAlert>
      )}
      {!hasVzdumpRetention && !hasPbsPruneJob && (
        <InlineAlert tone="danger">{t("pveClusters.retentionComparisonGap")}</InlineAlert>
      )}
      <p className="mutedText">{t("pveClusters.retentionComparisonAuthCaveat")}</p>
      {hasVzdumpRetention && (
        <PruneSimulationButton
          datastoreId={datastore.id}
          namespace={matchedStorage.pbs_namespace ?? ""}
          keepLast={keepLast}
          keepHourly={keepHourly}
          keepDaily={keepDaily}
          keepWeekly={keepWeekly}
          keepMonthly={keepMonthly}
          keepYearly={keepYearly}
        />
      )}
    </div>
  );
}

/** Create/edit form for a single vzdump job. `prune_keep_*` mirrors a
 * PBS Prune Job's own field names -- see `VzdumpJobOut`'s own docstring
 * for why this exists and why the primary, intended home for retention
 * is still a PBS Prune Job, not this (docs/adr/0023). */
export function VzdumpJobFormModal({
  clusterId,
  job,
  onClose,
}: {
  clusterId: string;
  job: VzdumpJobOut | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [schedule, setSchedule] = useState(job?.schedule ?? "");
  const [storage, setStorage] = useState(job?.storage ?? "");
  const [node, setNode] = useState(job?.node ?? "");
  const [comment, setComment] = useState(job?.comment ?? "");
  const [allGuests, setAllGuests] = useState(job ? job.all_guests : true);
  const [vmid, setVmid] = useState(job?.vmid ?? "");
  const [pool, setPool] = useState(job?.pool ?? "");
  const [exclude, setExclude] = useState(job?.exclude ?? "");
  const [mode, setMode] = useState(job?.mode ?? "");
  const [compress, setCompress] = useState(job?.compress ?? "");
  const [notesTemplate, setNotesTemplate] = useState(job?.notes_template ?? (job === null ? "{{guestname}}" : ""));
  const [showRetention, setShowRetention] = useState(
    job !== null &&
      (job.prune_keep_last != null ||
        job.prune_keep_hourly != null ||
        job.prune_keep_daily != null ||
        job.prune_keep_weekly != null ||
        job.prune_keep_monthly != null ||
        job.prune_keep_yearly != null),
  );
  const [keepLast, setKeepLast] = useState(job?.prune_keep_last?.toString() ?? "");
  const [keepHourly, setKeepHourly] = useState(job?.prune_keep_hourly?.toString() ?? "");
  const [keepDaily, setKeepDaily] = useState(job?.prune_keep_daily?.toString() ?? "");
  const [keepWeekly, setKeepWeekly] = useState(job?.prune_keep_weekly?.toString() ?? "");
  const [keepMonthly, setKeepMonthly] = useState(job?.prune_keep_monthly?.toString() ?? "");
  const [keepYearly, setKeepYearly] = useState(job?.prune_keep_yearly?.toString() ?? "");
  const [error, setError] = useState<string | null>(null);
  const [showGuestPicker, setShowGuestPicker] = useState(false);

  const nodesQuery = useQuery({ queryKey: ["pve-cluster-nodes", clusterId], queryFn: () => pveClustersApi.nodes(clusterId) });
  const firstNode = nodesQuery.data?.[0]?.node;
  const storagesQuery = useQuery({
    queryKey: ["pve-cluster-storages", clusterId, firstNode],
    queryFn: () => pveClustersApi.storages(clusterId, firstNode!),
    enabled: !!firstNode,
  });
  const poolsQuery = useQuery({ queryKey: ["pve-cluster-pools", clusterId], queryFn: () => pveClustersApi.pools(clusterId) });
  // Any storage PVE will actually let a vzdump job target -- a real "pbs"
  // storage always qualifies; any other storage type only if its
  // `content` list includes "backup" (PVE's own real content-type gate).
  const backupCapableStorages = (storagesQuery.data ?? []).filter((s) => s.type === "pbs" || s.content.includes("backup"));

  const queryKey = ["pve-cluster-backup-jobs", clusterId];
  const buildBody = (): VzdumpJobCreateRequest => ({
    schedule: schedule || null,
    storage: storage || null,
    node: node || null,
    comment: comment || null,
    all_guests: allGuests,
    vmid: allGuests ? null : vmid || null,
    pool: allGuests ? null : pool || null,
    exclude: exclude || null,
    mode: mode || null,
    compress: compress || null,
    notes_template: notesTemplate || null,
    ...(showRetention
      ? {
          prune_keep_last: toKeepInt(keepLast),
          prune_keep_hourly: toKeepInt(keepHourly),
          prune_keep_daily: toKeepInt(keepDaily),
          prune_keep_weekly: toKeepInt(keepWeekly),
          prune_keep_monthly: toKeepInt(keepMonthly),
          prune_keep_yearly: toKeepInt(keepYearly),
        }
      : {}),
  });
  const hasVzdumpRetention =
    showRetention && [keepLast, keepHourly, keepDaily, keepWeekly, keepMonthly, keepYearly].some((v) => v.trim() !== "");

  const saveMutation = useMutation({
    mutationFn: () =>
      job ? pveClustersApi.updateBackupJob(clusterId, job.id, buildBody()) : pveClustersApi.createBackupJob(clusterId, buildBody()),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
      onClose();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const clearRetentionMutation = useMutation({
    mutationFn: () => pveClustersApi.updateBackupJob(clusterId, job!.id, { clear_prune_backups: true }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
      onClose();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  return (
    <Modal
      open
      size="large"
      title={job ? t("pveClusters.editBackupJobTitle", { schedule: job.schedule ?? "" }) : t("pveClusters.createBackupJobTitle")}
      onClose={onClose}
    >
      <form
        className={`${styles.createForm} ${styles.createFormWide}`}
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          setError(null);
          saveMutation.mutate();
        }}
      >
        {error && (
          <div style={{ gridColumn: "1 / -1" }}>
            <InlineAlert>{error}</InlineAlert>
          </div>
        )}
        <VzdumpScheduleField value={schedule} onChange={setSchedule} />
        <div className="field">
          <label className="label" htmlFor="vzdump-storage">
            {t("pveClusters.backupJobStorage")}
          </label>
          <select id="vzdump-storage" className="select" value={storage} onChange={(e) => setStorage(e.target.value)} required>
            <option value="">{t("jobs.pickStoragePlaceholder")}</option>
            {backupCapableStorages.map((s) => (
              <option key={s.storage} value={s.storage}>
                {s.storage} {s.type === "pbs" ? `(${t("jobs.pickStoragePbsHint")})` : ""}
              </option>
            ))}
            {/* The job's current storage might not be backup-capable (or
                not visible from the first node) -- keep it selectable so
                editing an existing job never silently loses its value. */}
            {storage && !backupCapableStorages.some((s) => s.storage === storage) && <option value={storage}>{storage}</option>}
          </select>
        </div>
        <div className="field">
          <label className="label" htmlFor="vzdump-node">
            {t("pveClusters.backupJobNode")}
          </label>
          <select id="vzdump-node" className="select" value={node} onChange={(e) => setNode(e.target.value)}>
            <option value="">{t("pveClusters.backupJobNodeClusterWide")}</option>
            {nodesQuery.data?.map((n) => (
              <option key={n.node} value={n.node}>
                {n.node}
              </option>
            ))}
            {/* Same safety net the storage select above uses: keep the
                job's current node selectable even if it's momentarily
                missing from a live node list (node offline, API hiccup). */}
            {node && !nodesQuery.data?.some((n) => n.node === node) && <option value={node}>{node}</option>}
          </select>
        </div>
        <div className="field">
          <label className="label" htmlFor="vzdump-comment">
            {t("pveClusters.backupJobComment")}
          </label>
          <input id="vzdump-comment" className="input" value={comment} onChange={(e) => setComment(e.target.value)} />
        </div>

        <div className="field" style={{ gridColumn: "1 / -1" }}>
          <span className="label">{t("pveClusters.backupJobTargeting")}</span>
          <label style={{ display: "flex", alignItems: "center", gap: "0.4rem", fontSize: "0.875rem" }}>
            <input type="checkbox" checked={allGuests} onChange={(e) => setAllGuests(e.target.checked)} />
            {t("pveClusters.backupJobAllGuests")}
          </label>
        </div>
        {!allGuests && (
          <>
            <div className="field" style={{ gridColumn: "1 / -1" }}>
              <span className="label">{t("pveClusters.backupJobVmid")}</span>
              <div style={{ display: "flex", alignItems: "center", gap: "0.6rem" }}>
                <button type="button" className="button" onClick={() => setShowGuestPicker(true)}>
                  {t("jobs.selectGuestsButton")}
                </button>
                <span className="mutedText">
                  {vmid ? t("jobs.selectGuestsCount", { count: vmid.split(",").filter(Boolean).length }) : t("jobs.selectGuestsNone")}
                </span>
              </div>
            </div>
            <div className="field">
              <label className="label" htmlFor="vzdump-pool">
                {t("pveClusters.backupJobPool")}
              </label>
              <select id="vzdump-pool" className="select" value={pool} onChange={(e) => setPool(e.target.value)}>
                <option value="">{t("jobs.pickPoolNone")}</option>
                {poolsQuery.data?.map((p) => (
                  <option key={p.poolid} value={p.poolid}>
                    {p.poolid}
                  </option>
                ))}
              </select>
            </div>
          </>
        )}
        {showGuestPicker && (
          <GuestPickerDialog
            clusterId={clusterId}
            initialVmids={vmid.split(",").map((v) => v.trim()).filter(Boolean)}
            onClose={() => setShowGuestPicker(false)}
            onConfirm={(vmids) => {
              setVmid(vmids.join(","));
              setShowGuestPicker(false);
            }}
          />
        )}
        <div className="field">
          <label className="label" htmlFor="vzdump-exclude">
            {t("pveClusters.backupJobExclude")}
          </label>
          <input id="vzdump-exclude" className="input" value={exclude} onChange={(e) => setExclude(e.target.value)} />
        </div>
        <div className="field">
          <label className="label" htmlFor="vzdump-mode">
            {t("pveClusters.backupJobMode")}
          </label>
          <select id="vzdump-mode" className="select" value={mode} onChange={(e) => setMode(e.target.value)}>
            {VZDUMP_MODES.map((option) => (
              <option key={option} value={option}>
                {t(`pveClusters.backupJobModeOptions.${option}`)}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label className="label" htmlFor="vzdump-compress">
            {t("pveClusters.backupJobCompress")}
          </label>
          <select id="vzdump-compress" className="select" value={compress} onChange={(e) => setCompress(e.target.value)}>
            {VZDUMP_COMPRESS.map((option) => (
              <option key={option} value={option}>
                {t(`pveClusters.backupJobCompressOptions.${option}`)}
              </option>
            ))}
          </select>
        </div>
        <div className="field" style={{ gridColumn: "1 / -1" }}>
          <label className="label" htmlFor="vzdump-notes-template">
            {t("pveClusters.backupJobNotesTemplate")}
          </label>
          <input
            id="vzdump-notes-template"
            className="input"
            value={notesTemplate}
            onChange={(e) => setNotesTemplate(e.target.value)}
            placeholder="{{guestname}}"
          />
          <p className="mutedText">
            {t("pveClusters.backupJobNotesTemplateHint")}{" "}
            <code>{"{{guestname}}"}</code> ({t("pveClusters.backupJobNotesTemplateVarGuestname")}, default),{" "}
            <code>{"{{cluster}}"}</code> ({t("pveClusters.backupJobNotesTemplateVarCluster")}),{" "}
            <code>{"{{node}}"}</code> ({t("pveClusters.backupJobNotesTemplateVarNode")}),{" "}
            <code>{"{{vmid}}"}</code> ({t("pveClusters.backupJobNotesTemplateVarVmid")})
          </p>
        </div>

        <div style={{ gridColumn: "1 / -1" }}>
          <h3 className={styles.sectionSubtitle}>{t("pveClusters.backupJobRetentionTitle")}</h3>
          <p className="mutedText">{t("pveClusters.backupJobRetentionHint")}</p>
          <label style={{ display: "flex", alignItems: "center", gap: "0.4rem", fontSize: "0.875rem", marginBottom: "0.5rem" }}>
            <input type="checkbox" checked={showRetention} onChange={(e) => setShowRetention(e.target.checked)} />
            {t("pveClusters.backupJobRetentionEnable")}
          </label>
        </div>
        {showRetention && (
          <>
            <div className="field">
              <label className="label" htmlFor="vzdump-keep-last">
                {t("pbsJobs.pruneKeepLast")}
              </label>
              <input id="vzdump-keep-last" type="number" min={0} className="input" value={keepLast} onChange={(e) => setKeepLast(e.target.value)} />
            </div>
            <div className="field">
              <label className="label" htmlFor="vzdump-keep-hourly">
                {t("pbsJobs.pruneKeepHourly")}
              </label>
              <input id="vzdump-keep-hourly" type="number" min={0} className="input" value={keepHourly} onChange={(e) => setKeepHourly(e.target.value)} />
            </div>
            <div className="field">
              <label className="label" htmlFor="vzdump-keep-daily">
                {t("pbsJobs.pruneKeepDaily")}
              </label>
              <input id="vzdump-keep-daily" type="number" min={0} className="input" value={keepDaily} onChange={(e) => setKeepDaily(e.target.value)} />
            </div>
            <div className="field">
              <label className="label" htmlFor="vzdump-keep-weekly">
                {t("pbsJobs.pruneKeepWeekly")}
              </label>
              <input id="vzdump-keep-weekly" type="number" min={0} className="input" value={keepWeekly} onChange={(e) => setKeepWeekly(e.target.value)} />
            </div>
            <div className="field">
              <label className="label" htmlFor="vzdump-keep-monthly">
                {t("pbsJobs.pruneKeepMonthly")}
              </label>
              <input id="vzdump-keep-monthly" type="number" min={0} className="input" value={keepMonthly} onChange={(e) => setKeepMonthly(e.target.value)} />
            </div>
            <div className="field">
              <label className="label" htmlFor="vzdump-keep-yearly">
                {t("pbsJobs.pruneKeepYearly")}
              </label>
              <input id="vzdump-keep-yearly" type="number" min={0} className="input" value={keepYearly} onChange={(e) => setKeepYearly(e.target.value)} />
            </div>
            {storage && (
              <RetentionComparisonHint
                clusterId={clusterId}
                storage={storage}
                hasVzdumpRetention={hasVzdumpRetention}
                keepLast={keepLast}
                keepHourly={keepHourly}
                keepDaily={keepDaily}
                keepWeekly={keepWeekly}
                keepMonthly={keepMonthly}
                keepYearly={keepYearly}
              />
            )}
          </>
        )}

        <div className="formActions">
          <button type="button" className="button" onClick={onClose} disabled={saveMutation.isPending}>
            {t("common.cancel")}
          </button>
          {job &&
            (job.prune_keep_last != null ||
              job.prune_keep_hourly != null ||
              job.prune_keep_daily != null ||
              job.prune_keep_weekly != null ||
              job.prune_keep_monthly != null ||
              job.prune_keep_yearly != null) && (
              <button
                type="button"
                className="button buttonDanger"
                onClick={() => clearRetentionMutation.mutate()}
                disabled={saveMutation.isPending || clearRetentionMutation.isPending}
              >
                {t("pveClusters.backupJobClearRetention")}
              </button>
            )}
          <button type="submit" className="button buttonPrimary" disabled={saveMutation.isPending || schedule.trim() === ""}>
            {t("common.save")}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function PveClusterBackupJobsSection({ clusterId }: { clusterId: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [pendingDisable, setPendingDisable] = useState<VzdumpJobOut | null>(null);
  const [pendingTimerChange, setPendingTimerChange] = useState<VzdumpJobOut | null>(null);
  const [pendingDelete, setPendingDelete] = useState<VzdumpJobOut | null>(null);
  const [editingJob, setEditingJob] = useState<VzdumpJobOut | null | "new">(null);
  const queryKey = ["pve-cluster-backup-jobs", clusterId];

  const capabilitiesQuery = useQuery({
    queryKey: ["pve-cluster-backup-job-capabilities", clusterId],
    queryFn: () => pveClustersApi.backupJobCapabilities(clusterId),
  });
  const canWrite = capabilitiesQuery.data?.can_write ?? false;

  const jobsQuery = useQuery({ queryKey, queryFn: () => pveClustersApi.backupJobs(clusterId) });
  const customersQuery = useQuery({ queryKey: ["customers"], queryFn: customersApi.list });
  const invalidate = () => queryClient.invalidateQueries({ queryKey });
  const enableMutation = useMutation({
    mutationFn: (jobId: string) => pveClustersApi.enableBackupJob(clusterId, jobId),
    onSuccess: invalidate,
  });
  const disableMutation = useMutation({
    mutationFn: ({ jobId, hours }: { jobId: string; hours: number | null }) =>
      pveClustersApi.disableBackupJob(clusterId, jobId, hours),
    onSuccess: invalidate,
  });
  const cancelReenableMutation = useMutation({
    mutationFn: (jobId: string) => pveClustersApi.cancelBackupJobReenable(clusterId, jobId),
    onSuccess: invalidate,
  });
  const changeReenableMutation = useMutation({
    mutationFn: ({ jobId, hours }: { jobId: string; hours: number }) =>
      pveClustersApi.setBackupJobReenable(clusterId, jobId, hours),
    onSuccess: invalidate,
  });
  const deleteMutation = useMutation({
    mutationFn: (jobId: string) => pveClustersApi.deleteBackupJob(clusterId, jobId),
    onSuccess: invalidate,
  });
  const assignCustomerMutation = useMutation({
    mutationFn: ({ jobId, customerId }: { jobId: string; customerId: string }) =>
      customerId ? pveClustersApi.assignBackupJobCustomer(clusterId, jobId, customerId) : pveClustersApi.unassignBackupJobCustomer(clusterId, jobId),
    onSuccess: invalidate,
  });

  const columns: DataTableColumn<VzdumpJobOut>[] = [
    { key: "schedule", header: t("pveClusters.backupJobSchedule"), render: (row) => row.schedule ?? "—" },
    { key: "storage", header: t("pveClusters.backupJobStorage"), render: (row) => row.storage ?? "—" },
    { key: "comment", header: t("common.name"), render: (row) => row.comment ?? "—" },
    {
      key: "customer_id",
      header: t("pveClusters.backupJobCustomerOverride"),
      render: (row) => (
        <select
          className="select"
          value={row.customer_id ?? ""}
          disabled={!canWrite}
          onChange={(e) => assignCustomerMutation.mutate({ jobId: row.id, customerId: e.target.value })}
        >
          <option value="">{t("pveClusters.backupJobCustomerOverrideNone")}</option>
          {customersQuery.data?.map((customer) => (
            <option key={customer.id} value={customer.id}>
              {customer.name}
            </option>
          ))}
        </select>
      ),
    },
    {
      key: "enabled",
      header: t("common.status"),
      render: (row) => (
        <BackupJobStatus
          job={row}
          canWrite={canWrite}
          cancelPending={cancelReenableMutation.isPending || changeReenableMutation.isPending}
          onCancelTimer={() => cancelReenableMutation.mutate(row.id)}
          onChangeTimer={() => setPendingTimerChange(row)}
        />
      ),
    },
    {
      key: "actions",
      header: t("common.actions"),
      render: (row) => (
        <span className={styles.rowActions}>
          {row.enabled ? (
            <button type="button" className="button buttonDanger" onClick={() => setPendingDisable(row)} disabled={!canWrite}>
              {t("common.disable")}
            </button>
          ) : (
            <button type="button" className="button" onClick={() => enableMutation.mutate(row.id)} disabled={!canWrite}>
              {row.reenable_at ? t("backupJobReenable.enableNow") : t("common.enable")}
            </button>
          )}
          <button type="button" className="button" onClick={() => setEditingJob(row)} disabled={!canWrite}>
            {t("common.edit")}
          </button>
          <button type="button" className="button buttonDanger" onClick={() => setPendingDelete(row)} disabled={!canWrite}>
            {t("common.delete")}
          </button>
        </span>
      ),
    },
  ];

  return (
    <Panel
      title={t("pveClusters.backupJobsTitle")}
      className={localStyles.modalSection}
      actions={
        <button type="button" className="button" onClick={() => setEditingJob("new")} disabled={!canWrite}>
          {t("common.create")}
        </button>
      }
    >
      {capabilitiesQuery.data && !canWrite && (
        <div className={localStyles.alert}>
          <InlineAlert tone="info">{t("common.capabilitiesReadOnly")}</InlineAlert>
        </div>
      )}
      <QueryState query={jobsQuery} isEmpty={(data) => data.length === 0}>
        {(jobs) => <DataTable columns={columns} rows={jobs} rowKey={(row) => row.id} />}
      </QueryState>
      <BackupJobDisableDialog
        open={pendingTimerChange !== null}
        mode="changeTimer"
        schedule={pendingTimerChange?.schedule}
        onConfirm={(hours) => {
          if (pendingTimerChange && hours !== null) changeReenableMutation.mutate({ jobId: pendingTimerChange.id, hours });
          setPendingTimerChange(null);
        }}
        onCancel={() => setPendingTimerChange(null)}
      />
      <BackupJobDisableDialog
        open={pendingDisable !== null}
        schedule={pendingDisable?.schedule}
        onConfirm={(hours) => {
          if (pendingDisable) disableMutation.mutate({ jobId: pendingDisable.id, hours });
          setPendingDisable(null);
        }}
        onCancel={() => setPendingDisable(null)}
      />
      <ConfirmActionDialog
        open={pendingDelete !== null}
        title={t("pveClusters.confirmDeleteBackupJobTitle")}
        body={t("pveClusters.confirmDeleteBackupJobBody", { schedule: pendingDelete?.schedule ?? "" })}
        confirmLabel={t("common.delete")}
        danger
        onConfirm={() => {
          if (pendingDelete) deleteMutation.mutate(pendingDelete.id);
          setPendingDelete(null);
        }}
        onCancel={() => setPendingDelete(null)}
      />
      {editingJob !== null && (
        <VzdumpJobFormModal
          clusterId={clusterId}
          job={editingJob === "new" ? null : editingJob}
          onClose={() => setEditingJob(null)}
        />
      )}
    </Panel>
  );
}

function PveClusterDetailModal({
  cluster,
  onClose,
  onSetUpLab,
}: {
  cluster: PveClusterOut;
  onClose: () => void;
  onSetUpLab: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [displayName, setDisplayName] = useState(cluster.display_name);
  const [host, setHost] = useState(cluster.host);
  const [port, setPort] = useState(String(cluster.port));
  const [trustMode, setTrustMode] = useState<TlsTrustMode>(cluster.tls_trust_mode);
  const [fingerprint, setFingerprint] = useState(cluster.tls_fingerprint_sha256 ?? "");
  const [token, setToken] = useState<TokenFormState>(emptyTokenState);
  const [linkedIds, setLinkedIds] = useState<string[]>(cluster.linked_pbs_instance_ids);
  const [linkedDatastoreIds, setLinkedDatastoreIds] = useState<string[]>(cluster.linked_datastore_ids);
  const [isLabCompute, setIsLabCompute] = useState(cluster.is_lab_compute);
  const [labSdnZoneName, setLabSdnZoneName] = useState(cluster.lab_sdn_zone_name ?? "");
  const [labApplianceTemplateVmid, setLabApplianceTemplateVmid] = useState(
    cluster.lab_appliance_template_vmid?.toString() ?? "",
  );
  const [labSnippetStorageId, setLabSnippetStorageId] = useState(cluster.lab_snippet_storage_id ?? "");
  const [labApplianceUplinkBridge, setLabApplianceUplinkBridge] = useState(cluster.lab_appliance_uplink_bridge ?? "");
  const [instantRecoveryEnabled, setInstantRecoveryEnabled] = useState(cluster.instant_recovery_enabled ?? false);
  const [instantRecoveryAlarmHours, setInstantRecoveryAlarmHours] = useState(
    String(cluster.instant_recovery_alarm_hours ?? 24),
  );
  const [error, setError] = useState<string | null>(null);
  const [labAgentToken, setLabAgentToken] = useState<string | null>(null);

  const instancesQuery = useQuery({ queryKey: ["pbs-instances"], queryFn: pbsInstancesApi.list });
  const datastoresQuery = useQuery({ queryKey: ["datastores"], queryFn: datastoresApi.list });

  const rotateLabAgentTokenMutation = useMutation({
    mutationFn: () => pveClustersApi.rotateLabAgentToken(cluster.id),
    onSuccess: (result) => setLabAgentToken(result.token),
  });

  useEffect(() => {
    setEditing(false);
    setDisplayName(cluster.display_name);
    setHost(cluster.host);
    setPort(String(cluster.port));
    setTrustMode(cluster.tls_trust_mode);
    setFingerprint(cluster.tls_fingerprint_sha256 ?? "");
    setToken(emptyTokenState());
    setLinkedIds(cluster.linked_pbs_instance_ids);
    setLinkedDatastoreIds(cluster.linked_datastore_ids);
    setIsLabCompute(cluster.is_lab_compute);
    setLabSdnZoneName(cluster.lab_sdn_zone_name ?? "");
    setLabApplianceTemplateVmid(cluster.lab_appliance_template_vmid?.toString() ?? "");
    setLabSnippetStorageId(cluster.lab_snippet_storage_id ?? "");
    setLabApplianceUplinkBridge(cluster.lab_appliance_uplink_bridge ?? "");
    setInstantRecoveryEnabled(cluster.instant_recovery_enabled ?? false);
    setInstantRecoveryAlarmHours(String(cluster.instant_recovery_alarm_hours ?? 24));
    setError(null);
    setLabAgentToken(null);
  }, [cluster]);

  const probeMutation = useMutation({
    mutationFn: () => pveClustersApi.probeTls(host, parsePort(port) ?? cluster.port),
    onSuccess: (result) => setFingerprint(result.fingerprint_sha256),
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const testMutation = useMutation({
    mutationFn: () => pveClustersApi.testConnection(cluster.id),
    onSettled: () => queryClient.invalidateQueries({ queryKey: QUERY_KEY }),
  });

  const updateMutation = useMutation({
    mutationFn: (validPort: number) =>
      pveClustersApi.update(cluster.id, {
        display_name: displayName,
        host,
        port: validPort,
        tls_trust_mode: trustMode,
        tls_fingerprint_sha256: trustMode === "fingerprint" ? fingerprint : null,
        linked_pbs_instance_ids: linkedIds,
        linked_datastore_ids: linkedDatastoreIds,
        is_lab_compute: isLabCompute,
        lab_sdn_zone_name: labSdnZoneName.trim() === "" ? null : labSdnZoneName.trim(),
        lab_appliance_template_vmid: labApplianceTemplateVmid.trim() === "" ? null : Number(labApplianceTemplateVmid),
        lab_snippet_storage_id: labSnippetStorageId.trim() === "" ? null : labSnippetStorageId.trim(),
        lab_appliance_uplink_bridge: labApplianceUplinkBridge.trim() === "" ? null : labApplianceUplinkBridge.trim(),
        instant_recovery_enabled: instantRecoveryEnabled,
        instant_recovery_alarm_hours: Math.min(720, Math.max(1, Number(instantRecoveryAlarmHours) || 24)),
        ...tokenPayload(token, true),
      }),
    onSuccess: (_updated, validPort) => {
      const connectionChanged =
        host !== cluster.host ||
        validPort !== cluster.port ||
        trustMode !== cluster.tls_trust_mode ||
        (trustMode === "fingerprint" && fingerprint !== (cluster.tls_fingerprint_sha256 ?? "")) ||
        tokenChanged(token, true);
      queryClient.invalidateQueries({ queryKey: QUERY_KEY });
      setEditing(false);
      setToken(emptyTokenState());
      if (connectionChanged) testMutation.mutate();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const instanceNameFor = (id: string) => instancesQuery.data?.find((i) => i.id === id)?.display_name ?? id;
  const datastoreNameFor = (id: string) => datastoresQuery.data?.find((d) => d.id === id)?.name ?? id;

  if (!editing) {
    return (
      <Modal open title={cluster.display_name} onClose={onClose} size="large">
        <dl className={`detailGrid ${localStyles.modalDetails}`}>
          <dt>{t("pveClusters.host")}</dt>
          <dd>
            {cluster.host}:{cluster.port}
          </dd>
          <dt>{t("pbsInstances.trustMode")}</dt>
          <dd>
            {cluster.tls_trust_mode === "fingerprint" ? t("pbsInstances.trustModeFingerprint") : t("pbsInstances.trustModeCa")}
          </dd>
          {cluster.tls_fingerprint_sha256 && (
            <>
              <dt>{t("pbsInstances.fingerprint")}</dt>
              <dd className={styles.fingerprintValue}>{cluster.tls_fingerprint_sha256}</dd>
            </>
          )}
          <dt>{t("connection.tokenId")}</dt>
          <dd>{cluster.token_id ? <code>{cluster.token_id}</code> : "—"}</dd>
          <dt>{t("pveClusters.healthStatus")}</dt>
          <dd>
            <HealthCell status={cluster.last_health_status} checkedAt={cluster.last_health_check_at} secretSet={cluster.token_secret_set !== false} />
          </dd>
          <dt>{t("pveClusters.linkedPbsInstances")}</dt>
          <dd>
            {cluster.linked_pbs_instance_ids.length === 0
              ? "—"
              : cluster.linked_pbs_instance_ids.map(instanceNameFor).join(", ")}
          </dd>
          <dt>{t("pveClusters.linkedDatastores")}</dt>
          <dd>
            {cluster.linked_datastore_ids.length === 0
              ? "—"
              : cluster.linked_datastore_ids.map(datastoreNameFor).join(", ")}
          </dd>
          <dt>{t("pveClusters.labCompute")}</dt>
          <dd>
            <StatusBadge
              tone={cluster.is_lab_compute ? "ok" : "unknown"}
              label={cluster.is_lab_compute ? t("common.statusActive") : t("common.statusDisabled")}
            />
          </dd>
          {cluster.is_lab_compute && (
            <>
              <dt>{t("pveClusters.labSdnZoneName")}</dt>
              <dd>{cluster.lab_sdn_zone_name ?? t("pveClusters.labSdnZoneAutoDefault")}</dd>
              <dt>{t("pveClusters.labApplianceTemplateVmid")}</dt>
              <dd>{cluster.lab_appliance_template_vmid ?? "—"}</dd>
              <dt>{t("pveClusters.labSnippetStorageId")}</dt>
              <dd>{cluster.lab_snippet_storage_id ?? "—"}</dd>
              <dt>{t("pveClusters.labApplianceUplinkBridge")}</dt>
              <dd>{cluster.lab_appliance_uplink_bridge ?? "—"}</dd>
            </>
          )}
        </dl>

        <Panel
          title={t("labSetup.panelTitle")}
          className={localStyles.modalSection}
          actions={
            <button type="button" className="button" onClick={onSetUpLab} disabled={!canSetUpLab(cluster)}>
              {t("labSetup.open")}
            </button>
          }
        >
          <p className="panelIntro">
            {canSetUpLab(cluster) ? t("labSetup.panelIntro") : t("labSetup.needsConnection")}
          </p>
          {cluster.is_lab_compute && <LabReadinessPill cluster={cluster} />}
        </Panel>

        {cluster.is_lab_compute && (
          <Panel
            title={t("pveClusters.labAgentTokenTitle")}
            className={localStyles.modalSection}
            actions={
              !labAgentToken && (
                <button
                  type="button"
                  className="button"
                  disabled={rotateLabAgentTokenMutation.isPending}
                  onClick={() => rotateLabAgentTokenMutation.mutate()}
                >
                  {t("pveClusters.rotateLabAgentToken")}
                </button>
              )
            }
          >
            <p className="panelIntro">{t("pveClusters.labAgentTokenHint")}</p>
            {labAgentToken && (
              <>
                <pre className={styles.fingerprintValue}>{labAgentToken}</pre>
                <InlineAlert tone="info">{t("pveClusters.labAgentTokenShownOnce")}</InlineAlert>
              </>
            )}
          </Panel>
        )}

        <Panel
          title={t("instantRecovery.clusterPanelTitle")}
          className={localStyles.modalSection}
          testId="pve-cluster-instant-recovery"
          actions={
            <button type="button" className="button" onClick={onSetUpLab} disabled={!canSetUpLab(cluster)}>
              {t("instantRecovery.setUpAgent")}
            </button>
          }
        >
          <p className="panelIntro">{t("instantRecovery.clusterPanelIntro")}</p>
          <dl className="detailGrid">
            <dt>{t("instantRecovery.allowed")}</dt>
            <dd>
              <StatusBadge
                tone={cluster.instant_recovery_enabled ? "ok" : "unknown"}
                label={cluster.instant_recovery_enabled ? t("common.statusActive") : t("common.statusDisabled")}
              />
            </dd>
            <dt>{t("instantRecovery.agentNode")}</dt>
            <dd>{cluster.lab_agent_node ?? "—"}</dd>
            <dt>{t("instantRecovery.agentLastSeen")}</dt>
            <dd>{cluster.lab_agent_last_seen_at ? new Date(cluster.lab_agent_last_seen_at).toLocaleString() : t("instantRecovery.agentNever")}</dd>
            <dt>{t("instantRecovery.alarmHours")}</dt>
            <dd>{t("instantRecovery.alarmHoursValue", { hours: cluster.instant_recovery_alarm_hours ?? 24 })}</dd>
          </dl>
        </Panel>

        <ConnectionTestResult
          result={testMutation.data ?? null}
          pending={testMutation.isPending}
          error={testMutation.error ? (testMutation.error as Error).message : null}
        />

        <PveClusterBackupJobsSection clusterId={cluster.id} />
        <div className="formActions">
          <button type="button" className="button" onClick={onClose}>
            {t("common.close")}
          </button>
          <button type="button" className="button" onClick={() => testMutation.mutate()} disabled={testMutation.isPending}>
            {t("pbsInstances.testConnection")}
          </button>
          <button type="button" className="button buttonPrimary" onClick={() => setEditing(true)}>
            {t("common.edit")}
          </button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal open title={cluster.display_name} onClose={onClose} size="large">
      <form
        className={styles.createForm}
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          setError(null);
          const validPort = parsePort(port);
          if (validPort === null) {
            setError(t("connection.portInvalid"));
            return;
          }
          updateMutation.mutate(validPort);
        }}
      >
        {error && (
          <div style={{ gridColumn: "1 / -1" }}>
            <InlineAlert>{error}</InlineAlert>
          </div>
        )}
        <div className="field">
          <label className="label" htmlFor="edit-pve-display-name">
            {t("pbsInstances.displayName")}
          </label>
          <input
            id="edit-pve-display-name"
            className="input"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            required
          />
        </div>
        <div className="field">
          <label className="label" htmlFor="edit-pve-host">
            {t("pveClusters.host")}
          </label>
          <input id="edit-pve-host" className="input" value={host} onChange={(e) => setHost(e.target.value)} required />
        </div>
        <div className="field">
          <label className="label" htmlFor="edit-pve-port">
            {t("pveClusters.port")}
          </label>
          <input
            id="edit-pve-port"
            type="number"
            className="input"
            value={port}
            onChange={(e) => setPort(e.target.value)}
            required
          />
        </div>
        <div className="field">
          <label className="label" htmlFor="edit-pve-trust-mode">
            {t("pbsInstances.trustMode")}
          </label>
          <select
            id="edit-pve-trust-mode"
            className="select"
            value={trustMode}
            onChange={(e) => setTrustMode(e.target.value as TlsTrustMode)}
          >
            <option value="ca">{t("pbsInstances.trustModeCa")}</option>
            <option value="fingerprint">{t("pbsInstances.trustModeFingerprint")}</option>
          </select>
        </div>
        <TokenFields
          product="pve"
          idPrefix="edit-pve"
          state={token}
          onChange={setToken}
          editing
          storedTokenId={cluster.token_id}
        />
        <RequiredPermissionsPanel product="pve" fetcher={pveClustersApi.requiredPrivileges} />
        <div className="field" style={{ gridColumn: "1 / -1" }}>
          <label className="label" htmlFor="edit-pve-linked">
            {t("pveClusters.linkedPbsInstances")}
          </label>
          <select
            id="edit-pve-linked"
            className="select"
            multiple
            value={linkedIds}
            onChange={(e) => setLinkedIds(Array.from(e.target.selectedOptions).map((o) => o.value))}
          >
            {instancesQuery.data?.map((instance) => (
              <option key={instance.id} value={instance.id}>
                {instance.display_name}
              </option>
            ))}
          </select>
        </div>

        <div className="field" style={{ gridColumn: "1 / -1" }}>
          <label className="label" htmlFor="edit-pve-linked-datastores">
            {t("pveClusters.linkedDatastores")}
          </label>
          <select
            id="edit-pve-linked-datastores"
            className="select"
            multiple
            value={linkedDatastoreIds}
            onChange={(e) => setLinkedDatastoreIds(Array.from(e.target.selectedOptions).map((o) => o.value))}
          >
            {datastoresQuery.data?.map((datastore) => (
              <option key={datastore.id} value={datastore.id}>
                {datastore.name}
              </option>
            ))}
          </select>
          <p className="mutedText">{t("pveClusters.linkedDatastoresHint")}</p>
        </div>

        <div className="field" style={{ gridColumn: "1 / -1" }}>
          <label className="label" htmlFor="edit-pve-instant-recovery">
            <input
              id="edit-pve-instant-recovery"
              type="checkbox"
              checked={instantRecoveryEnabled}
              onChange={(e) => setInstantRecoveryEnabled(e.target.checked)}
            />{" "}
            {t("instantRecovery.allowEdit")}
          </label>
          <p className="mutedText">{t("instantRecovery.allowEditHint")}</p>
        </div>
        {instantRecoveryEnabled && (
          <div className="field">
            <label className="label" htmlFor="edit-pve-instant-recovery-hours">
              {t("instantRecovery.alarmHours")}
            </label>
            <input
              id="edit-pve-instant-recovery-hours"
              type="number"
              min={1}
              max={720}
              className="input"
              value={instantRecoveryAlarmHours}
              onChange={(e) => setInstantRecoveryAlarmHours(e.target.value)}
            />
          </div>
        )}

        <div className="field" style={{ gridColumn: "1 / -1" }}>
          <label className="label" htmlFor="edit-pve-lab-compute">
            <input
              id="edit-pve-lab-compute"
              type="checkbox"
              checked={isLabCompute}
              onChange={(e) => setIsLabCompute(e.target.checked)}
            />{" "}
            {t("pveClusters.labCompute")}
          </label>
          <p className="mutedText">{t("pveClusters.labComputeHint")}</p>
        </div>

        {isLabCompute && (
          <>
            <div className="field">
              <label className="label" htmlFor="edit-pve-lab-zone">
                {t("pveClusters.labSdnZoneName")}
              </label>
              <input
                id="edit-pve-lab-zone"
                className="input"
                value={labSdnZoneName}
                onChange={(e) => setLabSdnZoneName(e.target.value)}
                placeholder={t("pveClusters.labSdnZoneAutoDefault")}
              />
            </div>
            <div className="field">
              <label className="label" htmlFor="edit-pve-lab-template-vmid">
                {t("pveClusters.labApplianceTemplateVmid")}
              </label>
              <input
                id="edit-pve-lab-template-vmid"
                type="number"
                min={1}
                className="input"
                value={labApplianceTemplateVmid}
                onChange={(e) => setLabApplianceTemplateVmid(e.target.value)}
              />
            </div>
            <div className="field">
              <label className="label" htmlFor="edit-pve-lab-snippet-storage">
                {t("pveClusters.labSnippetStorageId")}
              </label>
              <input
                id="edit-pve-lab-snippet-storage"
                className="input"
                value={labSnippetStorageId}
                onChange={(e) => setLabSnippetStorageId(e.target.value)}
                placeholder="local"
              />
              <p className="mutedText">{t("pveClusters.labSnippetStorageIdHint")}</p>
            </div>
            <div className="field">
              <label className="label" htmlFor="edit-pve-lab-uplink-bridge">
                {t("pveClusters.labApplianceUplinkBridge")}
              </label>
              <input
                id="edit-pve-lab-uplink-bridge"
                className="input"
                value={labApplianceUplinkBridge}
                onChange={(e) => setLabApplianceUplinkBridge(e.target.value)}
                placeholder="vmbr0"
              />
              <p className="mutedText">{t("pveClusters.labApplianceUplinkBridgeHint")}</p>
            </div>
          </>
        )}

        {trustMode === "fingerprint" && (
          <div className={styles.fingerprintRow}>
            <button
              type="button"
              className="button"
              onClick={() => probeMutation.mutate()}
              disabled={!host || parsePort(port) === null || probeMutation.isPending}
            >
              {t("pbsInstances.probeFingerprint")}
            </button>
            {fingerprint && <span className={styles.fingerprintValue}>{fingerprint}</span>}
          </div>
        )}

        <div className="formActions">
          <button type="button" className="button" onClick={() => setEditing(false)}>
            {t("common.cancel")}
          </button>
          <button
            type="submit"
            className="button buttonPrimary"
            disabled={updateMutation.isPending || (trustMode === "fingerprint" && !fingerprint)}
          >
            {t("common.save")}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function PveClustersPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [pendingDisable, setPendingDisable] = useState<PveClusterOut | null>(null);
  const [selectedCluster, setSelectedCluster] = useState<PveClusterOut | null>(null);
  const [labSetupCluster, setLabSetupCluster] = useState<PveClusterOut | null>(null);

  const query = useQuery({ queryKey: QUERY_KEY, queryFn: pveClustersApi.list });
  const invalidate = () => queryClient.invalidateQueries({ queryKey: QUERY_KEY });

  const [testResult, setTestResult] = useState<Record<string, TestConnectionResponse>>({});
  const testMutation = useMutation({
    mutationFn: (id: string) => pveClustersApi.testConnection(id),
    onSuccess: (result, id) => {
      setTestResult((prev) => ({ ...prev, [id]: result }));
      invalidate();
    },
  });
  const disableMutation = useMutation({ mutationFn: (id: string) => pveClustersApi.disable(id), onSuccess: invalidate });
  const enableMutation = useMutation({ mutationFn: (id: string) => pveClustersApi.enable(id), onSuccess: invalidate });

  const selected = selectedCluster ? query.data?.find((c) => c.id === selectedCluster.id) ?? selectedCluster : null;
  const labSetup = labSetupCluster ? query.data?.find((c) => c.id === labSetupCluster.id) ?? labSetupCluster : null;

  const columns: DataTableColumn<PveClusterOut>[] = [
    { key: "name", header: t("common.name"), render: (row) => row.display_name },
    { key: "host", header: t("pveClusters.host"), render: (row) => `${row.host}:${row.port}` },
    {
      key: "health",
      header: t("pveClusters.healthStatus"),
      render: (row) => <HealthCell status={row.last_health_status} checkedAt={row.last_health_check_at} secretSet={row.token_secret_set !== false} />,
    },
    {
      key: "lab",
      header: t("labSetup.column"),
      render: (row) => <LabReadinessPill cluster={row} />,
    },
    {
      key: "actions",
      header: t("common.actions"),
      render: (row) => (
        <div onClick={(e) => e.stopPropagation()}>
          <div className={styles.rowActions}>
            <button
              type="button"
              className="button"
              onClick={() => testMutation.mutate(row.id)}
              disabled={testMutation.isPending}
            >
              {t("pbsInstances.testConnection")}
            </button>
            <button
              type="button"
              className="button"
              onClick={() => setLabSetupCluster(row)}
              disabled={!canSetUpLab(row)}
              title={canSetUpLab(row) ? undefined : t("labSetup.needsConnection")}
            >
              {t("labSetup.open")}
            </button>
            {row.disabled ? (
              <button type="button" className="button" onClick={() => enableMutation.mutate(row.id)}>
                {t("common.enable")}
              </button>
            ) : (
              <button type="button" className="button buttonDanger" onClick={() => setPendingDisable(row)}>
                {t("common.disable")}
              </button>
            )}
          </div>
          {testResult[row.id]?.error && <div className="mutedText">{testResult[row.id].error}</div>}
        </div>
      ),
    },
  ];

  return (
    <div>
      <PageHeader
        title={t("nav.infrastructurePveTargets")}
        actions={
          !showCreateForm && (
            <button type="button" className="button buttonPrimary" onClick={() => setShowCreateForm(true)}>
              {t("common.create")}
            </button>
          )
        }
      />

      {showCreateForm && <CreatePveClusterForm onDone={() => setShowCreateForm(false)} />}

      <QueryState query={query} isEmpty={(data) => data.length === 0}>
        {(clusters) => (
          <DataTable columns={columns} rows={clusters} rowKey={(row) => row.id} onRowClick={(row) => setSelectedCluster(row)} />
        )}
      </QueryState>

      {selected && (
        <PveClusterDetailModal
          cluster={selected}
          onClose={() => setSelectedCluster(null)}
          onSetUpLab={() => {
            setSelectedCluster(null);
            setLabSetupCluster(selected);
          }}
        />
      )}
      {labSetup && <LabComputeSetup cluster={labSetup} onClose={() => setLabSetupCluster(null)} />}

      <ConfirmActionDialog
        open={pendingDisable !== null}
        title={t("pbsInstances.confirmDisableTitle")}
        body={t("pbsInstances.confirmDisableBody", { name: pendingDisable?.display_name })}
        danger
        confirmLabel={t("common.disable")}
        onCancel={() => setPendingDisable(null)}
        onConfirm={() => {
          if (pendingDisable) disableMutation.mutate(pendingDisable.id);
          setPendingDisable(null);
        }}
      />
    </div>
  );
}
