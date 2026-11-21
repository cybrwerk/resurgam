import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { diskMountsApi, pveClustersApi } from "../api/resources";
import type {
  BackupGroupOut,
  DiskMountCreatedOut,
  DiskMountIpMode,
  DiskMountOut,
  DiskMountPlacementOut,
  DiskMountProtocol,
  DiskMountStatus,
  LabSetupJobOut,
} from "../api/types";
import { CopyButton } from "../components/CopyButton";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import { InlineAlert } from "../components/InlineAlert";
import { LabJobLog, SshLoginForm, emptySshForm, sshLoginPayload, type SshFormState } from "../components/LabSshSetup";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { SnapshotPicker } from "../components/SnapshotPicker";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { Wizard, type WizardStep } from "../components/Wizard";
import { useAuth } from "../contexts/AuthContext";
import { formatBytes } from "../lib/formatBytes";
import styles from "./DiskMountsPage.module.css";

const POLL_MS = 5000;
const LIFETIME_CHOICES = [1, 2, 4, 8, 24, 48, 72];
const EXTEND_CHOICES = [1, 4, 24];
const ACTIVE: DiskMountStatus[] = ["pending_approval", "queued", "provisioning", "ready", "stopping"];

const STATUS_TONE: Record<DiskMountStatus, StatusTone> = {
  pending_approval: "warning",
  queued: "info",
  provisioning: "info",
  ready: "ok",
  stopping: "warning",
  stopped: "unknown",
  expired: "unknown",
  failed: "danger",
  rejected: "unknown",
};

function errorText(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

/** "3 h 12 min" until `iso`, or null once it passed. */
export function remainingTime(iso: string | null, now: number): { hours: number; minutes: number } | null {
  if (!iso) return null;
  const ms = new Date(iso).getTime() - now;
  if (ms <= 0) return null;
  const totalMinutes = Math.floor(ms / 60000);
  return { hours: Math.floor(totalMinutes / 60), minutes: totalMinutes % 60 };
}

/** Splits the client restriction textarea (one entry per line, or comma/space separated). */
export function parseClients(text: string): string[] {
  return text
    .split(/[\s,;]+/)
    .map((value) => value.trim())
    .filter(Boolean);
}

/** A UNC path / NFS export that wraps only after `\` or `/`, never mid-name. */
export function BreakablePath({ text }: { text: string }) {
  const parts = text.split(/(?<=[\\/])/);
  return (
    <code className={styles.path}>
      {parts.map((part, index) => (
        <Fragment key={index}>
          {part}
          {index < parts.length - 1 && <wbr />}
        </Fragment>
      ))}
    </code>
  );
}

const NARROW_QUERY = "(max-width: 600px)";

/** True below ~600 px, where the active mounts render as cards instead of a table. */
function useNarrowViewport(): boolean {
  const [narrow, setNarrow] = useState(
    () => typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(NARROW_QUERY).matches,
  );
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia(NARROW_QUERY);
    const onChange = () => setNarrow(query.matches);
    onChange();
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);
  return narrow;
}

function StatusPill({ mount }: { mount: DiskMountOut }) {
  const { t } = useTranslation();
  return <StatusBadge tone={STATUS_TONE[mount.status]} label={t(`diskMounts.status.${mount.status}`)} />;
}

function ConnectionCell({ mount }: { mount: DiskMountOut }) {
  const { t } = useTranslation();
  const connection = mount.connection;
  if (!connection.host) {
    return <span className="mutedText">{t("diskMounts.connectionPending")}</span>;
  }
  return (
    <div className={styles.connection}>
      {connection.smb_unc && (
        <div className={styles.connectionLine}>
          <BreakablePath text={connection.smb_unc} />
          <CopyButton text={connection.smb_unc} label={t("diskMounts.copySmb")} />
        </div>
      )}
      {connection.smb_username && (
        <div className="mutedText">{t("diskMounts.smbUser", { user: connection.smb_username })}</div>
      )}
      {connection.nfs_exports.map((nfs) => (
        <div key={nfs} className={styles.connectionLine}>
          <BreakablePath text={nfs} />
          <CopyButton text={nfs} label={t("diskMounts.copyNfs")} />
        </div>
      ))}
    </div>
  );
}

