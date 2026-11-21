import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { ApiError } from "../api/client";
import { pbsInstancesApi, pbsJobsApi } from "../api/resources";
import type { PbsInstanceOut, PbsRemoteOut, TestConnectionResponse, TlsTrustMode } from "../api/types";
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
import { InlineAlert } from "../components/InlineAlert";
import { Modal } from "../components/Modal";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { QueryState } from "../components/QueryState";
import styles from "./PbsInstancesPage.module.css";

const QUERY_KEY = ["pbs-instances"];

function CreatePbsInstanceForm({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [displayName, setDisplayName] = useState("");
  const [location, setLocation] = useState("");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("8007");
  const [trustMode, setTrustMode] = useState<TlsTrustMode>("ca");
  const [fingerprint, setFingerprint] = useState("");
  const [token, setToken] = useState<TokenFormState>(emptyTokenState);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<PbsInstanceOut | null>(null);

  const probeMutation = useMutation({
    mutationFn: () => pbsInstancesApi.probeTls(host, parsePort(port) ?? 8007),
    onSuccess: (result) => setFingerprint(result.fingerprint_sha256),
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  // The connection test runs automatically right after the instance was
  // saved; the button below only re-runs it.
  const testMutation = useMutation({
    mutationFn: (id: string) => pbsInstancesApi.testConnection(id),
    onSettled: () => queryClient.invalidateQueries({ queryKey: QUERY_KEY }),
  });

  const createMutation = useMutation({
    mutationFn: (validPort: number) =>
      pbsInstancesApi.create({
        display_name: displayName,
        location: location || null,
        host,
        port: validPort,
        tls_trust_mode: trustMode,
        tls_fingerprint_sha256: trustMode === "fingerprint" ? fingerprint : null,
        ...tokenPayload(token, false),
      }),
    onSuccess: (instance) => {
      setToken(emptyTokenState());
      setCreated(instance);
      queryClient.invalidateQueries({ queryKey: QUERY_KEY });
      testMutation.mutate(instance.id);
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    const validPort = parsePort(port);
    if (validPort === null) {
      setError(t("connection.portInvalid"));
      return;
    }
    createMutation.mutate(validPort);
  };

  if (created) {
    return (
      <Panel title={t("connection.createdTitle", { name: created.display_name })} className={styles.createPanel}>
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
    <Panel title={t("pbsInstances.createTitle")} className={styles.createPanel}>
      <form onSubmit={handleSubmit}>
        {error && (
          <div className={styles.alert}>
            <InlineAlert>{error}</InlineAlert>
          </div>
        )}
        <div className="formGrid">
          <div className="field">
            <label className="label" htmlFor="pbs-display-name">
              {t("pbsInstances.displayName")}
            </label>
            <input
              id="pbs-display-name"
              className="input"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="pbs-location">
              {t("pbsInstances.location")}
            </label>
            <input id="pbs-location" className="input" value={location} onChange={(e) => setLocation(e.target.value)} />
          </div>
          <div className="field">
            <label className="label" htmlFor="pbs-host">
              {t("pbsInstances.host")}
            </label>
            <input id="pbs-host" className="input" value={host} onChange={(e) => setHost(e.target.value)} required />
          </div>
          <div className="field">
            <label className="label" htmlFor="pbs-port">
              {t("pbsInstances.port")}
            </label>
            <input
              id="pbs-port"
              type="number"
              className="input"
              value={port}
              onChange={(e) => setPort(e.target.value)}
              aria-describedby="pbs-port-hint"
              required
            />
            <p id="pbs-port-hint" className="mutedText">
              {t("connection.portHint", { port: 8007 })}
            </p>
          </div>
          <div className="field">
            <label className="label" htmlFor="pbs-trust-mode">
              {t("pbsInstances.trustMode")}
            </label>
            <select
              id="pbs-trust-mode"
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
              <div className={styles.fingerprintRow}>
                <button
                  type="button"
                  className="button"
                  onClick={() => probeMutation.mutate()}
                  disabled={!host || parsePort(port) === null || probeMutation.isPending}
                >
                  {t("pbsInstances.probeFingerprint")}
                </button>
                {fingerprint && (
                  <span className={styles.fingerprintValue} title={t("pbsInstances.confirmFingerprint")}>
                    {fingerprint}
                  </span>
                )}
              </div>
            </div>
          )}

          <TokenFields product="pbs" idPrefix="pbs" state={token} onChange={setToken} />
          <RequiredPermissionsPanel product="pbs" fetcher={pbsInstancesApi.requiredPrivileges} />
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

function RemoteFormModal({
  instanceId,
  remote,
  onClose,
}: {
  instanceId: string;
  remote: PbsRemoteOut | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [name, setName] = useState(remote?.name ?? "");
  const [host, setHost] = useState(remote?.host ?? "");
  const [authId, setAuthId] = useState(remote?.auth_id ?? "");
  const [password, setPassword] = useState("");
  const [port, setPort] = useState(remote?.port?.toString() ?? "");
  const [fingerprint, setFingerprint] = useState(remote?.fingerprint ?? "");
  const [comment, setComment] = useState(remote?.comment ?? "");
  const [error, setError] = useState<string | null>(null);

  const queryKey = ["pbs-remotes", instanceId];
  const saveMutation = useMutation({
    mutationFn: () =>
      remote
        ? pbsJobsApi.updateRemote(instanceId, remote.name, {
            host: host || null,
            auth_id: authId || null,
            password: password || null,
            port: port.trim() === "" ? null : Number(port),
            fingerprint: fingerprint || null,
            comment: comment || null,
          })
        : pbsJobsApi.createRemote(instanceId, {
            name,
            host,
            auth_id: authId,
            password,
            port: port.trim() === "" ? null : Number(port),
            fingerprint: fingerprint || null,
            comment: comment || null,
          }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
      onClose();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  return (
    <Modal open title={remote ? t("pbsRemotes.editTitle") : t("pbsRemotes.createTitle")} onClose={onClose}>
      <form
        className={styles.createForm}
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          setError(null);
          saveMutation.mutate();
        }}
      >
        {error && (
          <div className={styles.fullRow}>
            <InlineAlert>{error}</InlineAlert>
          </div>
        )}
        <div className="field">
          <label className="label" htmlFor="remote-name">
            {t("pbsRemotes.name")}
          </label>
          <input
            id="remote-name"
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            disabled={remote !== null}
          />
        </div>
        <div className="field">
          <label className="label" htmlFor="remote-host">
            {t("pbsRemotes.host")}
          </label>
          <input id="remote-host" className="input" value={host} onChange={(e) => setHost(e.target.value)} required={!remote} />
        </div>
        <div className="field">
          <label className="label" htmlFor="remote-auth-id">
            {t("pbsRemotes.authId")}
          </label>
          <input id="remote-auth-id" className="input" value={authId} onChange={(e) => setAuthId(e.target.value)} required={!remote} />
        </div>
        <div className="field">
          <label className="label" htmlFor="remote-password">
            {t("pbsRemotes.password")}
          </label>
          <input
            id="remote-password"
            type="password"
            className="input"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required={!remote}
            placeholder={remote ? t("common.leaveBlankToKeep") : ""}
          />
          <p className="mutedText">{t("pbsRemotes.passwordHint")}</p>
        </div>
        <div className="field">
          <label className="label" htmlFor="remote-port">
            {t("pbsRemotes.port")}
          </label>
          <input id="remote-port" type="number" min={1} max={65535} className="input" value={port} onChange={(e) => setPort(e.target.value)} />
        </div>
        <div className="field">
          <label className="label" htmlFor="remote-fingerprint">
            {t("pbsRemotes.fingerprint")}
          </label>
          <input id="remote-fingerprint" className="input" value={fingerprint} onChange={(e) => setFingerprint(e.target.value)} />
        </div>
        <div className={`field ${styles.fullRow}`}>
          <label className="label" htmlFor="remote-comment">
            {t("pbsRemotes.comment")}
          </label>
          <input id="remote-comment" className="input" value={comment} onChange={(e) => setComment(e.target.value)} />
        </div>
        <div className="formActions">
          <button type="button" className="button" onClick={onClose} disabled={saveMutation.isPending}>
            {t("common.cancel")}
          </button>
          <button type="submit" className="button buttonPrimary" disabled={saveMutation.isPending}>
            {t("common.save")}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function PbsRemotesSection({ instanceId }: { instanceId: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [pendingDelete, setPendingDelete] = useState<PbsRemoteOut | null>(null);
  const [editingRemote, setEditingRemote] = useState<PbsRemoteOut | null | "new">(null);
  const queryKey = ["pbs-remotes", instanceId];

  const capabilitiesQuery = useQuery({
    queryKey: ["pbs-remote-capabilities", instanceId],
    queryFn: () => pbsJobsApi.remoteCapabilities(instanceId),
  });
  const canWrite = capabilitiesQuery.data?.can_write ?? false;

  const remotesQuery = useQuery({ queryKey, queryFn: () => pbsJobsApi.remotes(instanceId) });
  const deleteMutation = useMutation({
    mutationFn: (name: string) => pbsJobsApi.deleteRemote(instanceId, name),
    onSuccess: () => queryClient.invalidateQueries({ queryKey }),
  });

  const columns: DataTableColumn<PbsRemoteOut>[] = [
    { key: "name", header: t("pbsRemotes.name"), render: (row) => row.name },
    { key: "host", header: t("pbsRemotes.host"), render: (row) => row.host },
    { key: "auth_id", header: t("pbsRemotes.authId"), render: (row) => row.auth_id ?? "—" },
    {
      key: "actions",
      header: t("common.actions"),
      render: (row) => (
        <span className={styles.rowActions}>
          <button type="button" className="button" onClick={() => setEditingRemote(row)} disabled={!canWrite}>
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
      title={t("pbsRemotes.title")}
      className={styles.remotesPanel}
      actions={
        <button type="button" className="button" onClick={() => setEditingRemote("new")} disabled={!canWrite}>
          {t("common.create")}
        </button>
      }
    >
      <p className="panelIntro">{t("pbsRemotes.hint")}</p>
      {capabilitiesQuery.data && !canWrite && (
        <div className={styles.alert}>
          <InlineAlert tone="info">{t("common.capabilitiesReadOnly")}</InlineAlert>
        </div>
      )}
      <QueryState query={remotesQuery} isEmpty={(data) => data.length === 0}>
        {(remotes) => <DataTable columns={columns} rows={remotes} rowKey={(row) => row.name} />}
      </QueryState>
      <ConfirmActionDialog
        open={pendingDelete !== null}
        title={t("pbsRemotes.confirmDeleteTitle")}
        body={t("pbsRemotes.confirmDeleteBody", { name: pendingDelete?.name ?? "" })}
        confirmLabel={t("common.delete")}
        danger
        onConfirm={() => {
          if (pendingDelete) deleteMutation.mutate(pendingDelete.name);
          setPendingDelete(null);
        }}
        onCancel={() => setPendingDelete(null)}
      />
      {editingRemote !== null && (
        <RemoteFormModal instanceId={instanceId} remote={editingRemote === "new" ? null : editingRemote} onClose={() => setEditingRemote(null)} />
      )}
    </Panel>
  );
}

function PbsInstanceDetailModal({ instance, onClose }: { instance: PbsInstanceOut; onClose: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [displayName, setDisplayName] = useState(instance.display_name);
  const [location, setLocation] = useState(instance.location ?? "");
  const [host, setHost] = useState(instance.host);
  const [port, setPort] = useState(String(instance.port));
  const [trustMode, setTrustMode] = useState<TlsTrustMode>(instance.tls_trust_mode);
  const [fingerprint, setFingerprint] = useState(instance.tls_fingerprint_sha256 ?? "");
  const [token, setToken] = useState<TokenFormState>(emptyTokenState);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setEditing(false);
    setDisplayName(instance.display_name);
    setLocation(instance.location ?? "");
    setHost(instance.host);
    setPort(String(instance.port));
    setTrustMode(instance.tls_trust_mode);
    setFingerprint(instance.tls_fingerprint_sha256 ?? "");
    setToken(emptyTokenState());
    setError(null);
    // Only reset when a different instance is shown, not on every refetch
    // (the automatic post-save test refreshes the list).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instance.id]);

  const probeMutation = useMutation({
    mutationFn: () => pbsInstancesApi.probeTls(host, parsePort(port) ?? instance.port),
    onSuccess: (result) => setFingerprint(result.fingerprint_sha256),
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const testMutation = useMutation({
    mutationFn: () => pbsInstancesApi.testConnection(instance.id),
    onSettled: () => queryClient.invalidateQueries({ queryKey: QUERY_KEY }),
  });

  const updateMutation = useMutation({
    mutationFn: (validPort: number) =>
      pbsInstancesApi.update(instance.id, {
        display_name: displayName,
        location: location || null,
        host,
        port: validPort,
        tls_trust_mode: trustMode,
        tls_fingerprint_sha256: trustMode === "fingerprint" ? fingerprint : null,
        ...tokenPayload(token, true),
      }),
    onSuccess: (_updated, validPort) => {
      const connectionChanged =
        host !== instance.host ||
        validPort !== instance.port ||
        trustMode !== instance.tls_trust_mode ||
        (trustMode === "fingerprint" && fingerprint !== (instance.tls_fingerprint_sha256 ?? "")) ||
        tokenChanged(token, true);
      queryClient.invalidateQueries({ queryKey: QUERY_KEY });
      setEditing(false);
      setToken(emptyTokenState());
      if (connectionChanged) testMutation.mutate();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  if (!editing) {
    return (
      <Modal open title={instance.display_name} onClose={onClose}>
        <dl className={`detailGrid ${styles.modalDetails}`}>
          <dt>{t("pbsInstances.location")}</dt>
          <dd>{instance.location || "—"}</dd>
          <dt>{t("pbsInstances.host")}</dt>
          <dd>
            {instance.host}:{instance.port}
          </dd>
          <dt>{t("pbsInstances.trustMode")}</dt>
          <dd>
            {instance.tls_trust_mode === "fingerprint" ? t("pbsInstances.trustModeFingerprint") : t("pbsInstances.trustModeCa")}
          </dd>
          {instance.tls_fingerprint_sha256 && (
            <>
              <dt>{t("pbsInstances.fingerprint")}</dt>
              <dd className={styles.fingerprintValue}>{instance.tls_fingerprint_sha256}</dd>
            </>
          )}
          <dt>{t("connection.tokenId")}</dt>
          <dd>{instance.token_id ? <code>{instance.token_id}</code> : "—"}</dd>
          <dt>{t("pbsInstances.healthStatus")}</dt>
          <dd>
            <HealthCell status={instance.last_health_status} checkedAt={instance.last_health_check_at} secretSet={instance.token_secret_set !== false} />
          </dd>
          <dt>{t("pbsInstances.version")}</dt>
          <dd>{instance.version ?? "—"}</dd>
          <dt>{t("pbsInstances.serverTimezone")}</dt>
          <dd title={t("pbsInstances.serverTimezoneHint")}>{instance.server_timezone ?? "—"}</dd>
        </dl>

        {(instance.instant_recoveries_reading?.length ?? 0) > 0 && (
          <InlineAlert tone="danger">
            {t("instantRecovery.pbsWarning", {
              count: instance.instant_recoveries_reading!.length,
              datastores: Array.from(new Set(instance.instant_recoveries_reading)).join(", "),
            })}
          </InlineAlert>
        )}

        <ConnectionTestResult
          result={testMutation.data ?? null}
          pending={testMutation.isPending}
          error={testMutation.error ? (testMutation.error as Error).message : null}
        />

        <PbsRemotesSection instanceId={instance.id} />

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
    <Modal open title={instance.display_name} onClose={onClose}>
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
          <div className={styles.fullRow}>
            <InlineAlert>{error}</InlineAlert>
          </div>
        )}
        <div className="field">
          <label className="label" htmlFor="edit-pbs-display-name">
            {t("pbsInstances.displayName")}
          </label>
          <input
            id="edit-pbs-display-name"
            className="input"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            required
          />
        </div>
        <div className="field">
          <label className="label" htmlFor="edit-pbs-location">
            {t("pbsInstances.location")}
          </label>
          <input
            id="edit-pbs-location"
            className="input"
            value={location}
            onChange={(e) => setLocation(e.target.value)}
          />
        </div>
        <div className="field">
          <label className="label" htmlFor="edit-pbs-host">
            {t("pbsInstances.host")}
          </label>
          <input id="edit-pbs-host" className="input" value={host} onChange={(e) => setHost(e.target.value)} required />
        </div>
        <div className="field">
          <label className="label" htmlFor="edit-pbs-port">
            {t("pbsInstances.port")}
          </label>
          <input
            id="edit-pbs-port"
            type="number"
            className="input"
            value={port}
            onChange={(e) => setPort(e.target.value)}
            required
          />
        </div>
        <div className="field">
          <label className="label" htmlFor="edit-pbs-trust-mode">
            {t("pbsInstances.trustMode")}
          </label>
          <select
            id="edit-pbs-trust-mode"
            className="select"
            value={trustMode}
            onChange={(e) => setTrustMode(e.target.value as TlsTrustMode)}
          >
            <option value="ca">{t("pbsInstances.trustModeCa")}</option>
            <option value="fingerprint">{t("pbsInstances.trustModeFingerprint")}</option>
          </select>
        </div>

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

        <TokenFields
          product="pbs"
          idPrefix="edit-pbs"
          state={token}
          onChange={setToken}
          editing
          storedTokenId={instance.token_id}
        />
        <RequiredPermissionsPanel product="pbs" fetcher={pbsInstancesApi.requiredPrivileges} />

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

export function PbsInstancesPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [pendingDisable, setPendingDisable] = useState<PbsInstanceOut | null>(null);
  const [selectedInstance, setSelectedInstance] = useState<PbsInstanceOut | null>(null);
  const [testResult, setTestResult] = useState<Record<string, TestConnectionResponse>>({});
  const [searchParams, setSearchParams] = useSearchParams();

  const query = useQuery({ queryKey: QUERY_KEY, queryFn: pbsInstancesApi.list });

  // Deep-link support: the Replication & Sync page's "Manage remotes on
  // X" link has nowhere else to point (there's no per-instance route,
  // this whole page is a flat list + row-click modal, like every other
  // list page in this app) -- raised directly by the user after that
  // link went to a blank page. `?open=<id>` opens that instance's own
  // detail modal (where remotes actually live) once the list has
  // loaded, then clears the param so it doesn't re-trigger on its own
  // close/reopen.
  useEffect(() => {
    const openId = searchParams.get("open");
    if (!openId || !query.data) return;
    const match = query.data.find((i) => i.id === openId);
    if (match) setSelectedInstance(match);
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete("open");
        return next;
      },
      { replace: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams, query.data]);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: QUERY_KEY });

  const testMutation = useMutation({
    mutationFn: (id: string) => pbsInstancesApi.testConnection(id),
    onSuccess: (result, id) => {
      setTestResult((prev) => ({ ...prev, [id]: result }));
      invalidate();
    },
  });
  const disableMutation = useMutation({
    mutationFn: (id: string) => pbsInstancesApi.disable(id),
    onSuccess: invalidate,
  });
  const enableMutation = useMutation({
    mutationFn: (id: string) => pbsInstancesApi.enable(id),
    onSuccess: invalidate,
  });

  // Keep the open modal's data fresh after a mutation invalidates the list.
  const selected = selectedInstance ? query.data?.find((i) => i.id === selectedInstance.id) ?? selectedInstance : null;

  const columns: DataTableColumn<PbsInstanceOut>[] = [
    { key: "name", header: t("common.name"), render: (row) => row.display_name },
    { key: "host", header: t("pbsInstances.host"), render: (row) => `${row.host}:${row.port}` },
    {
      key: "health",
      header: t("pbsInstances.healthStatus"),
      render: (row) => <HealthCell status={row.last_health_status} checkedAt={row.last_health_check_at} secretSet={row.token_secret_set !== false} />,
    },
    { key: "version", header: t("pbsInstances.version"), render: (row) => row.version ?? "—" },
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
        title={t("nav.infrastructurePbsInstances")}
        actions={
          !showCreateForm && (
            <button type="button" className="button buttonPrimary" onClick={() => setShowCreateForm(true)}>
              {t("common.create")}
            </button>
          )
        }
      />

      {showCreateForm && <CreatePbsInstanceForm onDone={() => setShowCreateForm(false)} />}

      <QueryState query={query} isEmpty={(data) => data.length === 0}>
        {(instances) => (
          <DataTable
            columns={columns}
            rows={instances}
            rowKey={(row) => row.id}
            onRowClick={(row) => setSelectedInstance(row)}
          />
        )}
      </QueryState>

      {selected && <PbsInstanceDetailModal instance={selected} onClose={() => setSelectedInstance(null)} />}

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
