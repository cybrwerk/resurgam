import { useEffect, useId, useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { applicationGroupsApi, customersApi, datastoresApi, pveClustersApi, restoreTestPlansApi } from "../api/resources";
import type {
  ApplicationGroupMemberIn,
  ApplicationGroupOut,
  BackupGroupOut,
  CheckType,
  LabNetworkMode,
  RestoreTestPlanOut,
} from "../api/types";
import { ConfirmActionDialog } from "../components/ConfirmActionDialog";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { ChevronDownIcon } from "../components/icons";
import { InlineAlert } from "../components/InlineAlert";
import { Modal } from "../components/Modal";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { QueryState } from "../components/QueryState";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { TabPanel, Tabs, useTabParam } from "../components/Tabs";
import styles from "./RecoveryLabPage.module.css";

const RUNS_QUERY_KEY = ["lab-runs"];
const GROUPS_QUERY_KEY = ["application-groups"];

/** Manifest §22.6's checks, always presented as this 3-tier escalation
 * (guest agent -> pingable -> a real scripted service test), not a flat
 * bag of independently-orderable options -- important for real
 * usability of the feature (user guidance, 2026-09-19; see ADR 0019).
 */
const CHECK_TIERS: { labelKey: string; types: CheckType[] }[] = [
  { labelKey: "recoveryLab.checkTierGuestAgent", types: ["guest_agent"] },
  { labelKey: "recoveryLab.checkTierPingable", types: ["icmp", "arp"] },
  {
    labelKey: "recoveryLab.checkTierScripted",
    types: ["tcp_port", "http", "https", "dns", "tls_handshake", "ssh_banner", "nmap_scan"],
  },
];

function runStatusTone(status: string): StatusTone {
  switch (status) {
    case "passed":
    case "completed":
      return "ok";
    case "partial":
      return "warning";
    case "failed":
      return "danger";
    case "planned":
      return "unknown";
    default:
      return "info"; // running, cleaning_up
  }
}

function memberStatusTone(status: string): StatusTone {
  switch (status) {
    case "passed":
      return "ok";
    case "failed":
      return "danger";
    case "pending":
      return "unknown";
    default:
      return "info";
  }
}

/** No single "backup groups for this customer" endpoint exists yet (same
 * real gap `SnapshotPicker` documents) -- fetches every datastore's
 * groups once a customer is picked and filters client-side. Recovery Lab
 * members are VM-only for now (backend rejects CT members outright), so
 * this filters to `backup_type === "vm"` up front rather than surfacing
 * a pick that will always fail server-side.
 */
function useCustomerVmBackupGroups(customerId: string): { groups: BackupGroupOut[]; isLoading: boolean } {
  const datastoresQuery = useQuery({ queryKey: ["datastores"], queryFn: datastoresApi.list });
  const datastoreIds = datastoresQuery.data?.map((d) => d.id) ?? [];
  const groupsQueries = useQueries({
    queries:
      customerId === ""
        ? []
        : datastoreIds.map((id) => ({
            queryKey: ["backup-groups", id],
            queryFn: () => datastoresApi.backupGroups(id),
          })),
  });
  const isLoading = customerId !== "" && groupsQueries.some((q) => q.isLoading);
  const groups = (customerId === "" ? [] : groupsQueries.flatMap((q) => q.data ?? [])).filter(
    (group) => group.effective_customer_id === customerId && group.backup_type === "vm",
  );
  return { groups, isLoading };
}

function MemberRowEditor({
  member,
  groups,
  groupsLoading,
  superseded,
  onChange,
  onRemove,
}: {
  member: ApplicationGroupMemberIn;
  groups: BackupGroupOut[];
  groupsLoading: boolean;
  superseded?: boolean;
  onChange: (next: ApplicationGroupMemberIn) => void;
  onRemove: () => void;
}) {
  const { t } = useTranslation();
  const groupFieldId = useId();
  const orderFieldId = useId();
  const expectedIpFieldId = useId();

  const toggleCheck = (type: CheckType) => {
    const has = member.check_types.includes(type);
    onChange({
      ...member,
      check_types: has ? member.check_types.filter((c) => c !== type) : [...member.check_types, type],
    });
  };

  return (
    <div className={styles.memberRow}>
      {superseded && (
        <div style={{ gridColumn: "1 / -1" }}>
          <InlineAlert tone="info">{t("recoveryLab.memberBackupSourceSuperseded")}</InlineAlert>
        </div>
      )}
      <div className="field">
        <label className="label" htmlFor={groupFieldId}>
          {t("recoveryLab.member")}
        </label>
        <select
          id={groupFieldId}
          className="select"
          value={member.backup_group_id}
          disabled={groupsLoading}
          onChange={(e) => onChange({ ...member, backup_group_id: e.target.value })}
        >
          <option value="">—</option>
          {groups.map((group) => (
            <option key={group.id} value={group.id}>
              {group.resolved_name ? `${group.resolved_name} (${group.backup_id})` : group.backup_id}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label className="label" htmlFor={orderFieldId}>
          {t("recoveryLab.startupOrder")}
        </label>
        <input
          id={orderFieldId}
          type="number"
          className="input"
          value={member.startup_order}
          onChange={(e) => onChange({ ...member, startup_order: Number(e.target.value) })}
        />
      </div>
      <div className="field">
        <label className="label" htmlFor={expectedIpFieldId}>
          {t("recoveryLab.expectedIp")}
        </label>
        <input
          id={expectedIpFieldId}
          className="input"
          placeholder={t("recoveryLab.expectedIpPlaceholder")}
          value={member.expected_ip ?? ""}
          onChange={(e) => onChange({ ...member, expected_ip: e.target.value.trim() === "" ? null : e.target.value })}
        />
      </div>
      <div className={styles.checkTiers}>
        {CHECK_TIERS.map((tier) => (
          <fieldset key={tier.labelKey} className={styles.checkTier}>
            <legend>{t(tier.labelKey)}</legend>
            {tier.types.map((type) => (
              <label key={type}>
                <input type="checkbox" checked={member.check_types.includes(type)} onChange={() => toggleCheck(type)} />{" "}
                {t(`recoveryLab.checkType_${type}`)}
              </label>
            ))}
          </fieldset>
        ))}
      </div>
      <div style={{ gridColumn: "1 / -1" }}>
        <button type="button" className="button buttonDanger" onClick={onRemove}>
          {t("common.delete")}
        </button>
      </div>
    </div>
  );
}

function ApplicationGroupFormModal({
  group,
  onClose,
}: {
  group: ApplicationGroupOut | "new";
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const isNew = group === "new";

  const customersQuery = useQuery({ queryKey: ["customers"], queryFn: customersApi.list });
  const [customerId, setCustomerId] = useState(isNew ? "" : group.customer_id);
  const [name, setName] = useState(isNew ? "" : group.name);
  const [members, setMembers] = useState<ApplicationGroupMemberIn[]>(
    isNew
      ? []
      : group.members.map((m) => ({
          backup_group_id: m.backup_group_id,
          startup_order: m.startup_order,
          check_types: m.check_types as CheckType[],
          expected_ip: m.expected_ip,
        })),
  );
  const [labNetworkMode, setLabNetworkMode] = useState<LabNetworkMode>(isNew ? "dhcp" : (group.lab_network_mode as LabNetworkMode));
  const [labSubnetCidr, setLabSubnetCidr] = useState(isNew ? "" : (group.lab_subnet_cidr ?? ""));
  const [labGatewayIp, setLabGatewayIp] = useState(isNew ? "" : (group.lab_gateway_ip ?? ""));
  const [labDnsIp, setLabDnsIp] = useState(isNew ? "" : (group.lab_dns_ip ?? ""));
  const [labDhcpRangeStart, setLabDhcpRangeStart] = useState(isNew ? "" : (group.lab_dhcp_range_start ?? ""));
  const [labDhcpRangeEnd, setLabDhcpRangeEnd] = useState(isNew ? "" : (group.lab_dhcp_range_end ?? ""));
  const [scheduleEnabled, setScheduleEnabled] = useState(isNew ? false : group.lab_schedule_enabled);
  const [scheduleIntervalHours, setScheduleIntervalHours] = useState(
    isNew ? "" : (group.lab_schedule_interval_hours?.toString() ?? ""),
  );
  const [scheduleClusterId, setScheduleClusterId] = useState(isNew ? "" : (group.lab_schedule_pve_cluster_id ?? ""));
  const [scheduleTargetNode, setScheduleTargetNode] = useState(isNew ? "" : (group.lab_schedule_target_node ?? ""));
  const [scheduleTargetStorage, setScheduleTargetStorage] = useState(
    isNew ? "" : (group.lab_schedule_target_storage ?? ""),
  );
  const [error, setError] = useState<string | null>(null);
  const { groups, isLoading: groupsLoading } = useCustomerVmBackupGroups(customerId);

  const clustersQuery = useQuery({ queryKey: ["pve-clusters"], queryFn: pveClustersApi.list });
  const labClusters = (clustersQuery.data ?? []).filter((c) => c.is_lab_compute && !c.disabled);
  const scheduleNodesQuery = useQuery({
    queryKey: ["pve-nodes", scheduleClusterId],
    queryFn: () => pveClustersApi.nodes(scheduleClusterId),
    enabled: scheduleClusterId !== "",
  });
  const scheduleStoragesQuery = useQuery({
    queryKey: ["pve-storages", scheduleClusterId, scheduleTargetNode],
    queryFn: () => pveClustersApi.storages(scheduleClusterId, scheduleTargetNode),
    enabled: scheduleClusterId !== "" && scheduleTargetNode !== "",
  });
  // From the group's original members, before local edits -- a member's
  // backup_group_id doesn't change identity as the form is edited, so a
  // one-time snapshot at mount is enough to keep showing the warning.
  const supersededByBackupGroupId: Record<string, boolean> = isNew
    ? {}
    : Object.fromEntries(group.members.map((m) => [m.backup_group_id, m.backup_group_superseded]));

  const networkFields = {
    lab_network_mode: labNetworkMode,
    lab_subnet_cidr: labSubnetCidr.trim() === "" ? null : labSubnetCidr.trim(),
    lab_gateway_ip: labGatewayIp.trim() === "" ? null : labGatewayIp.trim(),
    lab_dns_ip: labDnsIp.trim() === "" ? null : labDnsIp.trim(),
    lab_dhcp_range_start: labDhcpRangeStart.trim() === "" ? null : labDhcpRangeStart.trim(),
    lab_dhcp_range_end: labDhcpRangeEnd.trim() === "" ? null : labDhcpRangeEnd.trim(),
  };
  const scheduleFields = {
    lab_schedule_enabled: scheduleEnabled,
    lab_schedule_interval_hours: scheduleIntervalHours.trim() === "" ? null : Number(scheduleIntervalHours),
    lab_schedule_pve_cluster_id: scheduleClusterId === "" ? null : scheduleClusterId,
    lab_schedule_target_node: scheduleTargetNode === "" ? null : scheduleTargetNode,
    lab_schedule_target_storage: scheduleTargetStorage === "" ? null : scheduleTargetStorage,
  };
  // Mirrors the backend's own `_validate_lab_schedule` -- caught here too
  // so enabling the schedule with an incomplete target fails fast in the
  // form instead of round-tripping a 400.
  const scheduleIncomplete =
    scheduleEnabled &&
    (scheduleFields.lab_schedule_interval_hours === null ||
      scheduleFields.lab_schedule_pve_cluster_id === null ||
      scheduleFields.lab_schedule_target_node === null ||
      scheduleFields.lab_schedule_target_storage === null);

  const saveMutation = useMutation({
    mutationFn: () =>
      isNew
        ? applicationGroupsApi.create({ customer_id: customerId, name, members, ...networkFields, ...scheduleFields })
        : applicationGroupsApi.update(group.id, { name, members, ...networkFields, ...scheduleFields }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: GROUPS_QUERY_KEY });
      onClose();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const addMember = () =>
    setMembers((prev) => [...prev, { backup_group_id: "", startup_order: prev.length, check_types: [], expected_ip: null }]);

  const canSave =
    customerId !== "" &&
    name.trim() !== "" &&
    members.length > 0 &&
    members.every((m) => m.backup_group_id !== "") &&
    !scheduleIncomplete;

  return (
    <Modal open title={isNew ? t("recoveryLab.createGroupTitle") : group.name} onClose={onClose}>
      <form
        className={styles.groupForm}
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          saveMutation.mutate();
        }}
      >
        {error && <InlineAlert tone="danger">{error}</InlineAlert>}
        <div className="field">
          <label className="label" htmlFor="app-group-customer">
            {t("recoveryLab.customer")}
          </label>
          <select
            id="app-group-customer"
            className="select"
            value={customerId}
            disabled={!isNew}
            onChange={(e) => {
              setCustomerId(e.target.value);
              setMembers([]);
            }}
          >
            <option value="">—</option>
            {customersQuery.data?.map((customer) => (
              <option key={customer.id} value={customer.id}>
                {customer.name}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label className="label" htmlFor="app-group-name">
            {t("common.name")}
          </label>
          <input id="app-group-name" className="input" value={name} onChange={(e) => setName(e.target.value)} required />
        </div>

        <h3 className={styles.sectionSubtitle}>{t("recoveryLab.labNetwork")}</h3>
        <p className="mutedText">{t("recoveryLab.labNetworkHelp")}</p>
        <div className="field">
          <label className="label" htmlFor="app-group-network-mode">
            {t("recoveryLab.labNetworkMode")}
          </label>
          <select
            id="app-group-network-mode"
            className="select"
            value={labNetworkMode}
            onChange={(e) => setLabNetworkMode(e.target.value as LabNetworkMode)}
          >
            <option value="dhcp">{t("recoveryLab.labNetworkModeDhcp")}</option>
            <option value="static">{t("recoveryLab.labNetworkModeStatic")}</option>
          </select>
        </div>
        <div className="field">
          <label className="label" htmlFor="app-group-subnet-cidr">
            {t("recoveryLab.labSubnetCidr")}
          </label>
          <input
            id="app-group-subnet-cidr"
            className="input"
            placeholder="10.99.0.0/24"
            value={labSubnetCidr}
            onChange={(e) => setLabSubnetCidr(e.target.value)}
          />
        </div>
        <div className="field">
          <label className="label" htmlFor="app-group-gateway-ip">
            {t("recoveryLab.labGatewayIp")}
          </label>
          <input
            id="app-group-gateway-ip"
            className="input"
            placeholder="10.99.0.1"
            value={labGatewayIp}
            onChange={(e) => setLabGatewayIp(e.target.value)}
          />
        </div>
        <div className="field">
          <label className="label" htmlFor="app-group-dns-ip">
            {t("recoveryLab.labDnsIp")}
          </label>
          <input
            id="app-group-dns-ip"
            className="input"
            placeholder={t("recoveryLab.labDnsIpPlaceholder")}
            value={labDnsIp}
            onChange={(e) => setLabDnsIp(e.target.value)}
          />
        </div>
        {labNetworkMode === "dhcp" && (
          <>
            <div className="field">
              <label className="label" htmlFor="app-group-dhcp-start">
                {t("recoveryLab.labDhcpRangeStart")}
              </label>
              <input
                id="app-group-dhcp-start"
                className="input"
                placeholder="10.99.0.10"
                value={labDhcpRangeStart}
                onChange={(e) => setLabDhcpRangeStart(e.target.value)}
              />
            </div>
            <div className="field">
              <label className="label" htmlFor="app-group-dhcp-end">
                {t("recoveryLab.labDhcpRangeEnd")}
              </label>
              <input
                id="app-group-dhcp-end"
                className="input"
                placeholder="10.99.0.200"
                value={labDhcpRangeEnd}
                onChange={(e) => setLabDhcpRangeEnd(e.target.value)}
              />
            </div>
          </>
        )}

        <h3 className={styles.sectionSubtitle}>{t("recoveryLab.members")}</h3>
        {customerId === "" ? (
          <p className="mutedText">{t("recoveryLab.pickCustomerFirst")}</p>
        ) : (
          <>
            {members.map((member, index) => (
              <MemberRowEditor
                key={index}
                member={member}
                groups={groups}
                groupsLoading={groupsLoading}
                superseded={supersededByBackupGroupId[member.backup_group_id]}
                onChange={(next) => setMembers((prev) => prev.map((m, i) => (i === index ? next : m)))}
                onRemove={() => setMembers((prev) => prev.filter((_, i) => i !== index))}
              />
            ))}
            <button type="button" className="button" onClick={addMember}>
              {t("recoveryLab.addMember")}
            </button>
          </>
        )}

        <h3 className={styles.sectionSubtitle}>{t("recoveryLab.schedule")}</h3>
        <p className="mutedText">{t("recoveryLab.scheduleHelp")}</p>
        <label style={{ display: "flex", alignItems: "center", gap: "0.4rem", fontSize: "0.875rem", marginBottom: "0.5rem" }}>
          <input type="checkbox" checked={scheduleEnabled} onChange={(e) => setScheduleEnabled(e.target.checked)} />
          {t("recoveryLab.scheduleEnabled")}
        </label>
        {scheduleEnabled && (
          <>
            <div className="field">
              <label className="label" htmlFor="app-group-schedule-interval">
                {t("recoveryLab.scheduleIntervalHours")}
              </label>
              <input
                id="app-group-schedule-interval"
                className="input"
                type="number"
                min={1}
                value={scheduleIntervalHours}
                onChange={(e) => setScheduleIntervalHours(e.target.value)}
                required
              />
            </div>
            <div className="field">
              <label className="label" htmlFor="app-group-schedule-cluster">
                {t("recoveryLab.labCluster")}
              </label>
              <select
                id="app-group-schedule-cluster"
                className="select"
                value={scheduleClusterId}
                onChange={(e) => {
                  setScheduleClusterId(e.target.value);
                  setScheduleTargetNode("");
                  setScheduleTargetStorage("");
                }}
                required
              >
                <option value="">—</option>
                {labClusters.map((cluster) => (
                  <option key={cluster.id} value={cluster.id}>
                    {cluster.display_name}
                  </option>
                ))}
              </select>
              {labClusters.length === 0 && <p className="mutedText">{t("recoveryLab.noLabClusters")}</p>}
            </div>
            {scheduleClusterId !== "" && (
              <div className="field">
                <label className="label" htmlFor="app-group-schedule-node">
                  {t("restoreCenter.node")}
                </label>
                <select
                  id="app-group-schedule-node"
                  className="select"
                  value={scheduleTargetNode}
                  onChange={(e) => {
                    setScheduleTargetNode(e.target.value);
                    setScheduleTargetStorage("");
                  }}
                  disabled={scheduleNodesQuery.isLoading}
                  required
                >
                  <option value="">—</option>
                  {scheduleNodesQuery.data?.map((node) => (
                    <option key={node.node} value={node.node}>
                      {node.node}
                    </option>
                  ))}
                </select>
              </div>
            )}
            {scheduleTargetNode !== "" && (
              <div className="field">
                <label className="label" htmlFor="app-group-schedule-storage">
                  {t("restoreCenter.storage")}
                </label>
                <select
                  id="app-group-schedule-storage"
                  className="select"
                  value={scheduleTargetStorage}
                  onChange={(e) => setScheduleTargetStorage(e.target.value)}
                  disabled={scheduleStoragesQuery.isLoading}
                  required
                >
                  <option value="">—</option>
                  {scheduleStoragesQuery.data
                    ?.filter((s) => s.type !== "pbs")
                    .map((storage) => (
                      <option key={storage.storage} value={storage.storage}>
                        {storage.storage}
                      </option>
                    ))}
                </select>
              </div>
            )}
            {!isNew && group.lab_next_scheduled_run_at && (
              <p className="mutedText">
                {t("recoveryLab.scheduleNextRun", { when: new Date(group.lab_next_scheduled_run_at).toLocaleString() })}
              </p>
            )}
          </>
        )}

        <div className="formActions">
          <button type="button" className="button" onClick={onClose}>
            {t("common.cancel")}
          </button>
          <button type="submit" className="button buttonPrimary" disabled={!canSave || saveMutation.isPending}>
            {t("common.save")}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function ApplicationGroupsTab({ creating, onCloseCreate }: { creating: boolean; onCloseCreate: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const groupsQuery = useQuery({ queryKey: GROUPS_QUERY_KEY, queryFn: applicationGroupsApi.list });
  const customersQuery = useQuery({ queryKey: ["customers"], queryFn: customersApi.list });
  const [editingGroup, setEditingGroup] = useState<ApplicationGroupOut | null>(null);
  const [pendingDelete, setPendingDelete] = useState<ApplicationGroupOut | null>(null);

  const removeMutation = useMutation({
    mutationFn: (id: string) => applicationGroupsApi.remove(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: GROUPS_QUERY_KEY }),
  });

  const customerName = (id: string) => customersQuery.data?.find((c) => c.id === id)?.name ?? id;

  const columns: DataTableColumn<ApplicationGroupOut>[] = [
    { key: "name", header: t("common.name"), render: (row) => row.name },
    { key: "customer", header: t("recoveryLab.customer"), render: (row) => customerName(row.customer_id) },
    { key: "members", header: t("recoveryLab.members"), render: (row) => row.members.length },
    {
      key: "actions",
      header: t("common.actions"),
      render: (row) => (
        <div className={styles.rowActions} onClick={(e) => e.stopPropagation()}>
          <button type="button" className="button" onClick={() => setEditingGroup(row)}>
            {t("common.edit")}
          </button>
          <button type="button" className="button buttonDanger" onClick={() => setPendingDelete(row)}>
            {t("common.delete")}
          </button>
        </div>
      ),
    },
  ];

  return (
    <div>
      <QueryState query={groupsQuery} isEmpty={(data) => data.length === 0}>
        {(groups) => <DataTable columns={columns} rows={groups} rowKey={(row) => row.id} />}
      </QueryState>

      {editingGroup && <ApplicationGroupFormModal group={editingGroup} onClose={() => setEditingGroup(null)} />}
      {creating && <ApplicationGroupFormModal group="new" onClose={onCloseCreate} />}

      <ConfirmActionDialog
        open={pendingDelete !== null}
        title={t("recoveryLab.confirmDeleteGroupTitle")}
        body={t("recoveryLab.confirmDeleteGroupBody", { name: pendingDelete?.name ?? "" })}
        danger
        confirmLabel={t("common.delete")}
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          if (pendingDelete) removeMutation.mutate(pendingDelete.id);
          setPendingDelete(null);
        }}
      />
      {removeMutation.isError && (
        <InlineAlert tone="danger">
          {removeMutation.error instanceof ApiError ? removeMutation.error.message : t("common.loadError")}
        </InlineAlert>
      )}
    </div>
  );
}

function CreateRunForm({ onCreated, onCancel }: { onCreated: (plan: RestoreTestPlanOut) => void; onCancel: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const groupsQuery = useQuery({ queryKey: GROUPS_QUERY_KEY, queryFn: applicationGroupsApi.list });
  const clustersQuery = useQuery({ queryKey: ["pve-clusters"], queryFn: pveClustersApi.list });
  const [groupId, setGroupId] = useState("");
  const [clusterId, setClusterId] = useState("");
  const [targetNode, setTargetNode] = useState("");
  const [targetStorage, setTargetStorage] = useState("");
  const [error, setError] = useState<string | null>(null);

  const nodesQuery = useQuery({
    queryKey: ["pve-nodes", clusterId],
    queryFn: () => pveClustersApi.nodes(clusterId),
    enabled: clusterId !== "",
  });
  const storagesQuery = useQuery({
    queryKey: ["pve-storages", clusterId, targetNode],
    queryFn: () => pveClustersApi.storages(clusterId, targetNode),
    enabled: clusterId !== "" && targetNode !== "",
  });

  const labClusters = (clustersQuery.data ?? []).filter((c) => c.is_lab_compute && !c.disabled);

  const createMutation = useMutation({
    mutationFn: () =>
      restoreTestPlansApi.create({
        application_group_id: groupId,
        pve_cluster_id: clusterId,
        target_node: targetNode,
        target_storage: targetStorage,
      }),
    onSuccess: (plan) => {
      queryClient.invalidateQueries({ queryKey: RUNS_QUERY_KEY });
      onCreated(plan);
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const canCreate = groupId !== "" && clusterId !== "" && targetNode !== "" && targetStorage !== "";

  return (
    <Panel title={t("recoveryLab.newRunTitle")} className={styles.createRunPanel}>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setError(null);
          createMutation.mutate();
        }}
      >
        {error && (
          <div className={styles.alert}>
            <InlineAlert>{error}</InlineAlert>
          </div>
        )}
        <div className="formGrid">
          <div className="field">
            <label className="label" htmlFor="lab-run-group">
              {t("recoveryLab.applicationGroup")}
            </label>
            <select id="lab-run-group" className="select" value={groupId} onChange={(e) => setGroupId(e.target.value)}>
              <option value="">—</option>
              {groupsQuery.data?.map((group) => (
                <option key={group.id} value={group.id}>
                  {group.name}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label className="label" htmlFor="lab-run-cluster">
              {t("recoveryLab.labCluster")}
            </label>
            <select
              id="lab-run-cluster"
              className="select"
              value={clusterId}
              onChange={(e) => {
                setClusterId(e.target.value);
                setTargetNode("");
                setTargetStorage("");
              }}
            >
              <option value="">—</option>
              {labClusters.map((cluster) => (
                <option key={cluster.id} value={cluster.id}>
                  {cluster.display_name}
                </option>
              ))}
            </select>
            {labClusters.length === 0 && <p className="mutedText">{t("recoveryLab.noLabClusters")}</p>}
          </div>
          {clusterId !== "" && (
            <div className="field">
              <label className="label" htmlFor="lab-run-node">
                {t("restoreCenter.node")}
              </label>
              <select
                id="lab-run-node"
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
            </div>
          )}
          {targetNode !== "" && (
            <div className="field">
              <label className="label" htmlFor="lab-run-storage">
                {t("restoreCenter.storage")}
              </label>
              <select
                id="lab-run-storage"
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
        </div>
        <div className="formActions">
          <button type="button" className="button" onClick={onCancel}>
            {t("common.cancel")}
          </button>
          <button type="submit" className="button buttonPrimary" disabled={!canCreate || createMutation.isPending}>
            {t("common.create")}
          </button>
        </div>
      </form>
    </Panel>
  );
}

function isRunActive(status: string): boolean {
  return status === "running";
}

function RunDetailModal({ plan: initialPlan, onClose }: { plan: RestoreTestPlanOut; onClose: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [plan, setPlan] = useState(initialPlan);
  const [applianceToken, setApplianceToken] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cleanupDone, setCleanupDone] = useState(false);
  const groupsQuery = useQuery({ queryKey: GROUPS_QUERY_KEY, queryFn: applicationGroupsApi.list });
  const clustersQuery = useQuery({ queryKey: ["pve-clusters"], queryFn: pveClustersApi.list });

  // Matches PveClusterDetailModal/CustomerDetailModal's own reset-on-
  // prop-change pattern -- not currently reachable (the native <dialog>
  // traps clicks on the underlying table while open, so a different run
  // can't be selected without closing first), but keeps this modal from
  // silently going stale if that ever changes.
  useEffect(() => {
    setPlan(initialPlan);
    setApplianceToken(null);
    setError(null);
    setCleanupDone(false);
  }, [initialPlan]);

  const pollQuery = useQuery({
    queryKey: ["lab-run", plan.id],
    queryFn: () => restoreTestPlansApi.get(plan.id),
    enabled: isRunActive(plan.status),
    refetchInterval: (query) => (isRunActive(query.state.data?.status ?? plan.status) ? 3000 : false),
  });

  useEffect(() => {
    if (pollQuery.data) setPlan(pollQuery.data);
  }, [pollQuery.data]);

  const invalidateList = () => queryClient.invalidateQueries({ queryKey: RUNS_QUERY_KEY });

  const executeMutation = useMutation({
    mutationFn: () => restoreTestPlansApi.execute(plan.id),
    onSuccess: (result) => {
      setPlan(result.plan);
      setApplianceToken(result.appliance_run_token);
      invalidateList();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });
  const pollMutation = useMutation({
    mutationFn: () => restoreTestPlansApi.poll(plan.id),
    onSuccess: (result) => {
      setPlan(result);
      invalidateList();
    },
  });
  const cleanupMutation = useMutation({
    mutationFn: () => restoreTestPlansApi.cleanup(plan.id),
    onMutate: () => {
      setError(null);
      setCleanupDone(false);
    },
    onSuccess: (result) => {
      setPlan(result);
      setCleanupDone(true);
      invalidateList();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const groupName = groupsQuery.data?.find((g) => g.id === plan.application_group_id)?.name ?? plan.application_group_id;
  const clusterName = clustersQuery.data?.find((c) => c.id === plan.pve_cluster_id)?.display_name ?? plan.pve_cluster_id;

  return (
    <Modal open title={groupName} onClose={onClose}>
      {error && <InlineAlert tone="danger">{error}</InlineAlert>}
      <dl className={`detailGrid ${styles.runDetails}`}>
        <dt>{t("common.status")}</dt>
        <dd>
          <StatusBadge tone={runStatusTone(plan.status)} label={t(`recoveryLab.runStatus_${plan.status}`)} />
        </dd>
        <dt>{t("recoveryLab.labCluster")}</dt>
        <dd>{clusterName}</dd>
        <dt>{t("recoveryLab.vnetName")}</dt>
        <dd>{plan.vnet_name}</dd>
      </dl>
      {plan.error && <InlineAlert tone="danger">{plan.error}</InlineAlert>}
      {cleanupMutation.isPending && <InlineAlert tone="info">{t("recoveryLab.cleanupInProgress")}</InlineAlert>}
      {cleanupDone && !plan.error && <InlineAlert tone="info">{t("recoveryLab.cleanupComplete")}</InlineAlert>}

      {applianceToken && (
        <div>
          <p className="label">{t("recoveryLab.applianceTokenTitle")}</p>
          <pre className={styles.tokenValue}>{applianceToken}</pre>
          <InlineAlert tone="info">{t("recoveryLab.applianceTokenShownOnce")}</InlineAlert>
        </div>
      )}

      <h3 className={styles.sectionSubtitle}>{t("recoveryLab.members")}</h3>
      <ul className={styles.memberList}>
        {plan.members.map((member) => (
          <li key={member.id} className={styles.memberListItem}>
            <div className={styles.memberListHeader}>
              <span>
                {t("recoveryLab.vmid")} {member.target_vmid}
              </span>
              <StatusBadge tone={memberStatusTone(member.member_status)} label={t(`recoveryLab.memberStatus_${member.member_status}`)} />
            </div>
            {member.resolved_ip && (
              <p className="mutedText">
                {t("recoveryLab.resolvedIp")}: {member.resolved_ip}
              </p>
            )}
            {member.error && <InlineAlert tone="danger">{member.error}</InlineAlert>}
            {member.probe_results.length > 0 && (
              <ul className={styles.probeResultsList}>
                {member.probe_results.map((result, index) => (
                  <li key={index}>
                    <StatusBadge
                      tone={result.status === "pass" ? "ok" : "danger"}
                      label={`${t(`recoveryLab.checkType_${result.check_type}`, result.check_type)}${
                        result.detail ? `: ${result.detail}` : ""
                      }`}
                    />
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>

      <div className="formActions">
        <button type="button" className="button" onClick={onClose}>
          {t("common.close")}
        </button>
        {plan.status === "planned" && (
          <button type="button" className="button buttonPrimary" disabled={executeMutation.isPending} onClick={() => executeMutation.mutate()}>
            {t("recoveryLab.execute")}
          </button>
        )}
        {plan.status === "running" && (
          <button type="button" className="button" disabled={pollMutation.isPending} onClick={() => pollMutation.mutate()}>
            {t("recoveryLab.pollNow")}
          </button>
        )}
        {plan.status !== "planned" && plan.status !== "completed" && (
          <button type="button" className="button buttonDanger" disabled={cleanupMutation.isPending} onClick={() => cleanupMutation.mutate()}>
            {cleanupMutation.isPending ? t("recoveryLab.cleanupInProgress") : t("recoveryLab.cleanup")}
          </button>
        )}
      </div>
    </Modal>
  );
}

/** A run's status counts as "active" for the purposes of defaulting its
 * Application Group's section open in the grouped Lab Runs view below --
 * matches `isRunActive`/the scheduler's own `_ACTIVE_STATUSES` notion of
 * "still in flight," not just "running".
 */
function isGroupSectionActive(status: string): boolean {
  return isRunActive(status) || status === "provisioning_appliance";
}

function LabRunsTab({ showCreateForm, onCloseCreate }: { showCreateForm: boolean; onCloseCreate: () => void }) {
  const { t } = useTranslation();
  const [selectedPlan, setSelectedPlan] = useState<RestoreTestPlanOut | null>(null);
  // Tracks only *deviations* from the computed default (expanded for a
  // group with an active run, collapsed otherwise) -- a group's default
  // can change over time (a run finishes, another starts), so this
  // stores "the user overrode this group's expand state," not the
  // expand state itself.
  const [toggledGroups, setToggledGroups] = useState<Set<string>>(new Set());
  const runsQuery = useQuery({ queryKey: RUNS_QUERY_KEY, queryFn: restoreTestPlansApi.list });
  const groupsQuery = useQuery({ queryKey: GROUPS_QUERY_KEY, queryFn: applicationGroupsApi.list });
  const clustersQuery = useQuery({ queryKey: ["pve-clusters"], queryFn: pveClustersApi.list });

  const groupName = (id: string) => groupsQuery.data?.find((g) => g.id === id)?.name ?? id;
  const clusterName = (id: string) => clustersQuery.data?.find((c) => c.id === id)?.display_name ?? id;

  const columns: DataTableColumn<RestoreTestPlanOut>[] = [
    { key: "cluster", header: t("recoveryLab.labCluster"), render: (row) => clusterName(row.pve_cluster_id) },
    {
      key: "status",
      header: t("common.status"),
      render: (row) => <StatusBadge tone={runStatusTone(row.status)} label={t(`recoveryLab.runStatus_${row.status}`)} />,
    },
    { key: "created", header: t("common.createdAt"), render: (row) => new Date(row.created_at).toLocaleString() },
  ];

  // Raised directly by the user: the Lab Runs table should be grouped
  // (tree-like) under its owning Application Group, rather than one
  // flat list of every run ever created. A group with zero runs doesn't
  // appear here at all -- Application Groups Tab is the place to browse
  // every group regardless of run history.
  const runsByGroup = new Map<string, RestoreTestPlanOut[]>();
  for (const run of runsQuery.data ?? []) {
    const bucket = runsByGroup.get(run.application_group_id);
    if (bucket) bucket.push(run);
    else runsByGroup.set(run.application_group_id, [run]);
  }
  const sortedGroupIds = [...runsByGroup.keys()].sort((a, b) => {
    const latestA = Math.max(...runsByGroup.get(a)!.map((r) => new Date(r.created_at).getTime()));
    const latestB = Math.max(...runsByGroup.get(b)!.map((r) => new Date(r.created_at).getTime()));
    return latestB - latestA;
  });

  const isExpanded = (groupId: string, runs: RestoreTestPlanOut[]) => {
    const defaultExpanded = runs.some((r) => isGroupSectionActive(r.status));
    return toggledGroups.has(groupId) ? !defaultExpanded : defaultExpanded;
  };
  const toggleGroup = (groupId: string) =>
    setToggledGroups((prev) => {
      const next = new Set(prev);
      if (next.has(groupId)) next.delete(groupId);
      else next.add(groupId);
      return next;
    });

  return (
    <div>
      {showCreateForm && (
        <CreateRunForm
          onCreated={(plan) => {
            onCloseCreate();
            setSelectedPlan(plan);
          }}
          onCancel={onCloseCreate}
        />
      )}
      <QueryState query={runsQuery} isEmpty={(data) => data.length === 0}>
        {() =>
          sortedGroupIds.map((groupId) => {
            const runs = runsByGroup.get(groupId)!;
            const expanded = isExpanded(groupId, runs);
            const hasActiveRun = runs.some((r) => isGroupSectionActive(r.status));
            return (
              <div key={groupId} className={styles.runsGroup}>
                <button type="button" className={styles.runsGroupHeader} onClick={() => toggleGroup(groupId)}>
                  <ChevronDownIcon className={expanded ? styles.chevronOpen : styles.chevronClosed} />
                  <span className={styles.runsGroupName}>{groupName(groupId)}</span>
                  <span className={styles.runsGroupCount}>
                    {t("recoveryLab.runsCount", { count: runs.length })}
                  </span>
                  {hasActiveRun && <StatusBadge tone="info" label={t("recoveryLab.runsGroupActive")} />}
                </button>
                {expanded && (
                  <div className={styles.runsGroupTable}>
                    <DataTable columns={columns} rows={runs} rowKey={(row) => row.id} onRowClick={setSelectedPlan} />
                  </div>
                )}
              </div>
            );
          })
        }
      </QueryState>
      {selectedPlan && <RunDetailModal plan={selectedPlan} onClose={() => setSelectedPlan(null)} />}
    </div>
  );
}

/** Manifest EPIC 10 (§22): Application Groups (define once, per customer)
 * and Recovery Lab runs against them (ADR 0019 — a zero-copy
 * `proxmox-backup-client map` + COW overlay, executed by a
 * customer-installed `resurgam-lab-agent`, not a PVE-API `qmrestore`
 * call). Two tabs on one page rather than a separate nav entry for
 * Application Groups, since the manifest's own nav tree only names one
 * Recovery Lab entry and groups are meaningless without a place to run
 * them.
 */
const TAB_IDS = ["runs", "groups"];

export function RecoveryLabPage() {
  const { t } = useTranslation();
  const [tab, setTab] = useTabParam(TAB_IDS);
  const [showCreateRun, setShowCreateRun] = useState(false);
  const [creatingGroup, setCreatingGroup] = useState(false);

  return (
    <div>
      <PageHeader title={t("recoveryLab.title")} description={t("recoveryLab.description")} />

      <Tabs
        label={t("recoveryLab.title")}
        tabs={[
          { id: "runs", label: t("recoveryLab.tabRuns") },
          { id: "groups", label: t("recoveryLab.tabGroups") },
        ]}
        active={tab}
        onChange={setTab}
        actions={
          tab === "runs" ? (
            <button type="button" className="button buttonPrimary" onClick={() => setShowCreateRun(true)} disabled={showCreateRun}>
              {t("recoveryLab.newRunTitle")}
            </button>
          ) : (
            <button type="button" className="button buttonPrimary" onClick={() => setCreatingGroup(true)}>
              {t("recoveryLab.createGroupTitle")}
            </button>
          )
        }
      />

      {tab === "runs" ? (
        <TabPanel id="runs">
          <LabRunsTab showCreateForm={showCreateRun} onCloseCreate={() => setShowCreateRun(false)} />
        </TabPanel>
      ) : (
        <TabPanel id="groups">
          <ApplicationGroupsTab creating={creatingGroup} onCloseCreate={() => setCreatingGroup(false)} />
        </TabPanel>
      )}
    </div>
  );
}