function MountActions({ mount, onChanged }: { mount: DiskMountOut; onChanged: () => void }) {
  const { t } = useTranslation();
  const { user, hasRole } = useAuth();
  const [confirmStop, setConfirmStop] = useState(false);
  const [confirmForce, setConfirmForce] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [hours, setHours] = useState(EXTEND_CHOICES[1]);
  const [error, setError] = useState<string | null>(null);

  const run = (fn: () => Promise<unknown>) => ({
    mutationFn: fn,
    onSuccess: () => {
      setError(null);
      setConfirmStop(false);
      setConfirmForce(false);
      setRejecting(false);
      onChanged();
    },
    onError: (err: unknown) => setError(errorText(err, t("common.loadError"))),
  });
  const stop = useMutation(run(() => diskMountsApi.stop(mount.id)));
  const extend = useMutation(run(() => diskMountsApi.extend(mount.id, hours)));
  const approve = useMutation(run(() => diskMountsApi.approve(mount.id)));
  const reject = useMutation(run(() => diskMountsApi.reject(mount.id, reason.trim())));
  const forceFinish = useMutation(run(() => diskMountsApi.forceFinish(mount.id)));
  const canForceFinish = mount.status === "stopping" && hasRole("platform_admin");

  const isRequester = mount.created_by_user_id === user?.user_id;
  const canStop = ACTIVE.includes(mount.status) && mount.status !== "stopping";

  return (
    <div className={styles.actions}>
      {mount.status === "pending_approval" && !isRequester && !rejecting && (
        <div className={styles.inlineRow}>
          <button type="button" className="button" disabled={approve.isPending} onClick={() => approve.mutate()}>
            {t("diskMounts.approve")}
          </button>
          <button type="button" className="button" onClick={() => setRejecting(true)}>
            {t("diskMounts.reject")}
          </button>
        </div>
      )}
      {mount.status === "pending_approval" && isRequester && (
        <span className="mutedText">{t("diskMounts.awaitingOtherApprover")}</span>
      )}
      {rejecting && (
        <div className={styles.inlineRow}>
          <input
            className="input"
            aria-label={t("diskMounts.rejectReason")}
            placeholder={t("diskMounts.rejectReason")}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <button type="button" className="button" onClick={() => setRejecting(false)}>
            {t("common.cancel")}
          </button>
          <button
            type="button"
            className="button buttonDanger"
            disabled={reason.trim() === "" || reject.isPending}
            onClick={() => reject.mutate()}
          >
            {t("diskMounts.rejectConfirm")}
          </button>
        </div>
      )}
      {mount.status === "ready" && (
        <div className={styles.inlineRow}>
          <select
            className="select"
            aria-label={t("diskMounts.extendBy")}
            value={hours}
            onChange={(e) => setHours(Number(e.target.value))}
          >
            {EXTEND_CHOICES.map((h) => (
              <option key={h} value={h}>
                {t("diskMounts.plusHours", { count: h })}
              </option>
            ))}
          </select>
          <button type="button" className="button" disabled={extend.isPending} onClick={() => extend.mutate()}>
            {t("diskMounts.extend")}
          </button>
        </div>
      )}
      {canStop && !confirmStop && (
        <button type="button" className="button buttonDanger" onClick={() => setConfirmStop(true)}>
          {t("diskMounts.stop")}
        </button>
      )}
      {confirmStop && (
        <div className={styles.confirm} role="group" aria-label={t("diskMounts.stopQuestion")}>
          <span>{t("diskMounts.stopQuestion")}</span>
          <div className={styles.inlineRow}>
            <button type="button" className="button" onClick={() => setConfirmStop(false)}>
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="button buttonDanger"
              disabled={stop.isPending}
              onClick={() => stop.mutate()}
            >
              {t("diskMounts.stopConfirm")}
            </button>
          </div>
        </div>
      )}
      {canForceFinish && !confirmForce && (
        <button type="button" className="button" onClick={() => setConfirmForce(true)}>
          {t("diskMounts.forceFinish")}
        </button>
      )}
      {confirmForce && (
        <div className={styles.confirm} role="group" aria-label={t("diskMounts.forceFinish")}>
          <span>{t("diskMounts.forceFinishQuestion")}</span>
          <div className={styles.inlineRow}>
            <button type="button" className="button" onClick={() => setConfirmForce(false)}>
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="button buttonDanger"
              disabled={forceFinish.isPending}
              onClick={() => forceFinish.mutate()}
            >
              {t("diskMounts.forceFinishConfirm")}
            </button>
          </div>
        </div>
      )}
      {error && <InlineAlert tone="danger">{error}</InlineAlert>}
    </div>
  );
}

/** One active mount on a phone: everything the table row has, stacked. */
function MountCard({
  mount,
  now,
  onChanged,
  onOpen,
}: {
  mount: DiskMountOut;
  now: number;
  onChanged: () => void;
  onOpen: () => void;
}) {
  const { t } = useTranslation();
  const left = remainingTime(mount.expires_at, now);
  return (
    <li className={styles.card} data-testid="disk-mount-card">
      <div className={styles.cardHeader}>
        <StatusPill mount={mount} />
        {mount.status === "ready" && left && (
          <span className="mutedText" title={mount.expires_at ?? undefined}>
            {t("diskMounts.remaining", left)}
          </span>
        )}
      </div>
      <div>
        <div className={styles.cardTitle}>{mount.workload_label}</div>
        <div className="mutedText">
          {mount.customer_name ?? "—"} · {mount.snapshot_time ? new Date(mount.snapshot_time).toLocaleString() : "—"}
        </div>
        <div className="mutedText">
          {mount.pve_cluster_name} / {mount.target_node}
        </div>
      </div>
      <ConnectionCell mount={mount} />
      <MountActions mount={mount} onChanged={onChanged} />
      <div>
        <button type="button" className="button buttonGhost" onClick={onOpen}>
          {t("diskMounts.showDetails")}
        </button>
      </div>
    </li>
  );
}

