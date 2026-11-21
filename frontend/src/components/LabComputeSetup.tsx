import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { pveClustersApi } from "../api/resources";
import type {
  LabReadinessItemOut,
  LabReadinessKey,
  LabReadinessOut,
  LabReadinessStatus,
  LabSetupJobOut,
  LabTemplateBuildScriptOut,
  LabTemplateImageSource,
  PveClusterOut,
} from "../api/types";
import { CopyButton } from "./CopyButton";
import { InlineAlert } from "./InlineAlert";
import { LabNodePairing } from "./LabNodePairing";
import { LabJobLog, SshLoginForm, emptySshForm, sshLoginPayload, type SshFormState } from "./LabSshSetup";
import { Modal } from "./Modal";
import { StatusBadge, type StatusTone } from "./StatusBadge";
import { Wizard } from "./Wizard";
import { type UplinkFormState, uplinkFormFromCluster, uplinkPayload, validateUplink } from "./labUplink";
import styles from "./LabComputeSetup.module.css";

const DEFAULT_OVERLAY_STORAGE_ID = "resurgam-lab-overlays";
const DEFAULT_OVERLAY_DIR = "/var/lib/resurgam-lab-agent/overlays";
/** How often the agent step re-checks until the agent has checked in. */
const AGENT_POLL_MS = 5000;

const STATUS_TONE: Record<LabReadinessStatus, StatusTone> = {
  ok: "ok",
  missing: "danger",
  warning: "warning",
  unknown: "unknown",
};

/** True when a PVE cluster is connected well enough to run the setup
 * (the last connection check reached it and the token was accepted). */
export function canSetUpLab(cluster: PveClusterOut): boolean {
  return !cluster.disabled && (cluster.last_health_status === "ok" || cluster.last_health_status === "missing_privileges");
}

function interpolationValues(item: LabReadinessItemOut): Record<string, string> {
  const values: Record<string, string> = {};
  for (const [key, value] of Object.entries(item.details ?? {})) {
    if (Array.isArray(value)) values[key] = value.join(", ");
    else if (value !== null && value !== undefined) values[key] = String(value);
  }
  const age = Number(item.details?.age_seconds);
  if (Number.isFinite(age)) values.minutes = String(Math.floor(age / 60));
  return values;
}

/** One checklist row: status pill plus the translated finding and fix. */
export function ReadinessRow({ item }: { item: LabReadinessItemOut }) {
  const { t } = useTranslation();
  const fallback = [item.message, item.fix_hint].filter(Boolean).join(" ");
  return (
    <li className={styles.readinessRow} data-testid={`readiness-${item.key}`}>
      <StatusBadge tone={STATUS_TONE[item.status]} label={t(`labSetup.status.${item.status}`)} />
      <div>
        <div className={styles.readinessTitle}>{t(`labSetup.items.${item.key}`)}</div>
        <div className="mutedText">
          {t(`labSetup.codes.${item.key}.${item.code}`, { ...interpolationValues(item), defaultValue: fallback })}
        </div>
      </div>
    </li>
  );
}

function ReadinessList({ readiness, keys }: { readiness: LabReadinessOut | undefined; keys: LabReadinessKey[] }) {
  const { t } = useTranslation();
  if (!readiness) return <p className="mutedText">{t("labSetup.checking")}</p>;
  const items = readiness.items.filter((item) => keys.includes(item.key));
  return (
    <ul className={styles.readinessList}>
      {items.map((item) => (
        <ReadinessRow key={item.key} item={item} />
      ))}
    </ul>
  );
}

/** A labelled uplink field with its translated validation error. */
function UplinkField({
  id,
  label,
  error,
  children,
}: {
  id: string;
  label: string;
  error: string | undefined;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <div className="field">
      <label className="label" htmlFor={id}>
        {label}
      </label>
      {children}
      {error && (
        <p className={styles.fieldError} role="alert">
          {t(`labSetup.uplink.errors.${error}`)}
        </p>
      )}
    </div>
  );
}

/** A choice between the guided (SSH) and the manual way of doing one step. */
function ModeChoice<T extends string>({
  name,
  legend,
  value,
  options,
  onChange,
}: {
  name: string;
  legend: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
}) {
  return (
    <fieldset className={styles.nodeGroup}>
      <legend className="label">{legend}</legend>
      {options.map((option) => (
        <label key={option.value} className={styles.checkOption}>
          <input type="radio" name={name} checked={value === option.value} onChange={() => onChange(option.value)} />
          {option.label}
        </label>
      ))}
    </fieldset>
  );
}

/** An auto-fix button that first asks for confirmation in place (what will
 * be changed on PVE), then runs the fix. */
function AutoFixAction({
  label,
  confirmText,
  pending,
  onConfirm,
}: {
  label: string;
  confirmText: string;
  pending: boolean;
  onConfirm: () => void;
}) {
  const { t } = useTranslation();
  const [confirming, setConfirming] = useState(false);
  if (!confirming) {
    return (
      <button type="button" className="button" onClick={() => setConfirming(true)} disabled={pending}>
        {label}
      </button>
    );
  }
  return (
    <div className={styles.confirmBox} role="group" aria-label={label}>
      <p>{confirmText}</p>
      <div className={styles.inlineActions}>
        <button type="button" className="button" onClick={() => setConfirming(false)}>
          {t("common.cancel")}
        </button>
        <button
          type="button"
          className="button"
          disabled={pending}
          onClick={() => {
            setConfirming(false);
            onConfirm();
          }}
        >
          {t("labSetup.confirmApply")}
        </button>
      </div>
    </div>
  );
}

