import { type FormEvent, useEffect, useId, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { apiTokensApi } from "../api/resources";
import type { ApiTokenCreatedOut, ApiTokenGrant, ApiTokenOut } from "../api/types";
import { ConfirmActionDialog } from "./ConfirmActionDialog";
import { CopyButton } from "./CopyButton";
import { DataTable, type DataTableColumn } from "./DataTable";
import { InlineAlert } from "./InlineAlert";
import { Modal } from "./Modal";
import { Panel } from "./Panel";
import { QueryState } from "./QueryState";
import { StatusBadge } from "./StatusBadge";
import styles from "./ApiTokens.module.css";

/** Personal API tokens (docs/adr/0029): the own-token panel on the
 * Profile page and the all-users panel for platform admins. */

const MY_TOKENS_KEY = ["api-tokens", "mine"];
const ALL_TOKENS_KEY = ["api-tokens", "all"];
const EXPIRY_PRESETS = [30, 90, 180, 365];

type TFn = (key: string, options?: Record<string, unknown>) => string;

function grantKey(g: { role: string; scope_type: string; scope_id: string | null }): string {
  return `${g.role}|${g.scope_type}|${g.scope_id ?? ""}`;
}

export function grantLabel(grant: ApiTokenGrant, t: TFn): string {
  const scope =
    grant.scope_type === "global" ? t("apiTokens.scopeGlobal") : (grant.scope_name ?? grant.scope_id ?? grant.scope_type);
  return `${grant.role} — ${scope}`;
}

function formatDate(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

function ApiTokenTable({
  tokens,
  showOwner,
  onRevoke,
}: {
  tokens: ApiTokenOut[];
  showOwner: boolean;
  onRevoke: (token: ApiTokenOut) => void;
}) {
  const { t } = useTranslation();
  const columns: DataTableColumn<ApiTokenOut>[] = [
    {
      key: "name",
      header: t("common.name"),
      render: (row) => (
        <span className={styles.stack}>
          <strong>{row.name}</strong>
          <code className="mutedText">{row.token_prefix}…</code>
        </span>
      ),
    },
    ...(showOwner
      ? [
          {
            key: "owner",
            header: t("apiTokens.owner"),
            render: (row: ApiTokenOut) => (
              <span className={styles.stack}>
                <span>{row.user_display_name}</span>
                <span className="mutedText">{row.user_email}</span>
              </span>
            ),
          },
        ]
      : []),
    {
      key: "permissions",
      header: t("apiTokens.permissions"),
      render: (row) => (
        <span className={styles.stack}>
          <StatusBadge
            tone={row.read_only ? "info" : "warning"}
            label={row.read_only ? t("apiTokens.readOnly") : t("apiTokens.readWrite")}
          />
          {row.grants.map((g) => (
            <span key={grantKey(g)}>{grantLabel(g, t)}</span>
          ))}
          {row.allowed_cidrs && (
            <span className="mutedText">{t("apiTokens.onlyFrom", { cidrs: row.allowed_cidrs.join(", ") })}</span>
          )}
        </span>
      ),
    },
    {
      key: "expires_at",
      header: t("apiTokens.expires"),
      render: (row) =>
        row.status === "expired" ? (
          <StatusBadge tone="unknown" label={t("apiTokens.expired")} />
        ) : (
          formatDate(row.expires_at)
        ),
    },
    {
      key: "last_used",
      header: t("apiTokens.lastUsed"),
      render: (row) =>
        row.last_used_at ? (
          <span className={styles.stack}>
            <span>{formatDate(row.last_used_at)}</span>
            {row.last_used_ip && <span className="mutedText">{row.last_used_ip}</span>}
          </span>
        ) : (
          <span className="mutedText">{t("apiTokens.neverUsed")}</span>
        ),
    },
    {
      key: "actions",
      header: t("common.actions"),
      render: (row) => (
        <button type="button" className="button buttonDanger" onClick={() => onRevoke(row)}>
          {row.status === "expired" ? t("common.delete") : t("apiTokens.revoke")}
        </button>
      ),
    },
  ];
  return <DataTable columns={columns} rows={tokens} rowKey={(row) => row.id} />;
}

function RevokeDialog({
  token,
  onConfirm,
  onCancel,
}: {
  token: ApiTokenOut | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  return (
    <ConfirmActionDialog
      open={token !== null}
      title={t("apiTokens.revokeTitle", { name: token?.name ?? "" })}
      body={t("apiTokens.revokeBody")}
      confirmLabel={t("apiTokens.revoke")}
      danger
      onConfirm={onConfirm}
      onCancel={onCancel}
    />
  );
}

interface GrantRow {
  role: string;
  scopeKey: string; // grantKey of the chosen option
}

function CreateApiTokenModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (token: ApiTokenCreatedOut) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const ids = { name: useId(), expiry: useId(), customDays: useId(), cidrs: useId() };
  const grantableQuery = useQuery({ queryKey: ["api-tokens", "grantable"], queryFn: apiTokensApi.grantable, enabled: open });
  const options = grantableQuery.data?.options ?? [];
  const maxDays = grantableQuery.data?.max_lifetime_days ?? 548;
  const roles = useMemo(() => [...new Set(options.map((o) => o.role))], [options]);

  const [name, setName] = useState("");
  const [expiry, setExpiry] = useState("90");
  const [customDays, setCustomDays] = useState("90");
  const [readOnly, setReadOnly] = useState(true);
  const [grants, setGrants] = useState<GrantRow[]>([]);
  const [cidrs, setCidrs] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setName("");
      setExpiry("90");
      setCustomDays("90");
      setReadOnly(true);
      setGrants([]);
      setCidrs("");
      setError(null);
    }
  }, [open]);

  // Start with one row on the first role the user may grant.
  useEffect(() => {
    if (open && grants.length === 0 && options.length > 0) {
      const first = options[0];
      setGrants([{ role: first.role, scopeKey: grantKey(first) }]);
    }
  }, [open, options, grants.length]);

  const scopesFor = (role: string) => options.filter((o) => o.role === role);

  const days = expiry === "custom" ? Number(customDays) : Number(expiry);
  const daysValid = Number.isInteger(days) && days >= 1 && days <= maxDays;
  const chosen = grants
    .map((row) => options.find((o) => grantKey(o) === row.scopeKey))
    .filter((o): o is ApiTokenGrant => o !== undefined);
  const canSubmit = name.trim() !== "" && daysValid && chosen.length > 0;
  const cidrList = cidrs
    .split(/[\s,]+/)
    .map((c) => c.trim())
    .filter(Boolean);

  const mutation = useMutation({
    mutationFn: () =>
      apiTokensApi.create({
        name: name.trim(),
        expires_in_days: days,
        read_only: readOnly,
        grants: chosen.map((g) => ({ role: g.role, scope_type: g.scope_type, scope_id: g.scope_id })),
        allowed_cidrs: cidrList.length > 0 ? cidrList : null,
      }),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: ["api-tokens"] });
      onCreated(created);
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (canSubmit) mutation.mutate();
  };

  return (
    <Modal open={open} title={t("apiTokens.createTitle")} onClose={onClose} size="large">
      <form onSubmit={submit} className={styles.form}>
        <div className="field">
          <label className="label" htmlFor={ids.name}>
            {t("common.name")}
          </label>
          <input
            id={ids.name}
            className="input"
            value={name}
            maxLength={100}
            placeholder={t("apiTokens.namePlaceholder")}
            onChange={(e) => setName(e.target.value)}
          />
        </div>

        <div className={styles.row}>
          <div className="field">
            <label className="label" htmlFor={ids.expiry}>
              {t("apiTokens.expiresIn")}
            </label>
            <select id={ids.expiry} className="select" value={expiry} onChange={(e) => setExpiry(e.target.value)}>
              {EXPIRY_PRESETS.filter((d) => d <= maxDays).map((d) => (
                <option key={d} value={String(d)}>
                  {t("apiTokens.days", { count: d })}
                </option>
              ))}
              <option value={String(maxDays)}>{t("apiTokens.maxLifetime", { count: maxDays })}</option>
              <option value="custom">{t("apiTokens.customDays")}</option>
            </select>
          </div>
          {expiry === "custom" && (
            <div className="field">
              <label className="label" htmlFor={ids.customDays}>
                {t("apiTokens.daysField", { max: maxDays })}
              </label>
              <input
                id={ids.customDays}
                className="input"
                type="number"
                min={1}
                max={maxDays}
                value={customDays}
                aria-invalid={!daysValid}
                onChange={(e) => setCustomDays(e.target.value)}
              />
            </div>
          )}
        </div>

        <fieldset className={styles.fieldset}>
          <legend className="label">{t("apiTokens.access")}</legend>
          <label className={styles.radio}>
            <input type="radio" name="access" checked={readOnly} onChange={() => setReadOnly(true)} />
            <span>
              <strong>{t("apiTokens.readOnly")}</strong> — {t("apiTokens.readOnlyHint")}
            </span>
          </label>
          <label className={styles.radio}>
            <input type="radio" name="access" checked={!readOnly} onChange={() => setReadOnly(false)} />
            <span>
              <strong>{t("apiTokens.readWrite")}</strong> — {t("apiTokens.readWriteHint")}
            </span>
          </label>
        </fieldset>

        <fieldset className={styles.fieldset}>
          <legend className="label">{t("apiTokens.roles")}</legend>
          <p className="mutedText">
            {grantableQuery.data?.can_grant_any ? t("apiTokens.rolesHintAdmin") : t("apiTokens.rolesHint")}
          </p>
          {grantableQuery.isSuccess && options.length === 0 && (
            <InlineAlert tone="info">{t("apiTokens.noRoles")}</InlineAlert>
          )}
          {grants.map((row, index) => (
            <div key={index} className={styles.grantRow}>
              <select
                className="select"
                aria-label={t("apiTokens.roleN", { n: index + 1 })}
                value={row.role}
                onChange={(e) => {
                  const role = e.target.value;
                  const first = scopesFor(role)[0];
                  setGrants(grants.map((g, i) => (i === index ? { role, scopeKey: first ? grantKey(first) : "" } : g)));
                }}
              >
                {roles.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
              <select
                className="select"
                aria-label={t("apiTokens.scopeN", { n: index + 1 })}
                value={row.scopeKey}
                onChange={(e) => setGrants(grants.map((g, i) => (i === index ? { ...g, scopeKey: e.target.value } : g)))}
              >
                {scopesFor(row.role).map((o) => (
                  <option key={grantKey(o)} value={grantKey(o)}>
                    {o.scope_type === "global" ? t("apiTokens.scopeGlobal") : (o.scope_name ?? o.scope_id)}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="button"
                onClick={() => setGrants(grants.filter((_, i) => i !== index))}
                disabled={grants.length === 1}
              >
                {t("apiTokens.removeRole")}
              </button>
            </div>
          ))}
          {options.length > 0 && grants.length < 20 && (
            <button
              type="button"
              className="button"
              onClick={() => setGrants([...grants, { role: options[0].role, scopeKey: grantKey(options[0]) }])}
            >
              {t("apiTokens.addRole")}
            </button>
          )}
        </fieldset>

        <div className="field">
          <label className="label" htmlFor={ids.cidrs}>
            {t("apiTokens.allowedIps")}
          </label>
          <textarea
            id={ids.cidrs}
            className="input"
            rows={2}
            placeholder="192.0.2.10, 10.20.0.0/24"
            value={cidrs}
            onChange={(e) => setCidrs(e.target.value)}
          />
          <span className="mutedText">{t("apiTokens.allowedIpsHint")}</span>
        </div>

        {error && <InlineAlert tone="danger">{error}</InlineAlert>}

        <div className={styles.actions}>
          <button type="button" className="button" onClick={onClose}>
            {t("common.cancel")}
          </button>
          <button type="submit" className="button buttonPrimary" disabled={!canSubmit || mutation.isPending}>
            {t("apiTokens.create")}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function CreatedTokenModal({ token, onClose }: { token: ApiTokenCreatedOut | null; onClose: () => void }) {
  const { t } = useTranslation();
  const origin = typeof window !== "undefined" ? window.location.origin : "https://resurgam.example";
  return (
    <Modal open={token !== null} title={t("apiTokens.createdTitle", { name: token?.name ?? "" })} onClose={onClose}>
      {token && (
        <div className={styles.form}>
          <InlineAlert tone="danger">{t("apiTokens.createdWarning")}</InlineAlert>
          <code className={styles.secret} data-testid="api-token-secret">
            {token.token}
          </code>
          <div>
            <CopyButton text={token.token} label={t("apiTokens.copy")} />
          </div>
          <p className="mutedText">{t("apiTokens.usage")}</p>
          <code className={styles.example}>
            {`curl -H "Authorization: Bearer ${token.token_prefix}…" ${origin}/api/v1/auth/whoami`}
          </code>
          <div className={styles.actions}>
            <button type="button" className="button buttonPrimary" onClick={onClose}>
              {t("apiTokens.done")}
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}

/** Profile page: the signed-in user's own tokens. */
export function MyApiTokensPanel({ isBreakglass }: { isBreakglass: boolean }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: MY_TOKENS_KEY, queryFn: apiTokensApi.mine, enabled: !isBreakglass });
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<ApiTokenCreatedOut | null>(null);
  const [pendingRevoke, setPendingRevoke] = useState<ApiTokenOut | null>(null);
  const revoke = useMutation({
    mutationFn: (id: string) => apiTokensApi.revoke(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["api-tokens"] }),
  });

  return (
    <Panel
      title={t("apiTokens.title")}
      padded={false}
      actions={
        !isBreakglass && (
          <button type="button" className="button buttonPrimary" onClick={() => setCreating(true)}>
            {t("apiTokens.create")}
          </button>
        )
      }
    >
      <div className={styles.intro}>
        <p className="mutedText">{t("apiTokens.intro")}</p>
        {isBreakglass && <InlineAlert tone="info">{t("apiTokens.breakglass")}</InlineAlert>}
      </div>
      {!isBreakglass && (
        <QueryState query={query}>
          {(tokens) =>
            tokens.length === 0 ? (
              <p className={`mutedText ${styles.intro}`}>{t("apiTokens.empty")}</p>
            ) : (
              <ApiTokenTable tokens={tokens} showOwner={false} onRevoke={setPendingRevoke} />
            )
          }
        </QueryState>
      )}
      <CreateApiTokenModal
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={(token) => {
          setCreating(false);
          setCreated(token);
        }}
      />
      <CreatedTokenModal token={created} onClose={() => setCreated(null)} />
      <RevokeDialog
        token={pendingRevoke}
        onConfirm={() => {
          if (pendingRevoke) revoke.mutate(pendingRevoke.id);
          setPendingRevoke(null);
        }}
        onCancel={() => setPendingRevoke(null)}
      />
    </Panel>
  );
}

/** Users & Roles: every user's tokens, for platform admins. */
export function AllApiTokensPanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ALL_TOKENS_KEY, queryFn: apiTokensApi.all });
  const [pendingRevoke, setPendingRevoke] = useState<ApiTokenOut | null>(null);
  const revoke = useMutation({
    mutationFn: (id: string) => apiTokensApi.revoke(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["api-tokens"] }),
  });
  return (
    <Panel title={t("apiTokens.allTitle")} padded={false}>
      <div className={styles.intro}>
        <p className="mutedText">{t("apiTokens.allIntro")}</p>
      </div>
      <QueryState query={query}>
        {(tokens) =>
          tokens.length === 0 ? (
            <p className={`mutedText ${styles.intro}`}>{t("apiTokens.allEmpty")}</p>
          ) : (
            <ApiTokenTable tokens={tokens} showOwner onRevoke={setPendingRevoke} />
          )
        }
      </QueryState>
      <RevokeDialog
        token={pendingRevoke}
        onConfirm={() => {
          if (pendingRevoke) revoke.mutate(pendingRevoke.id);
          setPendingRevoke(null);
        }}
        onCancel={() => setPendingRevoke(null)}
      />
    </Panel>
  );
}