function MountDetail({ mount }: { mount: DiskMountOut }) {
  const { t } = useTranslation();
  const network = [
    mount.bridge,
    mount.vlan_tag !== null ? t("diskMounts.vlan", { tag: mount.vlan_tag }) : null,
    mount.ip_mode === "dhcp" ? t("diskMounts.ipMode.dhcp") : mount.ip_address_cidr,
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <Panel title={t("diskMounts.detailTitle", { name: mount.workload_label ?? mount.backup_group_id })}>
      <dl className="detailGrid">
        <dt>{t("diskMounts.colStatus")}</dt>
        <dd>
          <StatusPill mount={mount} />
          {mount.error && <span className="mutedText">{mount.error}</span>}
        </dd>
        <dt>{t("diskMounts.colTarget")}</dt>
        <dd>
          {mount.pve_cluster_name} / {mount.target_node}
          {mount.helper_vmid !== null && ` · ${t("diskMounts.helperVm", { vmid: mount.helper_vmid })}`}
        </dd>
        <dt>{t("diskMounts.network")}</dt>
        <dd>{network}</dd>
        <dt>{t("diskMounts.protocols")}</dt>
        <dd>{mount.protocols.map((p) => t(`diskMounts.protocol.${p}`)).join(", ")}</dd>
        <dt>{t("diskMounts.allowedClients")}</dt>
        <dd>{mount.allowed_clients.length ? mount.allowed_clients.join(", ") : t("diskMounts.anyClient")}</dd>
        {mount.approval_notes && (
          <>
            <dt>{t("diskMounts.rejectReason")}</dt>
            <dd>{mount.approval_notes}</dd>
          </>
        )}
      </dl>
      {mount.volumes.length > 0 && (
        <table className={`table ${styles.volumes}`} data-testid="disk-mount-volumes">
          <thead>
            <tr>
              <th>{t("diskMounts.volDisk")}</th>
              <th>{t("diskMounts.volPartition")}</th>
              <th>{t("diskMounts.volFs")}</th>
              <th>{t("diskMounts.volState")}</th>
            </tr>
          </thead>
          <tbody>
            {mount.volumes.map((v, index) => (
              <tr key={`${v.archive}-${v.partition}-${index}`}>
                <td>
                  {t("diskMounts.diskN", { n: v.disk_index })} <span className="mutedText">{v.archive}</span>
                </td>
                <td>
                  {v.partition}
                  {v.size_bytes !== null && <span className="mutedText"> · {formatBytes(v.size_bytes)}</span>}
                </td>
                <td>{v.fstype ?? "—"}</td>
                <td>
                  {v.mounted ? (
                    <StatusBadge tone="ok" label={t("diskMounts.volMounted", { name: v.name })} />
                  ) : (
                    <StatusBadge
                      tone={v.reason === "mount_failed" ? "danger" : "unknown"}
                      label={t(`diskMounts.volReason.${v.reason ?? "unsupported"}`)}
                    />
                  )}
                  {v.detail && <div className="mutedText">{v.detail}</div>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}

// --- the "new disk mount" wizard ------------------------------------------------------------

function NewDiskMountWizard({
  placement,
  onCreated,
  onCancel,
}: {
  placement: DiskMountPlacementOut[];
  onCreated: (created: DiskMountCreatedOut) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [step, setStep] = useState(0);
  const [customerId, setCustomerId] = useState("");
  const [groupId, setGroupId] = useState("");
  const [snapshotId, setSnapshotId] = useState("");
  const [archives, setArchives] = useState<string[]>([]);
  const [clusterId, setClusterId] = useState("");
  const [node, setNode] = useState("");
  const [ipMode, setIpMode] = useState<DiskMountIpMode>("dhcp");
  const [bridge, setBridge] = useState("");
  const [vlan, setVlan] = useState("");
  const [address, setAddress] = useState("");
  const [gateway, setGateway] = useState("");
  const [protocols, setProtocols] = useState<DiskMountProtocol[]>(["smb"]);
  const [clients, setClients] = useState("");
  const [lifetime, setLifetime] = useState(4);

  const readyClusters = placement.filter((p) => p.ready);
  const cluster = placement.find((p) => p.pve_cluster_id === clusterId);

  const disksQuery = useQuery({
    queryKey: ["disk-mount-disks", snapshotId],
    queryFn: () => diskMountsApi.snapshotDisks(snapshotId),
    enabled: snapshotId !== "",
  });
  const nodesQuery = useQuery({
    queryKey: ["disk-mount-nodes", clusterId],
    queryFn: () => diskMountsApi.nodes(clusterId),
    enabled: clusterId !== "",
  });
  const bridgesQuery = useQuery({
    queryKey: ["disk-mount-bridges", clusterId, node],
    queryFn: () => diskMountsApi.bridges(clusterId, node),
    enabled: clusterId !== "" && node !== "" && ipMode !== "cluster_range",
  });

  const create = useMutation({
    mutationFn: () =>
      diskMountsApi.create({
        backup_snapshot_id: snapshotId,
        disk_archives: archives,
        pve_cluster_id: clusterId,
        target_node: node,
        bridge: ipMode === "cluster_range" ? null : bridge,
        vlan_tag: ipMode === "cluster_range" || vlan.trim() === "" ? null : Number(vlan),
        ip_mode: ipMode,
        ip_address_cidr: ipMode === "static" ? address.trim() : null,
        gateway: ipMode === "static" && gateway.trim() ? gateway.trim() : null,
        protocols,
        allowed_clients: parseClients(clients),
        lifetime_hours: lifetime,
      }),
    onSuccess: onCreated,
  });

  const clientList = parseClients(clients);
  const steps: WizardStep[] = [
    { id: "source", title: t("diskMounts.stepSource") },
    { id: "disks", title: t("diskMounts.stepDisks") },
    { id: "network", title: t("diskMounts.stepNetwork") },
    { id: "share", title: t("diskMounts.stepShare") },
    { id: "review", title: t("diskMounts.stepReview") },
  ];
  const vlanValid = vlan.trim() === "" || (Number(vlan) >= 1 && Number(vlan) <= 4094);
  const canAdvance = (() => {
    switch (step) {
      case 0:
        return snapshotId !== "";
      case 1:
        return archives.length > 0;
      case 2:
        return (
          clusterId !== "" &&
          node !== "" &&
          (ipMode === "cluster_range" || bridge !== "") &&
          vlanValid &&
          (ipMode !== "static" || address.includes("/"))
        );
      case 3:
        return protocols.length > 0 && (!protocols.includes("nfs") || clientList.length > 0);
      case 4:
        return !create.isPending;
      default:
        return false;
    }
  })();

  const toggle = <T,>(list: T[], value: T) => (list.includes(value) ? list.filter((v) => v !== value) : [...list, value]);

  return (
    <Panel
      title={t("diskMounts.newTitle")}
      testId="disk-mount-wizard"
      actions={
        <button type="button" className="button buttonGhost" onClick={onCancel}>
          {t("common.cancel")}
        </button>
      }
    >
      <Wizard
        steps={steps}
        currentStep={step}
        onStepClick={setStep}
        canAdvance={canAdvance}
        onBack={() => setStep((s) => Math.max(0, s - 1))}
        onNext={() => (step < steps.length - 1 ? setStep((s) => s + 1) : create.mutate())}
        nextLabel={t("diskMounts.submit", { cluster: cluster?.display_name ?? "" })}
      >
        {step === 0 && (
          <>
            <SnapshotPicker
              customerId={customerId}
              onCustomerChange={setCustomerId}
              groupId={groupId}
              onGroupChange={setGroupId}
              snapshotId={snapshotId}
              onSnapshotChange={(id) => {
                setSnapshotId(id);
                setArchives([]);
              }}
              groupFilter={(group: BackupGroupOut) => group.backup_type === "vm"}
            />
            <p className="mutedText">{t("diskMounts.vmOnlyHint")}</p>
          </>
        )}

        {step === 1 && (
          <div className={styles.stepFields}>
            {disksQuery.isLoading && <p className="mutedText">{t("common.loading")}</p>}
            {disksQuery.isError && (
              <InlineAlert tone="danger">{errorText(disksQuery.error, t("common.loadError"))}</InlineAlert>
            )}
            {disksQuery.data?.length === 0 && <p className="mutedText">{t("diskMounts.noDisks")}</p>}
            {disksQuery.data?.map((disk) => (
              <label key={disk.archive} className="checkboxField">
                <input
                  type="checkbox"
                  checked={archives.includes(disk.archive)}
                  onChange={() => setArchives((current) => toggle(current, disk.archive))}
                />
                <span>
                  {disk.slot} <span className="mutedText">· {formatBytes(disk.size_bytes)}</span>
                  {disk.crypt_mode === "encrypt" && (
                    <span className="mutedText"> · {t("diskMounts.encryptedBackup")}</span>
                  )}
                </span>
              </label>
            ))}
            <p className="mutedText">{t("diskMounts.disksHint")}</p>
          </div>
        )}

        {step === 2 && (
          <div className="formGrid">
            <div className="field">
              <label className="label" htmlFor="dm-cluster">
                {t("diskMounts.cluster")}
              </label>
              <select
                id="dm-cluster"
                className="select"
                value={clusterId}
                onChange={(e) => {
                  setClusterId(e.target.value);
                  setNode("");
                  setBridge("");
                }}
              >
                <option value="">—</option>
                {readyClusters.map((p) => (
                  <option key={p.pve_cluster_id} value={p.pve_cluster_id}>
                    {p.display_name}
                  </option>
                ))}
              </select>
              {cluster?.require_approval && <p className="mutedText">{t("diskMounts.approvalHint")}</p>}
            </div>
            <div className="field">
              <label className="label" htmlFor="dm-node">
                {t("diskMounts.node")}
              </label>
              <select
                id="dm-node"
                className="select"
                value={node}
                disabled={clusterId === "" || nodesQuery.isLoading}
                onChange={(e) => {
                  setNode(e.target.value);
                  setBridge("");
                }}
              >
                <option value="">—</option>
                {nodesQuery.data?.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
              <p className="mutedText">{t("diskMounts.nodeHint")}</p>
            </div>
            <div className="field">
              <span className="label">{t("diskMounts.ipModeLabel")}</span>
              <div className={styles.radioColumn}>
                {(["dhcp", "static", "cluster_range"] as DiskMountIpMode[])
                  .filter((mode) => mode !== "cluster_range" || cluster?.uplink_static_range)
                  .map((mode) => (
                    <label key={mode}>
                      <input
                        type="radio"
                        name="dm-ip-mode"
                        checked={ipMode === mode}
                        onChange={() => setIpMode(mode)}
                      />{" "}
                      {t(`diskMounts.ipMode.${mode}`)}
                    </label>
                  ))}
              </div>
            </div>
            {ipMode === "cluster_range" ? (
              <div className="field">
                <span className="label">{t("diskMounts.bridge")}</span>
                <span>
                  {cluster?.uplink_bridge}
                  {cluster?.uplink_vlan_tag != null && ` · ${t("diskMounts.vlan", { tag: cluster.uplink_vlan_tag })}`}
                </span>
              </div>
            ) : (
              <>
                <div className="field">
                  <label className="label" htmlFor="dm-bridge">
                    {t("diskMounts.bridge")}
                  </label>
                  <select
                    id="dm-bridge"
                    className="select"
                    value={bridge}
                    disabled={node === "" || bridgesQuery.isLoading}
                    onChange={(e) => setBridge(e.target.value)}
                  >
                    <option value="">—</option>
                    {bridgesQuery.data?.map((b) => (
                      <option key={b.iface} value={b.iface}>
                        {b.iface}
                        {b.comments ? ` — ${b.comments}` : ""}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label className="label" htmlFor="dm-vlan">
                    {t("diskMounts.vlanTag")}
                  </label>
                  <input
                    id="dm-vlan"
                    className="input"
                    inputMode="numeric"
                    value={vlan}
                    onChange={(e) => setVlan(e.target.value)}
                    placeholder={t("diskMounts.vlanPlaceholder")}
                  />
                  {!vlanValid && <p className="mutedText">{t("diskMounts.vlanInvalid")}</p>}
                </div>
              </>
            )}
            {ipMode === "static" && (
              <>
                <div className="field">
                  <label className="label" htmlFor="dm-address">
                    {t("diskMounts.address")}
                  </label>
                  <input
                    id="dm-address"
                    className="input"
                    value={address}
                    onChange={(e) => setAddress(e.target.value)}
                    placeholder="192.168.10.50/24"
                  />
                </div>
                <div className="field">
                  <label className="label" htmlFor="dm-gateway">
                    {t("diskMounts.gateway")}
                  </label>
                  <input
                    id="dm-gateway"
                    className="input"
                    value={gateway}
                    onChange={(e) => setGateway(e.target.value)}
                    placeholder={t("diskMounts.optional")}
                  />
                </div>
              </>
            )}
          </div>
        )}

        {step === 3 && (
          <div className="formGrid">
            <div className="field">
              <span className="label">{t("diskMounts.protocols")}</span>
              <div className={styles.radioColumn}>
                {(["smb", "nfs"] as DiskMountProtocol[]).map((p) => (
                  <label key={p} className="checkboxField">
                    <input
                      type="checkbox"
                      checked={protocols.includes(p)}
                      onChange={() => setProtocols((current) => toggle(current, p))}
                    />
                    {t(`diskMounts.protocol.${p}`)}
                  </label>
                ))}
              </div>
            </div>
            <div className="field">
              <label className="label" htmlFor="dm-clients">
                {t("diskMounts.allowedClients")}
              </label>
              <textarea
                id="dm-clients"
                className="input"
                rows={3}
                value={clients}
                onChange={(e) => setClients(e.target.value)}
                placeholder="192.168.10.25&#10;192.168.10.0/24"
              />
              <p className="mutedText">
                {protocols.includes("nfs") ? t("diskMounts.clientsRequiredNfs") : t("diskMounts.clientsOptional")}
              </p>
            </div>
            <div className="field">
              <label className="label" htmlFor="dm-lifetime">
                {t("diskMounts.lifetime")}
              </label>
              <select
                id="dm-lifetime"
                className="select"
                value={lifetime}
                onChange={(e) => setLifetime(Number(e.target.value))}
              >
                {LIFETIME_CHOICES.map((h) => (
                  <option key={h} value={h}>
                    {t("diskMounts.hours", { count: h })}
                  </option>
                ))}
              </select>
              <p className="mutedText">{t("diskMounts.lifetimeHint")}</p>
            </div>
          </div>
        )}

        {step === 4 && (
          <div>
            <dl className="detailGrid" data-testid="disk-mount-review">
              <dt>{t("diskMounts.stepDisks")}</dt>
              <dd>{archives.join(", ")}</dd>
              <dt>{t("diskMounts.colTarget")}</dt>
              <dd>
                {cluster?.display_name} / {node}
              </dd>
              <dt>{t("diskMounts.network")}</dt>
              <dd>
                {ipMode === "cluster_range" ? cluster?.uplink_bridge : bridge}
                {ipMode !== "cluster_range" && vlan.trim() && ` · ${t("diskMounts.vlan", { tag: vlan.trim() })}`}
                {" · "}
                {ipMode === "static" ? address : t(`diskMounts.ipMode.${ipMode}`)}
              </dd>
              <dt>{t("diskMounts.protocols")}</dt>
              <dd>{protocols.map((p) => t(`diskMounts.protocol.${p}`)).join(", ")}</dd>
              <dt>{t("diskMounts.allowedClients")}</dt>
              <dd>{clientList.length ? clientList.join(", ") : t("diskMounts.anyClient")}</dd>
              <dt>{t("diskMounts.lifetime")}</dt>
              <dd>{t("diskMounts.hours", { count: lifetime })}</dd>
            </dl>
            <p className="mutedText">{t("diskMounts.reviewReadOnly")}</p>
            {cluster?.require_approval && <InlineAlert tone="info">{t("diskMounts.approvalHint")}</InlineAlert>}
            {create.isError && <InlineAlert tone="danger">{errorText(create.error, t("common.loadError"))}</InlineAlert>}
          </div>
        )}
      </Wizard>
    </Panel>
  );
}

// --- setup (platform admin) ------------------------------------------------------------------

function ClusterSetup({ placement }: { placement: DiskMountPlacementOut }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [vmid, setVmid] = useState(placement.helper_template_vmid?.toString() ?? "");
  const [storage, setStorage] = useState("");
  const [ssh, setSsh] = useState<SshFormState>(emptySshForm());
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const nodesQuery = useQuery({
    queryKey: ["disk-mount-nodes", placement.pve_cluster_id],
    queryFn: () => diskMountsApi.nodes(placement.pve_cluster_id),
  });
  // Same source as the Lab Compute wizard: storages that can hold VM disks.
  const storagesQuery = useQuery({
    queryKey: ["pve-storage-config-all", placement.pve_cluster_id],
    queryFn: () => pveClustersApi.storageConfig(placement.pve_cluster_id),
  });
  const imageStorages = (storagesQuery.data ?? []).filter((s) => !s.disabled && s.content.includes("images"));
  const invalidate = useCallback(
    () => queryClient.invalidateQueries({ queryKey: ["disk-mount-placement"] }),
    [queryClient],
  );
  const save = useMutation({
    mutationFn: () => diskMountsApi.setHelperTemplate(placement.pve_cluster_id, vmid.trim() ? Number(vmid) : null),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (err) => setError(errorText(err, t("common.loadError"))),
  });
  const build = useMutation({
    mutationFn: () => {
      const login = sshLoginPayload(ssh);
      if (!login) throw new Error("ssh login incomplete");
      return diskMountsApi.startHelperTemplateBuild(placement.pve_cluster_id, {
        ssh: login,
        storage: storage.trim(),
        image_source: "download",
      });
    },
    onSuccess: (job) => {
      setJobId(job.id);
      setSsh((current) => ({ ...current, password: "", privateKey: "", passphrase: "" }));
    },
    onError: (err) => setError(errorText(err, t("common.loadError"))),
  });
  // A paired lab agent (ADR 0027) builds the template on its node -- no SSH login.
  const agentBuild = useMutation({
    mutationFn: () =>
      pveClustersApi.startLabAgentJob(placement.pve_cluster_id, {
        kind: "share_helper_template_build",
        storage: storage.trim(),
        image_source: "download",
      }),
    onSuccess: (job) => setJobId(job.id),
    onError: (err) => setError(errorText(err, t("common.loadError"))),
  });
  const onFinished = useCallback(
    (job: LabSetupJobOut) => {
      const built = job.result.template_vmid;
      if (job.status === "succeeded" && typeof built === "number") setVmid(String(built));
      invalidate();
    },
    [invalidate],
  );

  return (
    <Panel
      title={placement.display_name}
      actions={
        <StatusBadge
          tone={placement.ready ? "ok" : "warning"}
          label={placement.ready ? t("diskMounts.setupReady") : t("diskMounts.setupIncomplete")}
        />
      }
    >
      {placement.problems.length > 0 && (
        <ul className={styles.problems}>
          {placement.problems.map((problem) => (
            <li key={problem}>{t(`diskMounts.problem.${problem}`)}</li>
          ))}
        </ul>
      )}
      <div className="formGrid">
        <div className="field">
          <label className="label" htmlFor={`dm-template-${placement.pve_cluster_id}`}>
            {t("diskMounts.helperTemplate")}
          </label>
          <div className={styles.inlineRow}>
            <input
              id={`dm-template-${placement.pve_cluster_id}`}
              className="input"
              inputMode="numeric"
              value={vmid}
              onChange={(e) => setVmid(e.target.value)}
            />
            <button type="button" className="button" disabled={save.isPending} onClick={() => save.mutate()}>
              {t("common.save")}
            </button>
          </div>
          <p className="mutedText">{t("diskMounts.helperTemplateHint")}</p>
        </div>
        <div className="field">
          <label className="label" htmlFor={`dm-storage-${placement.pve_cluster_id}`}>
            {t("diskMounts.buildStorage")}
          </label>
          {imageStorages.length > 0 ? (
            <select
              id={`dm-storage-${placement.pve_cluster_id}`}
              className="select"
              value={storage}
              onChange={(e) => setStorage(e.target.value)}
            >
              <option value="">{t("diskMounts.storagePick")}</option>
              {imageStorages.map((s) => (
                <option key={s.storage} value={s.storage}>
                  {s.storage} ({s.type})
                </option>
              ))}
            </select>
          ) : (
            <input
              id={`dm-storage-${placement.pve_cluster_id}`}
              className="input"
              value={storage}
              onChange={(e) => setStorage(e.target.value)}
              placeholder="local-lvm"
            />
          )}
        </div>
      </div>
      {placement.lab_agent_node && (
        <div className="formActions">
          <span className="mutedText">{t("diskMounts.buildWithAgentHint", { node: placement.lab_agent_node })}</span>
          <button
            type="button"
            className="button buttonPrimary"
            disabled={storage.trim() === "" || agentBuild.isPending}
            onClick={() => agentBuild.mutate()}
          >
            {t("diskMounts.buildWithAgent")}
          </button>
        </div>
      )}
      {placement.lab_agent_node && <p className="mutedText">{t("diskMounts.orOverSsh")}</p>}
      <SshLoginForm
        clusterId={placement.pve_cluster_id}
        nodes={nodesQuery.data ?? []}
        state={ssh}
        onChange={setSsh}
        idPrefix={`dm-ssh-${placement.pve_cluster_id}`}
      />
      <div className="formActions">
        <button
          type="button"
          className="button"
          disabled={!sshLoginPayload(ssh) || storage.trim() === "" || build.isPending}
          onClick={() => build.mutate()}
        >
          {t("diskMounts.buildTemplate")}
        </button>
      </div>
      {jobId && <LabJobLog clusterId={placement.pve_cluster_id} jobId={jobId} onFinished={onFinished} />}
      {error && <InlineAlert tone="danger">{error}</InlineAlert>}
    </Panel>
  );
}

// --- page ------------------------------------------------------------------------------------

export function DiskMountsPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { hasRole } = useAuth();
  const isAdmin = hasRole("platform_admin");
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<DiskMountCreatedOut | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showSetup, setShowSetup] = useState(false);
  const narrow = useNarrowViewport();

  const mountsQuery = useQuery({
    queryKey: ["disk-mounts"],
    queryFn: () => diskMountsApi.list(),
    refetchInterval: (q) =>
      (q.state.data as DiskMountOut[] | undefined)?.some((m) => ACTIVE.includes(m.status)) ? POLL_MS : false,
  });
  const placementQuery = useQuery({ queryKey: ["disk-mount-placement"], queryFn: diskMountsApi.placement });

  const now = Date.now();
  const mounts = useMemo(() => mountsQuery.data ?? [], [mountsQuery.data]);
  const active = mounts.filter((m) => ACTIVE.includes(m.status));
  const past = mounts.filter((m) => !ACTIVE.includes(m.status));
  const selected = mounts.find((m) => m.id === selectedId) ?? null;
  const placement = placementQuery.data ?? [];
  const anyReady = placement.some((p) => p.ready);
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["disk-mounts"] });

  const sourceColumn: DataTableColumn<DiskMountOut> = {
    key: "source",
    header: t("diskMounts.colSource"),
    sortValue: (m) => m.workload_label ?? "",
    render: (m) => (
      <div>
        <div>{m.workload_label}</div>
        <div className="mutedText">
          {m.customer_name ?? "—"} · {m.snapshot_time ? new Date(m.snapshot_time).toLocaleString() : "—"} ·{" "}
          {m.disk_archives.length === 1 ? m.disk_archives[0] : t("diskMounts.disksCount", { count: m.disk_archives.length })}
        </div>
      </div>
    ),
  };
  const statusColumn: DataTableColumn<DiskMountOut> = {
    key: "status",
    header: t("diskMounts.colStatus"),
    sortValue: (m) => m.status,
    render: (m) => <StatusPill mount={m} />,
  };

  const activeColumns: DataTableColumn<DiskMountOut>[] = [
    statusColumn,
    sourceColumn,
    {
      key: "target",
      header: t("diskMounts.colTarget"),
      render: (m) => (
        <div>
          <div>{m.pve_cluster_name}</div>
          <div className="mutedText">{m.target_node}</div>
        </div>
      ),
    },
    {
      key: "remaining",
      header: t("diskMounts.colRemaining"),
      sortValue: (m) => m.expires_at ?? "",
      render: (m) => {
        const left = remainingTime(m.expires_at, now);
        if (m.status !== "ready" || !left) return <span className="mutedText">—</span>;
        return <span title={m.expires_at ?? undefined}>{t("diskMounts.remaining", left)}</span>;
      },
    },
    { key: "connection", header: t("diskMounts.colConnection"), render: (m) => <ConnectionCell mount={m} /> },
    {
      key: "actions",
      header: t("common.actions"),
      render: (m) => (
        <div onClick={(e) => e.stopPropagation()}>
          <MountActions mount={m} onChanged={refresh} />
        </div>
      ),
    },
  ];
  const pastColumns: DataTableColumn<DiskMountOut>[] = [
    statusColumn,
    sourceColumn,
    {
      key: "ended",
      header: t("diskMounts.colEnded"),
      sortValue: (m) => m.ended_at ?? "",
      render: (m) => (m.ended_at ? new Date(m.ended_at).toLocaleString() : "—"),
    },
    { key: "error", header: t("diskMounts.colNote"), render: (m) => m.error ?? m.approval_notes ?? "" },
  ];

  return (
    <div>
      <PageHeader
        title={t("diskMounts.title")}
        description={t("diskMounts.description")}
        actions={
          <>
            {isAdmin && (
              <button
                type="button"
                className="button"
                aria-expanded={showSetup}
                aria-controls="disk-mount-setup"
                onClick={() => setShowSetup((s) => !s)}
              >
                {t("diskMounts.setup")}
              </button>
            )}
            {!creating && (
              <button
                type="button"
                className="button buttonPrimary"
                disabled={!anyReady}
                onClick={() => {
                  setCreated(null);
                  setCreating(true);
                }}
              >
                {t("diskMounts.new")}
              </button>
            )}
          </>
        }
      />

      {!placementQuery.isLoading && !anyReady && (
        <InlineAlert tone="info">{isAdmin ? t("diskMounts.notSetUpAdmin") : t("diskMounts.notSetUp")}</InlineAlert>
      )}

      {showSetup && isAdmin && (
        <div id="disk-mount-setup" className={styles.stack} data-testid="disk-mount-setup">
          {placement.length === 0 ? (
            <EmptyState title={t("diskMounts.noLabClusters")} description={t("diskMounts.noLabClustersHint")} />
          ) : (
            placement.map((p) => <ClusterSetup key={p.pve_cluster_id} placement={p} />)
          )}
        </div>
      )}

      {created && (
        <Panel title={t("diskMounts.createdTitle")} testId="disk-mount-credentials">
          <p>{t(created.mount.status === "pending_approval" ? "diskMounts.createdPending" : "diskMounts.createdQueued")}</p>
          {created.share_password && (
            <>
              <dl className="detailGrid">
                <dt>{t("diskMounts.smbUserLabel")}</dt>
                <dd>
                  <code>{created.mount.smb_username}</code>
                  <CopyButton text={created.mount.smb_username ?? ""} label={t("diskMounts.copyUser")} />
                </dd>
                <dt>{t("diskMounts.password")}</dt>
                <dd>
                  <code data-testid="disk-mount-password">{created.share_password}</code>
                  <CopyButton text={created.share_password} label={t("diskMounts.copyPassword")} />
                </dd>
              </dl>
              <InlineAlert tone="info">{t("diskMounts.passwordOnce")}</InlineAlert>
            </>
          )}
          <div className="formActions">
            <button type="button" className="button" onClick={() => setCreated(null)}>
              {t("diskMounts.passwordSaved")}
            </button>
          </div>
        </Panel>
      )}

      {creating && (
        <NewDiskMountWizard
          placement={placement}
          onCancel={() => setCreating(false)}
          onCreated={(result) => {
            setCreating(false);
            setCreated(result);
            setSelectedId(result.mount.id);
            refresh();
          }}
        />
      )}

      <Panel title={t("diskMounts.activeTitle")} padded={false} testId="disk-mounts-active">
        {mountsQuery.isError ? (
          <InlineAlert tone="danger">{errorText(mountsQuery.error, t("common.loadError"))}</InlineAlert>
        ) : mountsQuery.isLoading ? (
          <p className={`mutedText ${styles.loading}`}>{t("common.loading")}</p>
        ) : active.length === 0 ? (
          <EmptyState title={t("diskMounts.noActive")} description={t("diskMounts.noActiveHint")} />
        ) : narrow ? (
          <ul className={styles.cards}>
            {active.map((m) => (
              <MountCard key={m.id} mount={m} now={now} onChanged={refresh} onOpen={() => setSelectedId(m.id)} />
            ))}
          </ul>
        ) : (
          <DataTable
            columns={activeColumns}
            rows={active}
            rowKey={(m) => m.id}
            onRowClick={(m) => setSelectedId(m.id)}
          />
        )}
      </Panel>

      {selected && <MountDetail mount={selected} />}

      {past.length > 0 && (
        <Panel title={t("diskMounts.pastTitle")} padded={false}>
          <DataTable columns={pastColumns} rows={past} rowKey={(m) => m.id} onRowClick={(m) => setSelectedId(m.id)} />
        </Panel>
      )}
    </div>
  );
}