/** The "Lab bereit / Lab unvollständig" pill for a lab cluster in the list. */
export function LabReadinessPill({ cluster }: { cluster: PveClusterOut }) {
  const { t } = useTranslation();
  const query = useQuery({
    queryKey: ["pve-lab-readiness", cluster.id, [], DEFAULT_OVERLAY_STORAGE_ID],
    queryFn: () => pveClustersApi.labReadiness(cluster.id),
    enabled: cluster.is_lab_compute && !cluster.disabled,
    staleTime: 60_000,
  });
  if (!cluster.is_lab_compute) return <span className="mutedText">—</span>;
  if (query.isLoading) return <StatusBadge tone="unknown" label={t("labSetup.pillChecking")} />;
  if (query.isError || !query.data) return <StatusBadge tone="unknown" label={t("labSetup.pillUnknown")} />;
  return query.data.ready ? (
    <StatusBadge tone="ok" label={t("labSetup.pillReady")} />
  ) : (
    <StatusBadge tone="warning" label={t("labSetup.pillIncomplete")} />
  );
}

// The agent comes before the probe appliance: a paired agent can then build
// the template itself (ADR 0027).
const STEP_IDS = ["basics", "network", "storage", "agent", "appliance", "summary"] as const;

export function LabComputeSetup({ cluster, onClose }: { cluster: PveClusterOut; onClose: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [step, setStep] = useState(0);
  const [labNodes, setLabNodes] = useState<string[] | null>(null);
  const [zone, setZone] = useState(cluster.lab_sdn_zone_name ?? "");
  const [bridge, setBridge] = useState(cluster.lab_appliance_uplink_bridge ?? "");
  const [snippetStorage, setSnippetStorage] = useState(cluster.lab_snippet_storage_id ?? "");
  const [templateVmid, setTemplateVmid] = useState(cluster.lab_appliance_template_vmid?.toString() ?? "");
  const [overlayStorageId, setOverlayStorageId] = useState(DEFAULT_OVERLAY_STORAGE_ID);
  const [agentToken, setAgentToken] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [uplink, setUplink] = useState<UplinkFormState>(() => uplinkFormFromCluster(cluster));
  const uplinkErrors = validateUplink(uplink);
  const uplinkValid = Object.keys(uplinkErrors).length === 0;
  // One-time SSH login, shared by the template build and the agent install;
  // kept only in this open wizard's memory.
  const [ssh, setSsh] = useState<SshFormState>(() => emptySshForm());
  const [agentMode, setAgentMode] = useState<"pair" | "guided" | "manual">("pair");
  // The node a lab agent is paired on: template builds can then run there
  // without an SSH login.
  const [pairedNode, setPairedNode] = useState<string | null>(cluster.lab_agent_node ?? null);
  const [templateMode, setTemplateMode] = useState<"agent" | "guided" | "manual">(
    cluster.lab_agent_node ? "agent" : "guided",
  );
  const [pbsToken, setPbsToken] = useState("");
  const [agentJobId, setAgentJobId] = useState<string | null>(null);
  const [templateJobId, setTemplateJobId] = useState<string | null>(null);
  const [buildStorage, setBuildStorage] = useState("");
  const [buildVmid, setBuildVmid] = useState("");
  const [imageSource, setImageSource] = useState<LabTemplateImageSource>("download");
  const [nodeImage, setNodeImage] = useState("");
  const [buildScript, setBuildScript] = useState<LabTemplateBuildScriptOut | null>(null);

  const nodesQuery = useQuery({ queryKey: ["pve-cluster-nodes", cluster.id], queryFn: () => pveClustersApi.nodes(cluster.id) });
  const selectedNodes = labNodes ?? [];
  const readinessKey = ["pve-lab-readiness", cluster.id, selectedNodes, overlayStorageId];
  const agentStep = STEP_IDS[step] === "agent";
  const readinessQuery = useQuery({
    queryKey: readinessKey,
    queryFn: () => pveClustersApi.labReadiness(cluster.id, { nodes: selectedNodes, overlayStorageId }),
    // Keep showing the last checklist while a changed node/storage choice
    // is re-checked, instead of flashing back to "Checking…".
    placeholderData: (previous) => previous,
    refetchInterval: (query) => {
      const data = query.state.data as LabReadinessOut | undefined;
      const agent = data?.items.find((item) => item.key === "lab_agent");
      return agentStep && agent?.status !== "ok" ? AGENT_POLL_MS : false;
    },
  });
  const readiness = readinessQuery.data;

  // First readiness answer decides the default lab nodes (the overlay
  // storage's own node list, else every online node).
  useEffect(() => {
    if (labNodes === null && readiness) setLabNodes(readiness.lab_nodes);
  }, [labNodes, readiness]);
  // The SSH session targets a lab node; default to the first one.
  useEffect(() => {
    if (!ssh.node && selectedNodes.length > 0) setSsh((prev) => ({ ...prev, node: selectedNodes[0] }));
  }, [ssh.node, selectedNodes]);

  const zonesQuery = useQuery({
    queryKey: ["pve-sdn-zones", cluster.id],
    queryFn: () => pveClustersApi.sdnZones(cluster.id),
    enabled: STEP_IDS[step] === "network",
  });
  const bridgeQueries = useQueries({
    queries: selectedNodes.map((node) => ({
      queryKey: ["pve-bridges", cluster.id, node],
      queryFn: () => pveClustersApi.bridges(cluster.id, node),
      enabled: STEP_IDS[step] === "network",
    })),
  });
  const storagesQuery = useQuery({
    queryKey: ["pve-storage-config", cluster.id],
    queryFn: () => pveClustersApi.storageConfig(cluster.id, { snippetsCapable: true }),
    enabled: STEP_IDS[step] === "storage",
  });
  const templatesQuery = useQuery({
    queryKey: ["pve-templates", cluster.id],
    queryFn: () => pveClustersApi.templates(cluster.id),
    enabled: STEP_IDS[step] === "appliance",
  });
  const imageStoragesQuery = useQuery({
    queryKey: ["pve-storage-config-all", cluster.id],
    queryFn: () => pveClustersApi.storageConfig(cluster.id),
    enabled: STEP_IDS[step] === "appliance",
  });
  const nextVmidQuery = useQuery({
    queryKey: ["pve-lab-next-vmid", cluster.id],
    queryFn: () => pveClustersApi.labNextVmid(cluster.id),
    enabled: STEP_IDS[step] === "appliance",
  });
  const downloadsQuery = useQuery({
    queryKey: ["pve-lab-downloads"],
    queryFn: () => pveClustersApi.labDownloads(),
    enabled: STEP_IDS[step] === "appliance" || STEP_IDS[step] === "agent",
    retry: false,
  });
  // Storages the template's disk can go to: content "images" on the build node.
  const buildNode = templateMode === "agent" && pairedNode ? pairedNode : ssh.node;
  const imageStorages = (imageStoragesQuery.data ?? []).filter(
    (s) => s.content.includes("images") && !s.disabled && (s.nodes === null || s.nodes.includes(buildNode)),
  );

  const overlayItem = readiness?.items.find((item) => item.key === "overlay_storage");
  const overlayDir = (overlayItem?.details?.path as string | undefined) ?? DEFAULT_OVERLAY_DIR;
  const installQuery = useQuery({
    queryKey: ["pve-lab-agent-install", cluster.id, overlayStorageId, overlayDir],
    queryFn: () => pveClustersApi.labAgentInstall(cluster.id, overlayStorageId, overlayDir),
    enabled: agentStep,
  });

  // Bridges that exist on every selected lab node.
  const commonBridges = useMemo(() => {
    const lists = bridgeQueries.map((q) => q.data);
    if (lists.length === 0 || lists.some((list) => !list)) return [];
    const [first, ...rest] = lists as NonNullable<(typeof lists)[number]>[];
    return first.filter((b) => rest.every((list) => list.some((other) => other.iface === b.iface)));
  }, [bridgeQueries]);

  const errorText = (err: unknown) => (err instanceof ApiError ? err.message : t("common.loadError"));
  const download = async (path: string, filename: string) => {
    try {
      await pveClustersApi.downloadLabArtifact(path, filename);
    } catch (err) {
      setError(errorText(err));
    }
  };
  const refreshReadiness = () => queryClient.invalidateQueries({ queryKey: ["pve-lab-readiness", cluster.id] });

  const saveMutation = useMutation({
    mutationFn: () =>
      pveClustersApi.update(cluster.id, {
        lab_sdn_zone_name: zone.trim() === "" ? null : zone.trim(),
        lab_appliance_uplink_bridge: bridge.trim() === "" ? null : bridge.trim(),
        lab_snippet_storage_id: snippetStorage.trim() === "" ? null : snippetStorage.trim(),
        lab_appliance_template_vmid: templateVmid.trim() === "" ? null : Number(templateVmid),
        ...uplinkPayload(uplink),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["pve-clusters"] });
      refreshReadiness();
    },
  });

  const agentInstallMutation = useMutation({
    mutationFn: () => {
      const login = sshLoginPayload(ssh);
      if (!login) throw new Error("ssh login incomplete");
      return pveClustersApi.startLabAgentInstall(cluster.id, {
        ssh: login,
        overlay_storage_id: overlayStorageId,
        overlay_dir: overlayDir,
        pbs_token: pbsToken.trim() === "" ? null : pbsToken.trim(),
      });
    },
    onSuccess: (job) => {
      setAgentJobId(job.id);
      // The PBS token was needed once; don't keep it around.
      setPbsToken("");
    },
    onError: (err) => setError(errorText(err)),
  });

  const templateBuildMutation = useMutation({
    mutationFn: () => {
      const login = sshLoginPayload(ssh);
      if (!login) throw new Error("ssh login incomplete");
      return pveClustersApi.startLabTemplateBuild(cluster.id, {
        ssh: login,
        storage: buildStorage,
        vmid: buildVmid.trim() === "" ? null : Number(buildVmid),
        image_source: imageSource,
        node_image: imageSource === "node_image" ? nodeImage.trim() : null,
      });
    },
    onSuccess: (job) => setTemplateJobId(job.id),
    onError: (err) => setError(errorText(err)),
  });

  const agentTemplateBuildMutation = useMutation({
    mutationFn: () =>
      pveClustersApi.startLabAgentJob(cluster.id, {
        kind: "template_build",
        storage: buildStorage,
        vmid: buildVmid.trim() === "" ? null : Number(buildVmid),
        image_source: imageSource,
        node_image: imageSource === "node_image" ? nodeImage.trim() : null,
      }),
    onSuccess: (job) => setTemplateJobId(job.id),
    onError: (err) => setError(errorText(err)),
  });

  const onPaired = useCallback(
    (pairing: { node: string }) => {
      setPairedNode(pairing.node);
      setTemplateMode("agent");
      queryClient.invalidateQueries({ queryKey: ["pve-clusters"] });
      queryClient.invalidateQueries({ queryKey: ["pve-lab-readiness", cluster.id] });
    },
    [cluster.id, queryClient],
  );

  const scriptMutation = useMutation({
    mutationFn: () =>
      pveClustersApi.labTemplateBuildScript(cluster.id, {
        node: ssh.node,
        storage: buildStorage,
        vmid: buildVmid.trim() === "" ? null : Number(buildVmid),
        imageSource,
        nodeImage: imageSource === "node_image" ? nodeImage.trim() : null,
      }),
    onSuccess: (script) => setBuildScript(script),
    onError: (err) => setError(errorText(err)),
  });

  const onTemplateJobFinished = useCallback(
    (job: LabSetupJobOut) => {
      if (job.status !== "succeeded") return;
      const vmid = job.result.template_vmid;
      if (typeof vmid === "number") setTemplateVmid(String(vmid));
      queryClient.invalidateQueries({ queryKey: ["pve-templates", cluster.id] });
      queryClient.invalidateQueries({ queryKey: ["pve-clusters"] });
      queryClient.invalidateQueries({ queryKey: ["pve-lab-readiness", cluster.id] });
    },
    [cluster.id, queryClient],
  );
  const onAgentJobFinished = useCallback(
    () => queryClient.invalidateQueries({ queryKey: ["pve-lab-readiness", cluster.id] }),
    [cluster.id, queryClient],
  );

  const createOverlayMutation = useMutation({
    mutationFn: () =>
      pveClustersApi.createLabOverlayStorage(cluster.id, { storage_id: overlayStorageId, nodes: selectedNodes }),
    onSuccess: (result) => {
      setNotice(t("labSetup.fixDone", { storage: result.storage_id }));
      refreshReadiness();
    },
    onError: (err) => setError(errorText(err)),
  });

  const addContentMutation = useMutation({
    mutationFn: ({ storageId, contentType }: { storageId: string; contentType: "snippets" | "images" }) =>
      pveClustersApi.addLabStorageContent(cluster.id, { storage_id: storageId, content_type: contentType }, selectedNodes),
    onSuccess: (result) => {
      setNotice(t("labSetup.fixDone", { storage: result.storage_id }));
      refreshReadiness();
    },
    onError: (err) => setError(errorText(err)),
  });

  const tokenMutation = useMutation({
    mutationFn: () => pveClustersApi.rotateLabAgentToken(cluster.id),
    onSuccess: (result) => {
      setAgentToken(result.token);
      refreshReadiness();
    },
    onError: (err) => setError(errorText(err)),
  });

  const finishMutation = useMutation({
    mutationFn: () => pveClustersApi.update(cluster.id, { is_lab_compute: true }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["pve-clusters"] });
      onClose();
    },
    onError: (err) => setError(errorText(err)),
  });

  const steps = STEP_IDS.map((id) => ({ id, title: t(`labSetup.steps.${id}`) }));
  const savesConfig = ["network", "storage", "appliance"].includes(STEP_IDS[step]);
  const isLast = step === steps.length - 1;

  const goNext = async () => {
    setError(null);
    setNotice(null);
    if (isLast) {
      finishMutation.mutate();
      return;
    }
    if (savesConfig) {
      if (!uplinkValid) {
        setError(t("labSetup.uplink.fixErrors"));
        return;
      }
      try {
        await saveMutation.mutateAsync();
      } catch (err) {
        setError(errorText(err));
        return;
      }
    }
    setStep((s) => s + 1);
  };

  const snippetItem = readiness?.items.find((item) => item.key === "snippet_storage");
  const agentItem = readiness?.items.find((item) => item.key === "lab_agent");
  const envFile =
    installQuery.data && agentToken
      ? installQuery.data.env_file.replace(installQuery.data.token_placeholder, agentToken)
      : installQuery.data?.env_file;

  const toggleNode = (node: string) =>
    setLabNodes((prev) => {
      const current = prev ?? [];
      return current.includes(node) ? current.filter((n) => n !== node) : [...current, node];
    });

  const renderStep = () => {
    switch (STEP_IDS[step]) {
      case "basics":
        return (
          <>
            <p className="panelIntro">{t("labSetup.basicsIntro")}</p>
            <fieldset className={styles.nodeGroup}>
              <legend className="label">{t("labSetup.labNodes")}</legend>
              {nodesQuery.data?.map((node) => (
                <label key={node.node} className={styles.checkOption}>
                  <input type="checkbox" checked={selectedNodes.includes(node.node)} onChange={() => toggleNode(node.node)} />
                  {node.node}
                  {node.status && node.status !== "online" && <span className="mutedText"> ({node.status})</span>}
                </label>
              ))}
              <p className="mutedText">{t("labSetup.labNodesHint")}</p>
            </fieldset>
            <ReadinessList readiness={readiness} keys={["privileges", "backend_url"]} />
          </>
        );
      case "network":
        return (
          <>
            <p className="panelIntro">{t("labSetup.networkIntro")}</p>
            <div className="formGrid">
              <div className="field">
                <label className="label" htmlFor="lab-setup-zone">
                  {t("labSetup.zone")}
                </label>
                <select id="lab-setup-zone" className="select" value={zone} onChange={(e) => setZone(e.target.value)}>
                  <option value="">{t("labSetup.zoneAuto")}</option>
                  {zonesQuery.data?.map((z) => (
                    <option key={z.zone} value={z.zone} disabled={!z.usable_for_lab}>
                      {z.zone} ({z.type ?? "?"}){z.usable_for_lab ? "" : ` — ${t("labSetup.zoneUnusable")}`}
                    </option>
                  ))}
                  {zone && !zonesQuery.data?.some((z) => z.zone === zone) && <option value={zone}>{zone}</option>}
                </select>
                <p className="mutedText">{t("labSetup.zoneHint")}</p>
              </div>
              <div className="field">
                <label className="label" htmlFor="lab-setup-bridge">
                  {t("labSetup.bridge")}
                </label>
                <select id="lab-setup-bridge" className="select" value={bridge} onChange={(e) => setBridge(e.target.value)}>
                  <option value="">{t("labSetup.pickPlaceholder")}</option>
                  {commonBridges.map((b) => (
                    <option key={b.iface} value={b.iface}>
                      {b.iface} ({b.type}){b.comments ? ` — ${b.comments}` : ""}
                    </option>
                  ))}
                  {bridge && !commonBridges.some((b) => b.iface === bridge) && <option value={bridge}>{bridge}</option>}
                </select>
                <p className="mutedText">{t("labSetup.bridgeHint")}</p>
              </div>
              <UplinkField id="lab-setup-vlan" label={t("labSetup.uplink.vlanTag")} error={uplinkErrors.vlanTag}>
                <input
                  id="lab-setup-vlan"
                  className="input"
                  inputMode="numeric"
                  placeholder={t("labSetup.uplink.vlanUntagged")}
                  value={uplink.vlanTag}
                  onChange={(e) => setUplink({ ...uplink, vlanTag: e.target.value.replace(/\D/g, "") })}
                />
                <p className="mutedText">{t("labSetup.uplink.vlanHint")}</p>
              </UplinkField>
            </div>
            <fieldset className={styles.nodeGroup}>
              <legend className="label">{t("labSetup.uplink.ipMode")}</legend>
              <label className={styles.checkOption}>
                <input
                  type="radio"
                  name="lab-setup-ip-mode"
                  checked={uplink.ipMode === "dhcp"}
                  onChange={() => setUplink({ ...uplink, ipMode: "dhcp" })}
                />
                {t("labSetup.uplink.modeDhcp")}
              </label>
              <label className={styles.checkOption}>
                <input
                  type="radio"
                  name="lab-setup-ip-mode"
                  checked={uplink.ipMode === "static_range"}
                  onChange={() => setUplink({ ...uplink, ipMode: "static_range" })}
                />
                {t("labSetup.uplink.modeStatic")}
              </label>
              <p className="mutedText">
                {uplink.ipMode === "dhcp" ? t("labSetup.uplink.dhcpHint") : t("labSetup.uplink.staticHint")}
              </p>
            </fieldset>
            {uplink.ipMode === "static_range" && (
              <div className="formGrid">
                <UplinkField id="lab-setup-range-start" label={t("labSetup.uplink.rangeStart")} error={uplinkErrors.rangeStart}>
                  <input
                    id="lab-setup-range-start"
                    className="input"
                    placeholder="192.168.10.50"
                    value={uplink.rangeStart}
                    onChange={(e) => setUplink({ ...uplink, rangeStart: e.target.value })}
                  />
                </UplinkField>
                <UplinkField id="lab-setup-range-end" label={t("labSetup.uplink.rangeEnd")} error={uplinkErrors.rangeEnd}>
                  <input
                    id="lab-setup-range-end"
                    className="input"
                    placeholder="192.168.10.59"
                    value={uplink.rangeEnd}
                    onChange={(e) => setUplink({ ...uplink, rangeEnd: e.target.value })}
                  />
                </UplinkField>
                <UplinkField id="lab-setup-prefix" label={t("labSetup.uplink.prefixLength")} error={uplinkErrors.prefixLength}>
                  <input
                    id="lab-setup-prefix"
                    className="input"
                    inputMode="numeric"
                    value={uplink.prefixLength}
                    onChange={(e) => setUplink({ ...uplink, prefixLength: e.target.value.replace(/\D/g, "") })}
                  />
                </UplinkField>
                <UplinkField id="lab-setup-gateway" label={t("labSetup.uplink.gateway")} error={uplinkErrors.gateway}>
                  <input
                    id="lab-setup-gateway"
                    className="input"
                    placeholder="192.168.10.1"
                    value={uplink.gateway}
                    onChange={(e) => setUplink({ ...uplink, gateway: e.target.value })}
                  />
                </UplinkField>
                <UplinkField id="lab-setup-dns" label={t("labSetup.uplink.dnsServers")} error={uplinkErrors.dnsServers}>
                  <input
                    id="lab-setup-dns"
                    className="input"
                    placeholder="192.168.10.2, 9.9.9.9"
                    value={uplink.dnsServers}
                    onChange={(e) => setUplink({ ...uplink, dnsServers: e.target.value })}
                  />
                  <p className="mutedText">{t("labSetup.uplink.dnsHint")}</p>
                </UplinkField>
              </div>
            )}
            <ReadinessList readiness={readiness} keys={["sdn_zone", "uplink_bridge", "uplink_network"]} />
          </>
        );
      case "storage":
        return (
          <>
            <p className="panelIntro">{t("labSetup.storageIntro")}</p>
            <div className="formGrid">
              <div className="field">
                <label className="label" htmlFor="lab-setup-snippet-storage">
                  {t("labSetup.snippetStorage")}
                </label>
                <select
                  id="lab-setup-snippet-storage"
                  className="select"
                  value={snippetStorage}
                  onChange={(e) => setSnippetStorage(e.target.value)}
                >
                  <option value="">{t("labSetup.pickPlaceholder")}</option>
                  {storagesQuery.data?.map((s) => (
                    <option key={s.storage} value={s.storage}>
                      {s.storage} ({s.type}){s.content.includes("snippets") ? "" : ` — ${t("labSetup.noSnippetsYet")}`}
                    </option>
                  ))}
                  {snippetStorage && !storagesQuery.data?.some((s) => s.storage === snippetStorage) && (
                    <option value={snippetStorage}>{snippetStorage}</option>
                  )}
                </select>
                <p className="mutedText">{t("labSetup.snippetStorageHint")}</p>
              </div>
              <div className="field">
                <label className="label" htmlFor="lab-setup-overlay-id">
                  {t("labSetup.overlayStorage")}
                </label>
                <input
                  id="lab-setup-overlay-id"
                  className="input"
                  value={overlayStorageId}
                  onChange={(e) => setOverlayStorageId(e.target.value.trim() || DEFAULT_OVERLAY_STORAGE_ID)}
                />
                <p className="mutedText">{t("labSetup.overlayStorageHint", { path: DEFAULT_OVERLAY_DIR })}</p>
              </div>
            </div>
            <ReadinessList readiness={readiness} keys={["snippet_storage", "overlay_storage"]} />
            <div className={styles.fixes}>
              {snippetItem?.auto_fix === "add_snippets_content" && snippetStorage === cluster.lab_snippet_storage_id && (
                <AutoFixAction
                  label={t("labSetup.fixAddSnippets", { storage: snippetStorage })}
                  confirmText={t("labSetup.fixAddSnippetsConfirm", { storage: snippetStorage })}
                  pending={addContentMutation.isPending}
                  onConfirm={() => addContentMutation.mutate({ storageId: snippetStorage, contentType: "snippets" })}
                />
              )}
              {snippetItem?.auto_fix === "add_snippets_content" && snippetStorage !== cluster.lab_snippet_storage_id && (
                <p className="mutedText">{t("labSetup.saveFirst")}</p>
              )}
              {overlayItem?.auto_fix === "create_overlay_storage" && (
                <AutoFixAction
                  label={t("labSetup.fixCreateOverlay", { storage: overlayStorageId })}
                  confirmText={t("labSetup.fixCreateOverlayConfirm", {
                    storage: overlayStorageId,
                    path: DEFAULT_OVERLAY_DIR,
                    nodes: selectedNodes.join(", "),
                  })}
                  pending={createOverlayMutation.isPending || selectedNodes.length === 0}
                  onConfirm={() => createOverlayMutation.mutate()}
                />
              )}
              {overlayItem?.auto_fix === "add_images_content" && (
                <AutoFixAction
                  label={t("labSetup.fixAddImages", { storage: overlayStorageId })}
                  confirmText={t("labSetup.fixAddImagesConfirm", { storage: overlayStorageId })}
                  pending={addContentMutation.isPending}
                  onConfirm={() => addContentMutation.mutate({ storageId: overlayStorageId, contentType: "images" })}
                />
              )}
            </div>
          </>
        );
      case "appliance":
        return (
          <>
            <p className="panelIntro">{t("labSetup.applianceIntro")}</p>
            <div className="formGrid">
              <div className="field">
                <label className="label" htmlFor="lab-setup-template">
                  {t("labSetup.template")}
                </label>
                <select
                  id="lab-setup-template"
                  className="select"
                  value={templateVmid}
                  onChange={(e) => setTemplateVmid(e.target.value)}
                >
                  <option value="">{t("labSetup.pickPlaceholder")}</option>
                  {templatesQuery.data
                    ?.filter((tpl) => tpl.guest_type === "qemu")
                    .map((tpl) => (
                      <option key={tpl.vmid} value={String(tpl.vmid)}>
                        {tpl.vmid} — {tpl.name ?? "?"} ({tpl.node})
                      </option>
                    ))}
                  {templateVmid && !templatesQuery.data?.some((tpl) => String(tpl.vmid) === templateVmid) && (
                    <option value={templateVmid}>{templateVmid}</option>
                  )}
                </select>
                <p className="mutedText">{t("labSetup.templateHint")}</p>
              </div>
            </div>
            <ReadinessList readiness={readiness} keys={["appliance_template"]} />

            <section className={styles.subSection} aria-labelledby="lab-setup-build-title">
              <h3 id="lab-setup-build-title" className={styles.subTitle}>
                {t("labSetup.build.title")}
              </h3>
              <p className="mutedText">{t("labSetup.build.intro")}</p>
              {downloadsQuery.isError && <InlineAlert>{errorText(downloadsQuery.error)}</InlineAlert>}
              <ModeChoice
                name="lab-setup-template-mode"
                legend={t("labSetup.build.mode")}
                value={templateMode}
                options={[
                  ...(pairedNode
                    ? [{ value: "agent" as const, label: t("labSetup.build.modeAgent", { node: pairedNode }) }]
                    : []),
                  { value: "guided" as const, label: t("labSetup.build.modeGuided") },
                  { value: "manual" as const, label: t("labSetup.build.modeManual") },
                ]}
                onChange={setTemplateMode}
              />
              {!pairedNode && <p className="mutedText">{t("labSetup.build.agentHint")}</p>}
              <div className="formGrid">
                {templateMode === "manual" && (
                  <div className="field">
                    <label className="label" htmlFor="lab-setup-build-node">
                      {t("labSetup.ssh.node")}
                    </label>
                    <select
                      id="lab-setup-build-node"
                      className="select"
                      value={ssh.node}
                      onChange={(e) => setSsh({ ...ssh, node: e.target.value, host: "", hostKey: null, confirmed: false })}
                    >
                      {selectedNodes.map((node) => (
                        <option key={node} value={node}>
                          {node}
                        </option>
                      ))}
                    </select>
                  </div>
                )}
                <div className="field">
                  <label className="label" htmlFor="lab-setup-build-storage">
                    {t("labSetup.build.storage")}
                  </label>
                  <select
                    id="lab-setup-build-storage"
                    className="select"
                    value={buildStorage}
                    onChange={(e) => setBuildStorage(e.target.value)}
                  >
                    <option value="">{t("labSetup.pickPlaceholder")}</option>
                    {imageStorages.map((s) => (
                      <option key={s.storage} value={s.storage}>
                        {s.storage} ({s.type})
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label className="label" htmlFor="lab-setup-build-vmid">
                    {t("labSetup.build.vmid")}
                  </label>
                  <input
                    id="lab-setup-build-vmid"
                    className="input"
                    inputMode="numeric"
                    placeholder={nextVmidQuery.data ? String(nextVmidQuery.data.vmid) : ""}
                    value={buildVmid}
                    onChange={(e) => setBuildVmid(e.target.value.replace(/\D/g, ""))}
                  />
                  <p className="mutedText">{t("labSetup.build.vmidHint")}</p>
                </div>
              </div>
              <ModeChoice
                name="lab-setup-image-source"
                legend={t("labSetup.build.image")}
                value={imageSource}
                options={[
                  { value: "download", label: t("labSetup.build.imageDownload") },
                  { value: "node_image", label: t("labSetup.build.imageNode") },
                ]}
                onChange={setImageSource}
              />
              <p className="mutedText">
                {imageSource === "download" ? t("labSetup.build.imageDownloadHint") : t("labSetup.build.imageNodeHint")}
              </p>
              {imageSource === "node_image" && (
                <div className="formGrid">
                  <div className="field">
                    <label className="label" htmlFor="lab-setup-node-image">
                      {t("labSetup.build.nodeImage")}
                    </label>
                    <input
                      id="lab-setup-node-image"
                      className="input"
                      placeholder="local:import/debian-13-genericcloud-amd64.qcow2"
                      value={nodeImage}
                      onChange={(e) => setNodeImage(e.target.value)}
                    />
                  </div>
                </div>
              )}

              {templateMode === "agent" ? (
                <>
                  <p className="mutedText">{t("labSetup.build.agentRuns", { node: pairedNode })}</p>
                  <div className={styles.inlineActions}>
                    <button
                      type="button"
                      className="button buttonPrimary"
                      disabled={
                        !buildStorage ||
                        (imageSource === "node_image" && !nodeImage.trim()) ||
                        agentTemplateBuildMutation.isPending
                      }
                      onClick={() => {
                        setError(null);
                        agentTemplateBuildMutation.mutate();
                      }}
                    >
                      {t("labSetup.build.start")}
                    </button>
                  </div>
                  {templateJobId && (
                    <LabJobLog clusterId={cluster.id} jobId={templateJobId} onFinished={onTemplateJobFinished} />
                  )}
                </>
              ) : templateMode === "guided" ? (
                <>
                  <SshLoginForm clusterId={cluster.id} nodes={selectedNodes} state={ssh} onChange={setSsh} idPrefix="lab-build-ssh" />
                  <div className={styles.inlineActions}>
                    <button
                      type="button"
                      className="button"
                      disabled={
                        !sshLoginPayload(ssh) ||
                        !buildStorage ||
                        (imageSource === "node_image" && !nodeImage.trim()) ||
                        templateBuildMutation.isPending
                      }
                      onClick={() => {
                        setError(null);
                        templateBuildMutation.mutate();
                      }}
                    >
                      {t("labSetup.build.start")}
                    </button>
                  </div>
                  {templateJobId && (
                    <LabJobLog clusterId={cluster.id} jobId={templateJobId} onFinished={onTemplateJobFinished} />
                  )}
                </>
              ) : (
                <>
                  <div className={styles.inlineActions}>
                    <button
                      type="button"
                      className="button"
                      disabled={!buildStorage || !ssh.node || scriptMutation.isPending}
                      onClick={() => {
                        setError(null);
                        scriptMutation.mutate();
                      }}
                    >
                      {t("labSetup.build.showScript")}
                    </button>
                    {buildScript && (
                      <button
                        type="button"
                        className="button"
                        onClick={() =>
                          download(buildScript.prober_download_path, buildScript.prober_filename)
                        }
                      >
                        {t("labSetup.downloadPackage", { file: buildScript.prober_filename })}
                      </button>
                    )}
                  </div>
                  {buildScript && (
                    <>
                      <p className="mutedText">{t("labSetup.build.scriptHint", { vmid: buildScript.vmid })}</p>
                      <pre className={styles.code} data-testid="lab-template-script">
                        {buildScript.script}
                      </pre>
                      <CopyButton text={buildScript.script} label={t("labSetup.copyCommands")} />
                    </>
                  )}
                </>
              )}
            </section>
          </>
        );
      case "agent":
        return (
          <>
            <p className="panelIntro">{t("labSetup.agentIntro")}</p>
            <ModeChoice
              name="lab-setup-agent-mode"
              legend={t("labSetup.agentMode")}
              value={agentMode}
              options={[
                { value: "pair", label: t("labSetup.agentModePair") },
                { value: "guided", label: t("labSetup.agentModeGuided") },
                { value: "manual", label: t("labSetup.agentModeManual") },
              ]}
              onChange={setAgentMode}
            />
            {agentMode === "pair" ? (
              <>
                <LabNodePairing
                  clusterId={cluster.id}
                  nodes={selectedNodes}
                  overlayStorageId={overlayStorageId}
                  overlayDir={overlayDir}
                  onPaired={onPaired}
                />
                <ReadinessList readiness={readiness} keys={["lab_agent"]} />
              </>
            ) : agentMode === "guided" ? (
              <ol className={styles.agentSteps}>
                <li>
                  <strong>{t("labSetup.guided.loginStep")}</strong>
                  <SshLoginForm clusterId={cluster.id} nodes={selectedNodes} state={ssh} onChange={setSsh} idPrefix="lab-agent-ssh" />
                </li>
                <li>
                  <strong>{t("labSetup.guided.installStep")}</strong>
                  <div className="formGrid">
                    <div className="field">
                      <label className="label" htmlFor="lab-setup-pbs-token">
                        {t("labSetup.guided.pbsToken")}
                      </label>
                      <input
                        id="lab-setup-pbs-token"
                        className="input"
                        type="password"
                        autoComplete="off"
                        placeholder="user@pbs!lab:xxxxxxxx-…"
                        value={pbsToken}
                        onChange={(e) => setPbsToken(e.target.value)}
                      />
                      <p className="mutedText">{t("labSetup.guided.pbsTokenHint")}</p>
                    </div>
                  </div>
                  <p className="mutedText">{t("labSetup.guided.installHint", { node: ssh.node })}</p>
                  {agentItem?.code !== "token_missing" && <p className="mutedText">{t("labSetup.rotateWarning")}</p>}
                  <div className={styles.inlineActions}>
                    <button
                      type="button"
                      className="button"
                      disabled={!sshLoginPayload(ssh) || agentInstallMutation.isPending}
                      onClick={() => {
                        setError(null);
                        agentInstallMutation.mutate();
                      }}
                    >
                      {t("labSetup.guided.install")}
                    </button>
                  </div>
                  {agentJobId && <LabJobLog clusterId={cluster.id} jobId={agentJobId} onFinished={onAgentJobFinished} />}
                </li>
                <li>
                  <strong>{t("labSetup.guided.waitStep")}</strong>
                  <ReadinessList readiness={readiness} keys={["lab_agent"]} />
                  {agentItem?.status !== "ok" && <p className="mutedText">{t("labSetup.agentPolling")}</p>}
                </li>
              </ol>
            ) : (
              <ol className={styles.agentSteps}>
                <li>
                  <strong>{t("labSetup.agentTokenStep")}</strong>
                  <div className={styles.inlineActions}>
                    <button type="button" className="button" onClick={() => tokenMutation.mutate()} disabled={tokenMutation.isPending}>
                      {cluster.lab_agent_last_seen_at || agentItem?.code !== "token_missing"
                        ? t("labSetup.rotateToken")
                        : t("labSetup.generateToken")}
                    </button>
                    {agentToken && <CopyButton text={agentToken} label={t("labSetup.copyToken")} />}
                  </div>
                  {agentToken ? (
                    <>
                      <pre className={styles.code}>{agentToken}</pre>
                      <InlineAlert tone="info">{t("labSetup.tokenShownOnce")}</InlineAlert>
                    </>
                  ) : (
                    agentItem?.code !== "token_missing" && <p className="mutedText">{t("labSetup.rotateWarning")}</p>
                  )}
                </li>
                {installQuery.data && (
                  <>
                    <li>
                      {installQuery.data.package_download_path ? (
                        <>
                          <strong>{t("labSetup.agentDownloadStep", { package: installQuery.data.package_name })}</strong>
                          <div className={styles.inlineActions}>
                            <button
                              type="button"
                              className="button"
                              onClick={() =>
                                download(
                                  installQuery.data.package_download_path ?? "",
                                  installQuery.data.package_filename ?? `${installQuery.data.package_name}.deb`,
                                )
                              }
                            >
                              {t("labSetup.downloadPackage", { file: installQuery.data.package_filename })}
                            </button>
                          </div>
                          <p className="mutedText">
                            {t("labSetup.agentDownloadHint", { sha256: installQuery.data.package_sha256 })}
                          </p>
                        </>
                      ) : (
                        <>
                          <strong>{t("labSetup.agentBuildStep", { package: installQuery.data.package_name })}</strong>
                          <p className="mutedText">{t("labSetup.agentBuildHint")}</p>
                          <pre className={styles.code}>{installQuery.data.build_steps.join("\n")}</pre>
                        </>
                      )}
                    </li>
                    <li>
                      <strong>{t("labSetup.agentInstallStep")}</strong>
                      <pre className={styles.code}>{installQuery.data.install_steps.join("\n")}</pre>
                      <CopyButton text={installQuery.data.install_steps.join("\n")} label={t("labSetup.copyCommands")} />
                    </li>
                    <li>
                      <strong>{t("labSetup.agentEnvStep", { path: installQuery.data.env_file_path })}</strong>
                      {!agentToken && <p className="mutedText">{t("labSetup.envNeedsToken")}</p>}
                      <pre className={styles.code} data-testid="lab-agent-env-file">
                        {envFile}
                      </pre>
                      {envFile && <CopyButton text={envFile} label={t("labSetup.copyEnvFile")} />}
                      <p className="mutedText">{t("labSetup.envPbsTokenHint")}</p>
                    </li>
                  </>
                )}
                <li>
                  <strong>{t("labSetup.agentWaitStep")}</strong>
                  <ReadinessList readiness={readiness} keys={["lab_agent"]} />
                  {agentItem?.status !== "ok" && <p className="mutedText">{t("labSetup.agentPolling")}</p>}
                </li>
              </ol>
            )}
          </>
        );
      case "summary":
      default:
        return (
          <>
            <p className="panelIntro">{readiness?.ready ? t("labSetup.summaryReady") : t("labSetup.summaryIncomplete")}</p>
            <ReadinessList readiness={readiness} keys={readiness?.items.map((item) => item.key) ?? []} />
            {readiness && !readiness.ready && <InlineAlert tone="info">{t("labSetup.activateWithWarnings")}</InlineAlert>}
          </>
        );
    }
  };

  return (
    <Modal open size="large" title={t("labSetup.title", { name: cluster.display_name })} onClose={onClose}>
      {error && (
        <div className={styles.alert}>
          <InlineAlert>{error}</InlineAlert>
        </div>
      )}
      {notice && (
        <div className={styles.alert}>
          <InlineAlert tone="info">{notice}</InlineAlert>
        </div>
      )}
      <Wizard
        steps={steps}
        currentStep={step}
        onStepClick={(index) => setStep(index)}
        canAdvance={!saveMutation.isPending && !finishMutation.isPending}
        onBack={() => setStep((s) => Math.max(0, s - 1))}
        onNext={goNext}
        nextLabel={cluster.is_lab_compute ? t("labSetup.finishAlreadyLab") : t("labSetup.finish")}
      >
        <div className={styles.toolbar}>
          {savesConfig ? (
            <button
              type="button"
              className="button"
              onClick={() => {
                setError(null);
                if (!uplinkValid) {
                  setError(t("labSetup.uplink.fixErrors"));
                  return;
                }
                saveMutation.mutate(undefined, { onError: (err) => setError(errorText(err)) });
              }}
              disabled={saveMutation.isPending || readinessQuery.isFetching}
            >
              {saveMutation.isPending || readinessQuery.isFetching ? t("labSetup.checking") : t("labSetup.saveAndCheck")}
            </button>
          ) : (
            <button type="button" className="button" onClick={() => readinessQuery.refetch()} disabled={readinessQuery.isFetching}>
              {readinessQuery.isFetching ? t("labSetup.checking") : t("labSetup.recheck")}
            </button>
          )}
          {savesConfig && <span className="mutedText">{t("labSetup.nextSaves")}</span>}
        </div>
        {renderStep()}
      </Wizard>
    </Modal>
  );
}
