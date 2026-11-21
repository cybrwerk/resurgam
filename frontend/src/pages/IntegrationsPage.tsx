import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { integrationsApi } from "../api/resources";
import { InlineAlert } from "../components/InlineAlert";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { QueryState } from "../components/QueryState";
import { StatusBadge } from "../components/StatusBadge";
import styles from "./IntegrationsPage.module.css";

/** Administration -> Integrations, manifest §6.3 -- OIDC is the only
 * integration type the manifest defines here (PBS/PVE both have their
 * own dedicated pages under Infrastructure already). A DB-backed
 * `OidcSettings` row, when enabled, is authoritative over `.env` --
 * `source` shows which one is actually in effect right now. First
 * admin-settings-style page in this codebase; closest existing template
 * is `CustomersPage`'s per-feature settings sections (edit form + save +
 * masked-secret display).
 */
export function IntegrationsPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [issuer, setIssuer] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [redirectUri, setRedirectUri] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<{ reachable: boolean; error: string | null } | null>(null);

  const settingsQuery = useQuery({ queryKey: ["integrations-oidc"], queryFn: integrationsApi.getOidc });

  // Seeded only when entering edit mode (see the Edit button below), not
  // via an effect reacting to settingsQuery.data -- a background refetch
  // while the form is open (e.g. react-query's default refetchOnReconnect)
  // must never silently overwrite in-progress edits, including blanking
  // the just-typed client secret.
  const startEditing = (data: typeof settingsQuery.data) => {
    if (!data) return;
    setIssuer(data.issuer);
    setClientId(data.client_id);
    setClientSecret("");
    setRedirectUri(data.redirect_uri);
    setEnabled(data.enabled);
    setEditing(true);
  };

  const saveMutation = useMutation({
    mutationFn: () =>
      integrationsApi.updateOidc({
        issuer, client_id: clientId, client_secret: clientSecret || undefined, redirect_uri: redirectUri, enabled,
      }),
    onSuccess: () => {
      setError(null);
      setEditing(false);
      setClientSecret("");
      queryClient.invalidateQueries({ queryKey: ["integrations-oidc"] });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const testMutation = useMutation({
    mutationFn: () => integrationsApi.testOidc(issuer),
    onSuccess: (result) => setTestResult(result),
  });

  return (
    <div>
      <PageHeader title={t("integrations.title")} description={t("integrations.description")} />

      <QueryState query={settingsQuery}>
        {(data) => (
          <Panel
            title={t("integrations.oidcTitle")}
            actions={
              <>
                <StatusBadge
                  tone={data.source === "database" ? "ok" : "info"}
                  label={data.source === "database" ? t("integrations.sourceDatabase") : t("integrations.sourceEnvironment")}
                />
                {!editing && (
                  <button type="button" className="button buttonPrimary" onClick={() => startEditing(data)}>
                    {t("common.edit")}
                  </button>
                )}
              </>
            }
          >
            {!editing ? (
              <dl className="detailGrid">
                <dt>{t("integrations.issuer")}</dt>
                <dd>{data.issuer}</dd>
                <dt>{t("integrations.clientId")}</dt>
                <dd>{data.client_id}</dd>
                <dt>{t("integrations.clientSecret")}</dt>
                <dd>{data.client_secret_configured ? "••••••••" : "—"}</dd>
                <dt>{t("integrations.redirectUri")}</dt>
                <dd>{data.redirect_uri}</dd>
                <dt>{t("common.status")}</dt>
                <dd>{data.enabled ? t("common.statusActive") : t("common.statusDisabled")}</dd>
              </dl>
            ) : (
              <>
                {error && (
                  <div className={styles.alert}>
                    <InlineAlert>{error}</InlineAlert>
                  </div>
                )}
                <div className="formGrid">
                  <div className="field">
                    <label className="label" htmlFor="oidc-issuer">
                      {t("integrations.issuer")}
                    </label>
                    <input id="oidc-issuer" className="input" value={issuer} onChange={(e) => setIssuer(e.target.value)} />
                  </div>
                  <div className="field">
                    <span className="label" aria-hidden="true" />
                    <div className={styles.testRow}>
                      <button type="button" className="button" onClick={() => testMutation.mutate()} disabled={testMutation.isPending}>
                        {t("integrations.testConnection")}
                      </button>
                      {testResult && (
                        <StatusBadge
                          tone={testResult.reachable ? "ok" : "danger"}
                          label={testResult.reachable ? t("integrations.testReachable") : (testResult.error ?? t("integrations.testUnreachable"))}
                        />
                      )}
                    </div>
                  </div>
                  <div className="field">
                    <label className="label" htmlFor="oidc-client-id">
                      {t("integrations.clientId")}
                    </label>
                    <input id="oidc-client-id" className="input" value={clientId} onChange={(e) => setClientId(e.target.value)} />
                  </div>
                  <div className="field">
                    <label className="label" htmlFor="oidc-client-secret">
                      {t("integrations.clientSecret")}
                    </label>
                    <input
                      id="oidc-client-secret"
                      type="password"
                      autoComplete="off"
                      className="input"
                      placeholder={t("integrations.clientSecretPlaceholder")}
                      value={clientSecret}
                      onChange={(e) => setClientSecret(e.target.value)}
                    />
                  </div>
                  <div className="field">
                    <label className="label" htmlFor="oidc-redirect-uri">
                      {t("integrations.redirectUri")}
                    </label>
                    <input
                      id="oidc-redirect-uri"
                      className="input"
                      value={redirectUri}
                      onChange={(e) => setRedirectUri(e.target.value)}
                    />
                  </div>
                  <div className="field">
                    <span className="label">{t("common.status")}</span>
                    <label className="checkboxField">
                      <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
                      {t("integrations.enabledToggle")}
                    </label>
                  </div>
                </div>

                <div className="formActions">
                  <button type="button" className="button" onClick={() => setEditing(false)} disabled={saveMutation.isPending}>
                    {t("common.cancel")}
                  </button>
                  <button
                    type="button"
                    className="button buttonPrimary"
                    onClick={() => saveMutation.mutate()}
                    disabled={saveMutation.isPending}
                  >
                    {t("common.save")}
                  </button>
                </div>
              </>
            )}
          </Panel>
        )}
      </QueryState>
    </div>
  );
}
