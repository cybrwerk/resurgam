import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { pveClustersApi } from "../api/resources";
import type { LabSetupJobOut, LabSetupJobStepStatus, SshHostKeyOut, SshLoginRequest } from "../api/types";
import { InlineAlert } from "./InlineAlert";
import { StatusBadge, type StatusTone } from "./StatusBadge";
import styles from "./LabComputeSetup.module.css";

/** How often a running guided job's step log is refreshed. */
export const JOB_POLL_MS = 2000;

/** The one-time SSH login form's state. Lives only in the open wizard's
 * memory: never stored, and dropped when the wizard closes. */
export interface SshFormState {
  node: string;
  host: string;
  port: string;
  username: string;
  authMethod: "password" | "private_key";
  password: string;
  privateKey: string;
  passphrase: string;
  hostKey: SshHostKeyOut | null;
  confirmed: boolean;
}

export function emptySshForm(node = ""): SshFormState {
  return {
    node,
    host: "",
    port: "22",
    username: "root",
    authMethod: "password",
    password: "",
    privateKey: "",
    passphrase: "",
    hostKey: null,
    confirmed: false,
  };
}

/** The login payload once the host key is confirmed and a secret is entered; else null. */
export function sshLoginPayload(state: SshFormState): SshLoginRequest | null {
  if (!state.hostKey || !state.confirmed || !state.node) return null;
  const secret = state.authMethod === "password" ? state.password : state.privateKey.trim();
  if (!secret) return null;
  return {
    node: state.node,
    host: state.host.trim(),
    port: Number(state.port) || 22,
    username: state.username.trim() || "root",
    auth_method: state.authMethod,
    password: state.authMethod === "password" ? state.password : null,
    private_key: state.authMethod === "private_key" ? state.privateKey : null,
    passphrase: state.authMethod === "private_key" && state.passphrase ? state.passphrase : null,
    confirmed_fingerprint: state.hostKey.fingerprint_sha256,
  };
}

