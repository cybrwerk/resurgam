import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { pveClustersApi } from "../api/resources";
import type { LabNodePairingOut } from "../api/types";
import { CopyButton } from "./CopyButton";
import { InlineAlert } from "./InlineAlert";
import { StatusBadge } from "./StatusBadge";
import styles from "./LabComputeSetup.module.css";

const POLL_MS = 3000;

interface LabNodePairingProps {
  clusterId: string;
  nodes: string[];
  overlayStorageId: string;
  overlayDir: string;
  /** Called once when the node used the code. */
  onPaired: (pairing: LabNodePairingOut) => void;
}

/** "Pair node" (ADR 0027): one command, pasted into the node's own Proxmox
 * web shell, installs and registers the lab agent with a one-time code --
 * no SSH login or password in RESURGAM. */
export function LabNodePairing({ clusterId, nodes, overlayStorageId, overlayDir, onPaired }: LabNodePairingProps) {
  const { t } = useTranslation();
  const [node, setNode] = useState(nodes[0] ?? "");
  const [created, setCreated] = useState<LabNodePairingOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reported = useRef<string | null>(null);

  useEffect(() => {
    if (!node && nodes.length > 0) setNode(nodes[0]);
  }, [node, nodes]);

  const create = useMutation({
    mutationFn: () =>
      pveClustersApi.createLabPairing(clusterId, { node, overlay_storage_id: overlayStorageId, overlay_dir: overlayDir }),
    onSuccess: (pairing) => {
      setError(null);
      setCreated(pairing);
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const status = useQuery({
    queryKey: ["lab-pairing", clusterId, created?.id],
    queryFn: () => pveClustersApi.labPairing(clusterId, created?.id ?? ""),
    enabled: created !== null,
    refetchInterval: (query) => ((query.state.data?.state ?? "waiting") === "waiting" ? POLL_MS : false),
  });
  const current = status.data ?? created;

  useEffect(() => {
    if (current?.state === "paired" && reported.current !== current.id) {
      reported.current = current.id;
      onPaired(current);
    }
  }, [current, onPaired]);

  return (
    <div data-testid="lab-node-pairing">
      <p className="mutedText">{t("labSetup.pair.intro")}</p>
      {error && <InlineAlert>{error}</InlineAlert>}
      <div className="formGrid">
        <div className="field">
          <label className="label" htmlFor="lab-pair-node">
            {t("labSetup.ssh.node")}
          </label>
          <select id="lab-pair-node" className="select" value={node} onChange={(e) => setNode(e.target.value)}>
            {nodes.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </div>
      </div>
      <div className={styles.inlineActions}>
        <button
          type="button"
          className={created ? "button" : "button buttonPrimary"}
          disabled={!node || create.isPending}
          onClick={() => create.mutate()}
        >
          {created ? t("labSetup.pair.newCode") : t("labSetup.pair.create")}
        </button>
        {created && <span className="mutedText">{t("labSetup.pair.newCodeHint")}</span>}
      </div>

      {created?.command && current && (
        <ol className={styles.agentSteps}>
          <li>
            <strong>{t("labSetup.pair.openShell", { node: created.node })}</strong>
            <div className={styles.inlineActions}>
              <a className={`button ${styles.linkButton}`} href={created.shell_url} target="_blank" rel="noreferrer">
                {t("labSetup.pair.openShellButton")}
              </a>
            </div>
            <p className="mutedText">{t("labSetup.pair.openShellHint")}</p>
          </li>
          <li>
            <strong>{t("labSetup.pair.pasteCommand")}</strong>
            <pre className={`${styles.code} ${styles.wrapCode}`} data-testid="lab-pair-command">
              {created.command}
            </pre>
            <div className={styles.inlineActions}>
              <CopyButton text={created.command} label={t("labSetup.pair.copyCommand")} />
            </div>
            <p className="mutedText">
              {t("labSetup.pair.commandHint", { expires: new Date(created.expires_at).toLocaleTimeString() })}
            </p>
          </li>
          <li>
            <strong>{t("labSetup.pair.waitStep")}</strong>{" "}
            <span data-testid="lab-pair-state">
              <StatusBadge
                tone={current.state === "paired" ? "ok" : current.state === "waiting" ? "info" : "warning"}
                label={t(`labSetup.pair.state.${current.state}`)}
              />
            </span>
            {current.state === "paired" && (
              <InlineAlert tone="success">{t("labSetup.pair.paired", { node: current.node })}</InlineAlert>
            )}
            {current.state === "paired" && current.pbs_token_provided === false && (
              <InlineAlert tone="info">{t("labSetup.pair.pbsMissing")}</InlineAlert>
            )}
            {(current.state === "expired" || current.state === "revoked") && (
              <p className="mutedText">{t("labSetup.pair.expired")}</p>
            )}
          </li>
        </ol>
      )}
    </div>
  );
}
