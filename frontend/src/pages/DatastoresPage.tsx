import { useEffect, useRef, useState, type CSSProperties } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import {
  complianceAttestationsApi,
  customersApi,
  datastoresApi,
  inventoryApi,
  pbsInstancesApi,
  protectionPoliciesApi,
} from "../api/resources";
import type {
  BackupGroupOut,
  BackupSnapshotOut,
  DatastoreMaintenanceMode,
  DatastoreOut,
  DatastoreRole,
  PbsInstanceOut,
} from "../api/types";
import { DatastoreJobsModal } from "../components/DatastoreJobsModal";
import { useSelectedCustomer } from "../contexts/CustomerContext";
import { EmptyState } from "../components/EmptyState";
import { FileBrowserModal } from "../components/FileBrowserModal";
import {
  BoxIcon,
  ChevronDownIcon,
  DatabaseIcon,
  FileIcon,
  FolderIcon,
  FolderOpenIcon,
  HostIcon,
  ServerIcon,
  VmIcon,
} from "../components/icons";
import { InlineAlert } from "../components/InlineAlert";
import { Modal } from "../components/Modal";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { StatusBadge } from "../components/StatusBadge";
import { shortFingerprint } from "../components/EncryptionBadge";
import { formatBytes } from "../lib/formatBytes";
import { protectionStatusPresentation, verifyStatusPresentation } from "../lib/statusPresentation";
import { ApiError } from "../api/client";
import styles from "./DatastoresPage.module.css";

/** Tree depth as a CSS variable, so narrow screens can scale it down
 * (DatastoresPage.module.css `.treeRow`). */
function indent(value: string): CSSProperties {
  return { "--tree-indent": value } as CSSProperties;
}

const DATASTORES_KEY = ["datastores"];
const NAMESPACE_ROOT = "";