function errorText(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

/** Host, port, user and password/key for a one-time SSH session, with the
 * host key fingerprint fetched first (no credentials sent) and confirmed
 * by the admin before any credential can be used. */
export function SshLoginForm({
  clusterId,
  nodes,
  state,
  onChange,
  idPrefix,
}: {
  clusterId: string;
  nodes: string[];
  state: SshFormState;
  onChange: (next: SshFormState) => void;
  idPrefix: string;
}) {
  const { t } = useTranslation();
  const [error, setError] = useState<string | null>(null);
  const set = (patch: Partial<SshFormState>) => onChange({ ...state, ...patch });
  // A different host/port/node invalidates a confirmed fingerprint.
  const setTarget = (patch: Partial<SshFormState>) => set({ ...patch, hostKey: null, confirmed: false });

  const targetQuery = useQuery({
    queryKey: ["pve-lab-ssh-target", clusterId, state.node],
    queryFn: () => pveClustersApi.labSshTarget(clusterId, state.node),
    enabled: Boolean(state.node) && state.host === "",
  });
  useEffect(() => {
    if (state.host === "" && targetQuery.data && targetQuery.data.node === state.node) {
      onChange({ ...state, host: targetQuery.data.host, port: String(targetQuery.data.port) });
    }
  }, [targetQuery.data, state, onChange]);

  const hostKeyMutation = useMutation({
    mutationFn: () =>
      pveClustersApi.labSshHostKey(clusterId, { node: state.node, host: state.host.trim(), port: Number(state.port) || 22 }),
    onSuccess: (hostKey) => {
      setError(null);
      set({ hostKey, confirmed: false });
    },
    onError: (err) => setError(errorText(err, t("common.loadError"))),
  });

  const id = (name: string) => `${idPrefix}-${name}`;
  return (
    <div className={styles.sshForm}>
      <p className="mutedText">{t("labSetup.ssh.intro")}</p>
      <div className="formGrid">
        <div className="field">
          <label className="label" htmlFor={id("node")}>
            {t("labSetup.ssh.node")}
          </label>
          <select id={id("node")} className="select" value={state.node} onChange={(e) => setTarget({ node: e.target.value, host: "" })}>
            {nodes.map((node) => (
              <option key={node} value={node}>
                {node}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label className="label" htmlFor={id("host")}>
            {t("labSetup.ssh.host")}
          </label>
          <input id={id("host")} className="input" value={state.host} onChange={(e) => setTarget({ host: e.target.value })} />
          {targetQuery.data?.host_source === "cluster_host" && <p className="mutedText">{t("labSetup.ssh.hostFallback")}</p>}
        </div>
        <div className="field">
          <label className="label" htmlFor={id("port")}>
            {t("labSetup.ssh.port")}
          </label>
          <input
            id={id("port")}
            className="input"
            inputMode="numeric"
            value={state.port}
            onChange={(e) => setTarget({ port: e.target.value.replace(/\D/g, "") })}
          />
        </div>
      </div>

      <div className={styles.inlineActions}>
        <button
          type="button"
          className="button"
          disabled={!state.node || !state.host.trim() || hostKeyMutation.isPending}
          onClick={() => hostKeyMutation.mutate()}
        >
          {hostKeyMutation.isPending ? t("labSetup.checking") : t("labSetup.ssh.fetchHostKey")}
        </button>
      </div>
      {error && <InlineAlert>{error}</InlineAlert>}

      {state.hostKey && (
        <div className={styles.confirmBox} data-testid={`${idPrefix}-host-key`}>
          <p>{t("labSetup.ssh.hostKeyIntro", { host: state.hostKey.host, type: state.hostKey.key_type })}</p>
          <pre className={styles.code}>{state.hostKey.fingerprint_sha256}</pre>
          <p className="mutedText">{t("labSetup.ssh.hostKeyVerify")}</p>
          <pre className={styles.code}>{state.hostKey.verify_command}</pre>
          <label className={styles.checkOption}>
            <input type="checkbox" checked={state.confirmed} onChange={(e) => set({ confirmed: e.target.checked })} />
            {t("labSetup.ssh.confirmHostKey")}
          </label>
        </div>
      )}

      {state.hostKey && state.confirmed && (
        <>
          <div className="formGrid">
            <div className="field">
              <label className="label" htmlFor={id("username")}>
                {t("labSetup.ssh.username")}
              </label>
              <input
                id={id("username")}
                className="input"
                autoComplete="off"
                value={state.username}
                onChange={(e) => set({ username: e.target.value })}
              />
              <p className="mutedText">{t("labSetup.ssh.usernameHint")}</p>
            </div>
          </div>
          <fieldset className={styles.nodeGroup}>
            <legend className="label">{t("labSetup.ssh.authMethod")}</legend>
            <label className={styles.checkOption}>
              <input
                type="radio"
                name={id("auth")}
                checked={state.authMethod === "password"}
                onChange={() => set({ authMethod: "password" })}
              />
              {t("labSetup.ssh.authPassword")}
            </label>
            <label className={styles.checkOption}>
              <input
                type="radio"
                name={id("auth")}
                checked={state.authMethod === "private_key"}
                onChange={() => set({ authMethod: "private_key" })}
              />
              {t("labSetup.ssh.authKey")}
            </label>
          </fieldset>
          <div className="formGrid">
            {state.authMethod === "password" ? (
              <div className="field">
                <label className="label" htmlFor={id("password")}>
                  {t("labSetup.ssh.password")}
                </label>
                <input
                  id={id("password")}
                  className="input"
                  type="password"
                  autoComplete="off"
                  value={state.password}
                  onChange={(e) => set({ password: e.target.value })}
                />
              </div>
            ) : (
              <>
                <div className="field">
                  <label className="label" htmlFor={id("key")}>
                    {t("labSetup.ssh.privateKey")}
                  </label>
                  <textarea
                    id={id("key")}
                    className={`input ${styles.keyInput}`}
                    rows={5}
                    spellCheck={false}
                    autoComplete="off"
                    placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                    value={state.privateKey}
                    onChange={(e) => set({ privateKey: e.target.value })}
                  />
                </div>
                <div className="field">
                  <label className="label" htmlFor={id("passphrase")}>
                    {t("labSetup.ssh.passphrase")}
                  </label>
                  <input
                    id={id("passphrase")}
                    className="input"
                    type="password"
                    autoComplete="off"
                    value={state.passphrase}
                    onChange={(e) => set({ passphrase: e.target.value })}
                  />
                </div>
              </>
            )}
          </div>
          <p className="mutedText">{t("labSetup.ssh.oneTime")}</p>
        </>
      )}
    </div>
  );
}

const STEP_TONE: Record<LabSetupJobStepStatus, StatusTone> = {
  running: "info",
  ok: "ok",
  warning: "warning",
  failed: "danger",
  skipped: "unknown",
};

/** Polls a guided job and shows its live step log. */
export function LabJobLog({
  clusterId,
  jobId,
  onFinished,
}: {
  clusterId: string;
  jobId: string;
  onFinished?: (job: LabSetupJobOut) => void;
}) {
  const { t } = useTranslation();
  const query = useQuery({
    queryKey: ["pve-lab-setup-job", clusterId, jobId],
    queryFn: () => pveClustersApi.labSetupJob(clusterId, jobId),
    refetchInterval: (q) => ((q.state.data as LabSetupJobOut | undefined)?.status === "running" ? JOB_POLL_MS : false),
  });
  const job = query.data;
  const [reported, setReported] = useState<string | null>(null);
  useEffect(() => {
    if (job && job.status !== "running" && reported !== job.id) {
      setReported(job.id);
      onFinished?.(job);
    }
  }, [job, reported, onFinished]);

  if (query.isError) return <InlineAlert>{errorText(query.error, t("common.loadError"))}</InlineAlert>;
  if (!job) return <p className="mutedText">{t("labSetup.checking")}</p>;
  return (
    <div className={styles.jobLog} data-testid="lab-job-log">
      <div className={styles.inlineActions}>
        <StatusBadge
          tone={job.status === "succeeded" ? "ok" : job.status === "failed" ? "danger" : "info"}
          label={t(`labSetup.job.status.${job.status}`)}
        />
        {job.status === "running" && <span className="mutedText">{t("labSetup.job.polling")}</span>}
      </div>
      <ul className={styles.readinessList}>
        {job.steps.map((step, index) => (
          <li key={`${step.key}-${index}`} className={styles.readinessRow}>
            <StatusBadge tone={STEP_TONE[step.status]} label={t(`labSetup.job.stepStatus.${step.status}`)} />
            <div>
              <div className={styles.readinessTitle}>{t(`labSetup.job.steps.${step.key}`, { defaultValue: step.key })}</div>
              <div className="mutedText">{step.message}</div>
              {step.output && (
                <details>
                  <summary>{t("labSetup.job.output")}</summary>
                  <pre className={styles.code}>{step.output}</pre>
                </details>
              )}
            </div>
          </li>
        ))}
      </ul>
      {job.status === "failed" && job.error_code && (
        <InlineAlert>
          {t(`labSetup.job.errors.${job.error_code}`, { defaultValue: job.error_message ?? job.error_code })}
        </InlineAlert>
      )}
    </div>
  );
}