function toggle(set: Set<string>, id: string): Set<string> {
  const next = new Set(set);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

function ImportPanel({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [instanceId, setInstanceId] = useState<string>("");

  const instancesQuery = useQuery({ queryKey: ["pbs-instances"], queryFn: pbsInstancesApi.list });
  const availableQuery = useQuery({
    queryKey: ["available-datastores", instanceId],
    queryFn: () => pbsInstancesApi.availableDatastores(instanceId),
    enabled: instanceId !== "",
  });

  const importMutation = useMutation({
    mutationFn: (name: string) => pbsInstancesApi.importDatastore(instanceId, name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: DATASTORES_KEY });
      queryClient.invalidateQueries({ queryKey: ["available-datastores", instanceId] });
    },
  });

  return (
    <Panel
      title={t("datastores.import")}
      className={styles.importPanel}
      actions={
        <button type="button" className="button" onClick={onDone}>
          {t("common.close")}
        </button>
      }
    >
      <p className={`mutedText ${styles.importHint}`}>{t("datastores.importAutoDiscoveryHint")}</p>
      <div className="formGrid">
        <div className="field">
          <label className="label" htmlFor="import-instance">
            {t("datastores.pbsInstance")}
          </label>
          <select
            id="import-instance"
            className="select"
            value={instanceId}
            onChange={(e) => setInstanceId(e.target.value)}
          >
            <option value="">—</option>
            {instancesQuery.data?.map((instance) => (
              <option key={instance.id} value={instance.id}>
                {instance.display_name}
              </option>
            ))}
          </select>
        </div>
      </div>

      {instanceId && availableQuery.data && (
        <div className={styles.availableList}>
          {availableQuery.data.length === 0 ? (
            <p className="mutedText">{t("common.empty")}</p>
          ) : (
            <div>
              <div className="label">{t("datastores.availableToImport")}</div>
              {availableQuery.data.map((remote) => (
                <div key={remote.name} className={styles.availableRow}>
                  <span>{remote.name}</span>
                  <button
                    type="button"
                    className="button buttonPrimary"
                    onClick={() => importMutation.mutate(remote.name)}
                    disabled={importMutation.isPending}
                  >
                    {t("datastores.import")}
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}

function groupByNamespace(groups: BackupGroupOut[]): [string, BackupGroupOut[]][] {
  const map = new Map<string, BackupGroupOut[]>();
  for (const group of groups) {
    const key = group.namespace || NAMESPACE_ROOT;
    const bucket = map.get(key) ?? [];
    bucket.push(group);
    map.set(key, bucket);
  }
  return Array.from(map.entries()).sort(([a], [b]) => a.localeCompare(b));
}

const MAINTENANCE_MODE_OPTIONS: DatastoreMaintenanceMode[] = ["read-only", "offline", "delete", "unmount", "s3-refresh"];

function DatastoreMaintenanceModal({
  datastore,
  currentMode,
  currentMessage,
  onClose,
}: {
  datastore: DatastoreOut;
  currentMode: DatastoreMaintenanceMode | null;
  currentMessage: string | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<DatastoreMaintenanceMode | "">(currentMode ?? "");
  const [message, setMessage] = useState(currentMessage ?? "");
  const [error, setError] = useState<string | null>(null);
  // Instant Recovery (ADR 0030 §6): VMs running from backups on this datastore,
  // returned by a 409 `instant_recovery_active`; saving again confirms it.
  const [instantRecoveryVms, setInstantRecoveryVms] = useState<{ name: string; vmid: number; node: string }[] | null>(
    null,
  );

  const saveMutation = useMutation({
    mutationFn: () =>
      pbsInstancesApi.setMaintenanceMode(datastore.pbs_instance_id, datastore.name, {
        mode: mode || null,
        message: mode ? message || null : null,
        confirm_instant_recovery: instantRecoveryVms !== null,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["datastore-maintenance", datastore.pbs_instance_id, datastore.name] });
      onClose();
    },
    onError: (err) => {
      if (err instanceof ApiError && err.code === "instant_recovery_active") {
        const plans = (err.details as { restore_plans?: { name: string; vmid: number; node: string }[] } | undefined)
          ?.restore_plans;
        setInstantRecoveryVms(plans ?? []);
        setError(null);
        return;
      }
      setError(err instanceof ApiError ? err.message : t("common.loadError"));
    },
  });

  return (
    <Modal open title={t("datastores.maintenanceModalTitle", { name: datastore.name })} onClose={onClose}>
      {instantRecoveryVms !== null && (
        <div className={styles.alert}>
          <InlineAlert tone="danger">
            {t("instantRecovery.maintenanceWarning", {
              vms: instantRecoveryVms.map((vm) => `${vm.name} (VM ${vm.vmid}, ${vm.node})`).join(", "),
            })}
          </InlineAlert>
        </div>
      )}
      {error && (
        <div className={styles.alert}>
          <InlineAlert>{error}</InlineAlert>
        </div>
      )}
      <div className="field">
        <label className="label" htmlFor="maintenance-mode-select">
          {t("datastores.maintenanceMode")}
        </label>
        <select
          id="maintenance-mode-select"
          className="select"
          value={mode}
          onChange={(e) => setMode(e.target.value as DatastoreMaintenanceMode | "")}
        >
          <option value="">{t("datastores.maintenanceModeNone")}</option>
          {MAINTENANCE_MODE_OPTIONS.map((option) => (
            <option key={option} value={option}>
              {t(`datastores.maintenanceModeOptions.${option}`, option)}
            </option>
          ))}
        </select>
      </div>
      {mode && (
        <div className="field">
          <label className="label" htmlFor="maintenance-message">
            {t("datastores.maintenanceMessage")}
          </label>
          <input
            id="maintenance-message"
            className="input"
            value={message}
            onChange={(e) => setMessage(e.target.value)}
          />
        </div>
      )}
      <div className="formActions">
        <button type="button" className="button" onClick={onClose} disabled={saveMutation.isPending}>
          {t("common.cancel")}
        </button>
        <button
          type="button"
          className={instantRecoveryVms !== null ? "button buttonDanger" : "button buttonPrimary"}
          onClick={() => saveMutation.mutate()}
          disabled={saveMutation.isPending}
        >
          {instantRecoveryVms !== null ? t("instantRecovery.maintenanceConfirm") : t("common.save")}
        </button>
      </div>
    </Modal>
  );
}

function DatastoreAttestationModal({ datastore, onClose }: { datastore: DatastoreOut; onClose: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [immutable, setImmutable] = useState(true);
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);

  const historyQuery = useQuery({
    queryKey: ["datastore-compliance-attestations", datastore.id],
    queryFn: () => complianceAttestationsApi.listForDatastore(datastore.id),
  });
  const current = historyQuery.data?.[0] ?? null;

  const saveMutation = useMutation({
    mutationFn: () =>
      complianceAttestationsApi.createForDatastore(datastore.id, {
        dimension: "immutable_copy", value: immutable, notes: notes || undefined,
      }),
    onSuccess: () => {
      setError(null);
      setNotes("");
      queryClient.invalidateQueries({ queryKey: ["datastore-compliance-attestations", datastore.id] });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  return (
    <Modal open title={t("datastores.attestationModalTitle", { name: datastore.name })} onClose={onClose}>
      {error && (
        <div className={styles.alert}>
          <InlineAlert>{error}</InlineAlert>
        </div>
      )}
      <p className="mutedText">{t("datastores.attestationIntro")}</p>
      <div className="field">
        {current ? (
          <StatusBadge
            tone={current.value === true ? "ok" : "danger"}
            label={current.value === true ? t("datastores.attestationImmutable") : t("datastores.attestationNotImmutable")}
          />
        ) : (
          <StatusBadge tone="unknown" label={t("datastores.attestationNotAttested")} />
        )}
        {current && (
          <p className="mutedText">
            {t("recoveryEvidence.attestedBy", {
              name: current.asserted_by_display_name,
              when: new Date(current.created_at).toLocaleString(),
            })}
          </p>
        )}
      </div>

      <div className="field">
        <label className="label" htmlFor="attestation-value">
          {t("datastores.attestationNewValue")}
        </label>
        <select
          id="attestation-value"
          className="select"
          value={immutable ? "yes" : "no"}
          onChange={(e) => setImmutable(e.target.value === "yes")}
        >
          <option value="yes">{t("datastores.attestationImmutable")}</option>
          <option value="no">{t("datastores.attestationNotImmutable")}</option>
        </select>
      </div>
      <div className="field">
        <label className="label" htmlFor="attestation-notes">
          {t("datastores.attestationNotes")}
        </label>
        <input id="attestation-notes" className="input" value={notes} onChange={(e) => setNotes(e.target.value)} />
      </div>
      <div className="formActions">
        <button type="button" className="button" onClick={onClose}>
          {t("common.close")}
        </button>
        <button type="button" className="button buttonPrimary" onClick={() => saveMutation.mutate()} disabled={saveMutation.isPending}>
          {t("datastores.attestationRecord")}
        </button>
      </div>

      {historyQuery.data && historyQuery.data.length > 0 && (
        <div className={styles.attestationHistory}>
          <div className="label">{t("datastores.attestationHistory")}</div>
          {historyQuery.data.map((row) => (
            <div key={row.id} className="mutedText">
              {row.value === true ? t("datastores.attestationImmutable") : t("datastores.attestationNotImmutable")} —{" "}
              {row.asserted_by_display_name}, {new Date(row.created_at).toLocaleString()}
              {row.notes ? ` — ${row.notes}` : ""}
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}

export function DatastoresPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { selectedCustomerId } = useSelectedCustomer();
  const [showImportPanel, setShowImportPanel] = useState(false);

  const [expandedInstances, setExpandedInstances] = useState<Set<string>>(new Set());
  const [expandedDatastores, setExpandedDatastores] = useState<Set<string>>(new Set());
  const [expandedNamespaces, setExpandedNamespaces] = useState<Set<string>>(new Set());
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  const [groupFilters, setGroupFilters] = useState<Record<string, string>>({});
  const [browsingSnapshot, setBrowsingSnapshot] = useState<{
    groupId: string;
    snapshotId: string;
    title: string;
  } | null>(null);
  const [maintenanceTarget, setMaintenanceTarget] = useState<DatastoreOut | null>(null);
  const [attestationTarget, setAttestationTarget] = useState<DatastoreOut | null>(null);
  const [jobsTarget, setJobsTarget] = useState<DatastoreOut | null>(null);

  const instancesQuery = useQuery({ queryKey: ["pbs-instances"], queryFn: pbsInstancesApi.list });

  // A lone PBS instance opens by itself (2026-10-04) -- a fully collapsed
  // tree with a single top-level node is just an extra click. Only once,
  // so collapsing it again sticks; deeper levels stay closed because each
  // one fetches from the PBS when opened.
  const autoExpandedRef = useRef(false);
  useEffect(() => {
    const instances = instancesQuery.data;
    if (autoExpandedRef.current || !instances) return;
    autoExpandedRef.current = true;
    if (instances.length === 1) setExpandedInstances(new Set([instances[0].id]));
  }, [instancesQuery.data]);
  const datastoresQuery = useQuery({ queryKey: DATASTORES_KEY, queryFn: datastoresApi.list });
  const customersQuery = useQuery({ queryKey: ["customers"], queryFn: customersApi.list });
  const policiesQuery = useQuery({ queryKey: ["protection-policies"], queryFn: protectionPoliciesApi.list });

  const allDatastores = datastoresQuery.data ?? [];
  const maintenanceQueries = useQueries({
    queries: allDatastores.map((ds) => ({
      queryKey: ["datastore-maintenance", ds.pbs_instance_id, ds.name],
      queryFn: () => pbsInstancesApi.getMaintenanceMode(ds.pbs_instance_id, ds.name),
      // A datastore PBS no longer lists has no maintenance mode to read.
      enabled: !ds.removed_from_pbs_at,
    })),
  });
  const maintenanceByDatastoreId = new Map<string, (typeof maintenanceQueries)[number]>();
  allDatastores.forEach((ds, index) => maintenanceByDatastoreId.set(ds.id, maintenanceQueries[index]));

  const expandedDatastoreIds = Array.from(expandedDatastores);
  const groupsQueries = useQueries({
    queries: expandedDatastoreIds.map((id) => ({
      queryKey: ["backup-groups", id],
      queryFn: () => datastoresApi.backupGroups(id),
    })),
  });
  const groupsByDatastore = new Map<string, (typeof groupsQueries)[number]>();
  expandedDatastoreIds.forEach((id, index) => groupsByDatastore.set(id, groupsQueries[index]));

  const expandedGroupIds = Array.from(expandedGroups);
  const snapshotsQueries = useQueries({
    queries: expandedGroupIds.map((id) => ({
      queryKey: ["snapshots", id],
      queryFn: () => inventoryApi.snapshots(id),
    })),
  });
  const snapshotsByGroup = new Map<string, (typeof snapshotsQueries)[number]>();
  expandedGroupIds.forEach((id, index) => snapshotsByGroup.set(id, snapshotsQueries[index]));

  const assignDatastoreMutation = useMutation({
    mutationFn: ({ id, customerId, role }: { id: string; customerId: string; role?: DatastoreRole }) =>
      customerId ? datastoresApi.assignCustomer(id, customerId, role) : datastoresApi.unassignCustomer(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: DATASTORES_KEY }),
  });
  const assignGroupCustomerMutation = useMutation({
    mutationFn: ({ groupId, customerId }: { groupId: string; datastoreId: string; customerId: string }) =>
      customerId ? inventoryApi.assignCustomer(groupId, customerId) : inventoryApi.unassignCustomer(groupId),
    onSuccess: (_data, variables) =>
      queryClient.invalidateQueries({ queryKey: ["backup-groups", variables.datastoreId] }),
  });
  const assignPolicyMutation = useMutation({
    mutationFn: ({ groupId, policyId }: { groupId: string; datastoreId: string; policyId: string }) =>
      policyId ? inventoryApi.assignProtectionPolicy(groupId, policyId) : inventoryApi.unassignProtectionPolicy(groupId),
    onSuccess: (_data, variables) =>
      queryClient.invalidateQueries({ queryKey: ["backup-groups", variables.datastoreId] }),
  });

  const customerNameFor = (id: string | null) => (id ? customersQuery.data?.find((c) => c.id === id)?.name ?? id : null);

  if (instancesQuery.isLoading || datastoresQuery.isLoading) {
    return (
      <div>
        <PageHeader title={t("datastores.title")} />
        <p className="mutedText">{t("common.loading")}</p>
      </div>
    );
  }

  if (instancesQuery.isError || datastoresQuery.isError) {
    const error = instancesQuery.error ?? datastoresQuery.error;
    if (error instanceof ApiError && error.status === 403) {
      return (
        <div>
          <PageHeader title={t("datastores.title")} />
          <EmptyState title={t("common.forbidden")} description={t("common.forbiddenDescription")} />
        </div>
      );
    }
    return (
      <div>
        <PageHeader title={t("datastores.title")} />
        <InlineAlert tone="danger">{t("common.loadError")}</InlineAlert>
      </div>
    );
  }

  // Each Datastore is customer-scoped by design (manifest §9.1) -- when a
  // customer is selected platform-wide, narrow the tree down to just
  // their datastores (and the PBS instances that host at least one),
  // rather than showing every tenant's infrastructure at once.
  const datastores = selectedCustomerId
    ? (datastoresQuery.data ?? []).filter((d) => d.customer_id === selectedCustomerId)
    : datastoresQuery.data ?? [];
  const datastoresByInstance = new Map<string, DatastoreOut[]>();
  for (const datastore of datastores) {
    const bucket = datastoresByInstance.get(datastore.pbs_instance_id) ?? [];
    bucket.push(datastore);
    datastoresByInstance.set(datastore.pbs_instance_id, bucket);
  }
  const instances = selectedCustomerId
    ? (instancesQuery.data ?? []).filter((instance) => datastoresByInstance.has(instance.id))
    : instancesQuery.data ?? [];

  const renderSnapshotRow = (snapshot: BackupSnapshotOut, groupId: string) => {
    const { tone, label } = verifyStatusPresentation(snapshot.verify_status, t);
    const snapshotLabel = new Date(snapshot.snapshot_time).toLocaleString();
    return (
      <div key={snapshot.id} className={styles.treeRow} style={indent("5.5rem")}>
        <span className={styles.treeSpacer} />
        <FileIcon className={styles.treeIcon} />
        <span className={styles.treeLabel}>{snapshotLabel}</span>
        <span className={styles.treeMeta}>
          <span className="mutedText">{formatBytes(snapshot.size_bytes)}</span>
          <StatusBadge tone={tone} label={label} />
          <button
            type="button"
            className={styles.treeToggle}
            aria-label={t("fileRestore.browseFiles")}
            title={t("fileRestore.browseFiles")}
            onClick={() => setBrowsingSnapshot({ groupId, snapshotId: snapshot.id, title: snapshotLabel })}
          >
            <FolderOpenIcon />
          </button>
        </span>
      </div>
    );
  };

  const renderGroupRow = (group: BackupGroupOut, datastoreId: string, datastoreCustomerId: string | null) => {
    const isExpanded = expandedGroups.has(group.id);
    const { tone, label } = protectionStatusPresentation(group.protection_status, t);
    const eligiblePolicies = policiesQuery.data?.filter((p) => p.customer_id === group.effective_customer_id) ?? [];
    const snapshotsResult = snapshotsByGroup.get(group.id);

    return (
      <div key={group.id}>
        <div className={styles.treeRow} style={indent("4rem")}>
          <button
            type="button"
            className={styles.treeToggle}
            aria-label={isExpanded ? t("common.collapse") : t("common.expand")}
            onClick={() => setExpandedGroups((prev) => toggle(prev, group.id))}
          >
            <ChevronDownIcon className={isExpanded ? styles.chevronOpen : styles.chevronClosed} />
          </button>
          {group.backup_type === "vm" ? (
            <VmIcon className={styles.treeIcon} />
          ) : group.backup_type === "host" ? (
            <HostIcon className={styles.treeIcon} />
          ) : (
            <BoxIcon className={styles.treeIcon} />
          )}
          <span className={styles.typeTag}>{group.backup_type.toUpperCase()}</span>
          <span className={styles.treeLabel}>
            {group.resolved_name ? `${group.resolved_name} (${group.backup_id})` : group.backup_id}
          </span>
          <span className={styles.treeMeta}>
            <StatusBadge tone={tone} label={label} />
            {group.owner && (
              <span className="mutedText" title={t("backupGroups.ownerHint")}>
                {t("backupGroups.owner")}: {group.owner}
              </span>
            )}
            <select
              className="select"
              value={group.customer_id ?? ""}
              onChange={(e) =>
                assignGroupCustomerMutation.mutate({ groupId: group.id, datastoreId, customerId: e.target.value })
              }
            >
              <option value="">
                {datastoreCustomerId
                  ? t("backupGroups.inheritedFrom", { name: customerNameFor(datastoreCustomerId) })
                  : t("datastores.unassigned")}
              </option>
              {customersQuery.data?.map((customer) => (
                <option key={customer.id} value={customer.id}>
                  {customer.name}
                </option>
              ))}
            </select>
            <select
              className="select"
              value={group.protection_policy_id ?? ""}
              disabled={!group.effective_customer_id}
              onChange={(e) =>
                assignPolicyMutation.mutate({ groupId: group.id, datastoreId, policyId: e.target.value })
              }
            >
              <option value="">{t("protectionPolicies.none")}</option>
              {eligiblePolicies.map((policy) => (
                <option key={policy.id} value={policy.id}>
                  {policy.name}
                </option>
              ))}
            </select>
          </span>
        </div>
        {isExpanded && (
          <div>
            {snapshotsResult?.isLoading && (
              <p className={`mutedText ${styles.treeLoading}`}>{t("common.loading")}</p>
            )}
            {snapshotsResult?.data?.length === 0 && (
              <p className={`mutedText ${styles.treeLoading}`}>{t("common.empty")}</p>
            )}
            {snapshotsResult?.data?.map((snapshot) => renderSnapshotRow(snapshot, group.id))}
          </div>
        )}
      </div>
    );
  };

  const groupMatchesFilter = (group: BackupGroupOut, term: string) =>
    [group.backup_type, group.backup_id, group.resolved_name ?? ""].join(" ").toLowerCase().includes(term);

  const renderDatastoreRow = (datastore: DatastoreOut) => {
    const isExpanded = expandedDatastores.has(datastore.id);
    const groupsResult = groupsByDatastore.get(datastore.id);
    const namespaces = groupsResult?.data ? groupByNamespace(groupsResult.data) : [];
    const maintenanceMode = maintenanceByDatastoreId.get(datastore.id)?.data?.mode ?? null;
    // Marked by the Collector's automatic discovery when PBS no longer
    // lists it -- kept visible (muted) rather than hidden, since its
    // assignment and history still exist; PBS-side actions are hidden.
    const removedFromPbs = Boolean(datastore.removed_from_pbs_at);

    return (
      <div key={datastore.id} data-testid={`datastore-row-${datastore.name}`}>
        <div
          className={`${styles.treeRow} ${removedFromPbs ? styles.treeRowRemoved : ""}`}
          style={indent("2.5rem")}
        >
          <button
            type="button"
            className={styles.treeToggle}
            aria-label={isExpanded ? t("common.collapse") : t("common.expand")}
            onClick={() => setExpandedDatastores((prev) => toggle(prev, datastore.id))}
          >
            <ChevronDownIcon className={isExpanded ? styles.chevronOpen : styles.chevronClosed} />
          </button>
          <DatabaseIcon className={styles.treeIcon} />
          <span className={styles.treeLabel}>{datastore.name}</span>
          <span className={styles.treeMeta}>
            {datastore.backend_type === "s3" && (
              <span
                title={t(datastore.s3_offsite ? "datastores.s3HintOffsite" : "datastores.s3HintOnSite", {
                  bucket: datastore.s3_bucket ?? "?",
                  endpoint: datastore.s3_endpoint_host ?? datastore.s3_endpoint_name ?? "?",
                })}
              >
                <StatusBadge tone="info" label="S3" />
              </span>
            )}
            {removedFromPbs && (
              <span
                title={t("datastores.removedFromPbsHint", {
                  date: new Date(datastore.removed_from_pbs_at!).toLocaleString(),
                })}
              >
                <StatusBadge tone="unknown" label={t("datastores.removedFromPbs")} />
              </span>
            )}
            <select
              className="select"
              value={datastore.customer_id ?? ""}
              onChange={(e) =>
                assignDatastoreMutation.mutate({ id: datastore.id, customerId: e.target.value })
              }
            >
              <option value="">{t("datastores.unassigned")}</option>
              {customersQuery.data?.map((customer) => (
                <option key={customer.id} value={customer.id}>
                  {customer.name}
                </option>
              ))}
            </select>
            {datastore.customer_id && (
              <select
                className="select"
                value={datastore.role ?? "primary"}
                onChange={(e) =>
                  assignDatastoreMutation.mutate({
                    id: datastore.id,
                    customerId: datastore.customer_id!,
                    role: e.target.value as DatastoreRole,
                  })
                }
              >
                <option value="primary">{t("datastores.rolePrimary")}</option>
                <option value="offsite">{t("datastores.roleOffsite")}</option>
              </select>
            )}
            {maintenanceMode && (
              <StatusBadge
                tone="warning"
                label={t("datastores.maintenanceActive", {
                  mode: t(`datastores.maintenanceModeOptions.${maintenanceMode}`, maintenanceMode),
                })}
              />
            )}
            {!removedFromPbs && (
              <button type="button" className="button" onClick={() => setMaintenanceTarget(datastore)}>
                {t("datastores.maintenance")}
              </button>
            )}
            <button type="button" className="button" onClick={() => setAttestationTarget(datastore)}>
              {t("datastores.attestation")}
            </button>
            {!removedFromPbs && (
              <button type="button" className="button" onClick={() => setJobsTarget(datastore)}>
                {t("datastores.jobs")}
              </button>
            )}
            {(datastore.key_fingerprints_in_use?.length ?? 0) > 0 && (
              <span
                title={`${t("encryption.keysInUseHint", { count: datastore.encrypted_snapshot_count ?? 0 })}\n${(
                  datastore.key_fingerprints_in_use ?? []
                ).join("\n")}`}
              >
                <StatusBadge
                  tone="info"
                  label={`${t("encryption.keysInUse")}: ${(datastore.key_fingerprints_in_use ?? []).map(shortFingerprint).join(", ")}`}
                />
              </span>
            )}
            {(datastore.keys_missing?.length ?? 0) > 0 && (
              <span title={`${t("encryptionKeys.datastoreKeysMissingHint")}\n${(datastore.keys_missing ?? []).join("\n")}`}>
                <StatusBadge
                  tone="danger"
                  label={t("encryptionKeys.datastoreKeysMissing", { count: datastore.keys_missing?.length ?? 0 })}
                />
              </span>
            )}
          </span>
        </div>
        {isExpanded && (
          <div>
            {groupsResult?.isLoading && <p className={`mutedText ${styles.treeLoading}`}>{t("common.loading")}</p>}
            {groupsResult?.data?.length === 0 && (
              <p className={`mutedText ${styles.treeLoading}`}>{t("common.empty")}</p>
            )}
            {namespaces.map(([namespace, groups]) => {
              const namespaceKey = `${datastore.id}::${namespace}`;
              const namespaceExpanded = expandedNamespaces.has(namespaceKey);
              const filterTerm = (groupFilters[namespaceKey] ?? "").trim().toLowerCase();
              const visibleGroups =
                filterTerm === "" ? groups : groups.filter((group) => groupMatchesFilter(group, filterTerm));
              return (
                <div key={namespaceKey}>
                  <div className={styles.treeRow} style={indent("3.25rem")}>
                    <button
                      type="button"
                      className={styles.treeToggle}
                      aria-label={namespaceExpanded ? t("common.collapse") : t("common.expand")}
                      onClick={() => setExpandedNamespaces((prev) => toggle(prev, namespaceKey))}
                    >
                      <ChevronDownIcon className={namespaceExpanded ? styles.chevronOpen : styles.chevronClosed} />
                    </button>
                    <FolderIcon className={styles.treeIcon} />
                    <span className={styles.treeLabel}>{namespace || "/"}</span>
                    <span className={styles.treeMeta}>
                      <span className="mutedText">
                        {t("backupGroups.title")}: {groups.length}
                      </span>
                    </span>
                  </div>
                  {namespaceExpanded && (
                    <>
                      <div className={styles.groupFilterRow}>
                        <input
                          type="text"
                          className="input"
                          value={groupFilters[namespaceKey] ?? ""}
                          onChange={(e) =>
                            setGroupFilters((prev) => ({ ...prev, [namespaceKey]: e.target.value }))
                          }
                          placeholder={t("backupGroups.filterPlaceholder")}
                          aria-label={t("backupGroups.filterPlaceholder")}
                        />
                      </div>
                      {visibleGroups.length === 0 ? (
                        <p className={`mutedText ${styles.treeLoading}`}>{t("backupGroups.noFilterMatches")}</p>
                      ) : (
                        visibleGroups.map((group) => renderGroupRow(group, datastore.id, datastore.customer_id))
                      )}
                    </>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    );
  };

  const renderInstanceRow = (instance: PbsInstanceOut) => {
    const isExpanded = expandedInstances.has(instance.id);
    const instanceDatastores = datastoresByInstance.get(instance.id) ?? [];

    const toggleExpanded = () => setExpandedInstances((prev) => toggle(prev, instance.id));

    return (
      <div key={instance.id}>
        {/* A real UX-audit finding (2026-09-23): this row visually invited
            a click across its full width (hover highlight, a chevron
            affordance) but only the small chevron icon itself actually
            responded -- clicking the label or the host text did nothing.
            The row now toggles too; the button still stops propagation
            so a click on it doesn't fire the handler twice. */}
        <div
          className={`${styles.treeRow} ${styles.treeRowClickable}`}
          style={indent("0.75rem")}
          onClick={toggleExpanded}
        >
          <button
            type="button"
            className={styles.treeToggle}
            aria-label={isExpanded ? t("common.collapse") : t("common.expand")}
            onClick={(e) => {
              e.stopPropagation();
              toggleExpanded();
            }}
          >
            <ChevronDownIcon className={isExpanded ? styles.chevronOpen : styles.chevronClosed} />
          </button>
          <ServerIcon className={styles.treeIcon} />
          <span className={styles.treeLabel}>{instance.display_name}</span>
          <span className={styles.treeMeta}>
            <span className="mutedText">
              {instance.host}:{instance.port}
            </span>
          </span>
        </div>
        {isExpanded && (
          <div>
            {instanceDatastores.length === 0 ? (
              <p className={`mutedText ${styles.treeLoading}`}>{t("common.empty")}</p>
            ) : (
              instanceDatastores.map(renderDatastoreRow)
            )}
          </div>
        )}
      </div>
    );
  };

  return (
    <div>
      <PageHeader
        title={t("datastores.title")}
        actions={
          !showImportPanel && (
            <button type="button" className="button buttonPrimary" onClick={() => setShowImportPanel(true)}>
              {t("datastores.import")}
            </button>
          )
        }
      />

      {showImportPanel && <ImportPanel onDone={() => setShowImportPanel(false)} />}

      {instances.length === 0 ? (
        <EmptyState title={t("common.empty")} />
      ) : (
        <Panel padded={false} className={styles.tree}>
          {instances.map(renderInstanceRow)}
        </Panel>
      )}

      <FileBrowserModal
        open={browsingSnapshot !== null}
        onClose={() => setBrowsingSnapshot(null)}
        groupId={browsingSnapshot?.groupId ?? ""}
        snapshotId={browsingSnapshot?.snapshotId ?? ""}
        title={browsingSnapshot ? t("fileRestore.title", { snapshot: browsingSnapshot.title }) : ""}
      />

      {maintenanceTarget && (
        <DatastoreMaintenanceModal
          datastore={maintenanceTarget}
          currentMode={maintenanceByDatastoreId.get(maintenanceTarget.id)?.data?.mode ?? null}
          currentMessage={maintenanceByDatastoreId.get(maintenanceTarget.id)?.data?.message ?? null}
          onClose={() => setMaintenanceTarget(null)}
        />
      )}

      {attestationTarget && (
        <DatastoreAttestationModal datastore={attestationTarget} onClose={() => setAttestationTarget(null)} />
      )}

      {jobsTarget && <DatastoreJobsModal datastore={jobsTarget} onClose={() => setJobsTarget(null)} />}
    </div>
  );
}
