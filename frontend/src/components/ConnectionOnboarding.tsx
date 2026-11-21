import { useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import type {
  ConnectionHealthStatus,
  RequiredPrivilegesOut,
  TestConnectionResponse,
  TokenInputFields,
} from "../api/types";
import { healthStatusPresentation } from "../lib/statusPresentation";
import { StatusBadge } from "./StatusBadge";
import styles from "./ConnectionOnboarding.module.css";

/** Shared PBS/PVE onboarding pieces: token-vs-credentials input, the
 * "Required permissions" panel, the classified connection test result and
 * the "last heartbeat" line. `product` selects the vendor wording. */
export type InfraProduct = "pbs" | "pve";

export type TokenMode = "token" | "credentials";

export interface TokenFormState {
  mode: TokenMode;
  tokenId: string;
  tokenSecret: string;
  username: string;
  password: string;
  tokenName: string;
}

export function emptyTokenState(): TokenFormState {
  return { mode: "token", tokenId: "", tokenSecret: "", username: "", password: "", tokenName: "resurgam" };
}

/** Request fields for the chosen mode. When editing, blank token fields
 * are left out ("leave blank to keep"). */
export function tokenPayload(state: TokenFormState, editing: boolean): TokenInputFields {
  if (state.mode === "credentials") {
    if (editing && !state.username && !state.password) return {};
    return {
      credentials: {
        username: state.username.trim(),
        password: state.password,
        token_name: state.tokenName.trim() || "resurgam",
      },
    };
  }
  const payload: TokenInputFields = {};
  if (state.tokenId.trim()) payload.token_id = state.tokenId.trim();
  if (state.tokenSecret.trim()) payload.token_secret = state.tokenSecret.trim();
  return payload;
}

export function tokenChanged(state: TokenFormState, editing: boolean): boolean {
  return Object.keys(tokenPayload(state, editing)).length > 0;
}

/** Port inputs keep the raw string while editing (clearing must not snap
 * to 0); this validates it on submit. */
export function parsePort(value: string): number | null {
  const trimmed = value.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const port = Number(trimmed);
  return port >= 1 && port <= 65535 ? port : null;
}

/** A field hint that works without hover: the short hint is always visible
 * (and linked via aria-describedby by the caller), the info button toggles
 * the longer explanation. */
export function FieldHint({ id, hint, details }: { id: string; hint: string; details: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const detailsId = `${id}-details`;
  return (
    <div className={styles.hint}>
      <p id={id} className="mutedText">
        {hint}{" "}
        <button
          type="button"
          className={styles.infoButton}
          aria-expanded={open}
          aria-controls={detailsId}
          title={details}
          onClick={() => setOpen((v) => !v)}
        >
          <span aria-hidden="true">i</span>
          <span className={styles.visuallyHidden}>{t("connection.moreInfo")}</span>
        </button>
      </p>
      {open && (
        <p id={detailsId} className={styles.hintDetails}>
          {details}
        </p>
      )}
    </div>
  );
}

export function TokenFields({
  product,
  idPrefix,
  state,
  onChange,
  editing = false,
  storedTokenId,
}: {
  product: InfraProduct;
  idPrefix: string;
  state: TokenFormState;
  onChange: (next: TokenFormState) => void;
  editing?: boolean;
  storedTokenId?: string | null;
}) {
  const { t } = useTranslation();
  const set = (patch: Partial<TokenFormState>) => onChange({ ...state, ...patch });
  const p = product;
  const keepPlaceholder = editing ? t("common.leaveBlankToKeep") : "";

  return (
    <>
      <fieldset className={`field ${styles.fullRow} ${styles.modeGroup}`}>
        <legend className="label">{t("connection.tokenMode")}</legend>
        <label className={styles.modeOption}>
          <input
            type="radio"
            name={`${idPrefix}-token-mode`}
            checked={state.mode === "token"}
            onChange={() => set({ mode: "token" })}
          />
          {t("connection.modeToken")}
        </label>
        <label className={styles.modeOption}>
          <input
            type="radio"
            name={`${idPrefix}-token-mode`}
            checked={state.mode === "credentials"}
            onChange={() => set({ mode: "credentials" })}
          />
          {t("connection.modeCredentials")}
        </label>
      </fieldset>

      {editing && storedTokenId && (
        <p className={`mutedText ${styles.fullRow}`}>
          {t("connection.storedTokenId")}: <code>{storedTokenId}</code>
        </p>
      )}

      {state.mode === "token" ? (
        <>
          <div className="field">
            <label className="label" htmlFor={`${idPrefix}-token-id`}>
              {t("connection.tokenId")}
            </label>
            <input
              id={`${idPrefix}-token-id`}
              className="input"
              value={state.tokenId}
              onChange={(e) => set({ tokenId: e.target.value })}
              placeholder={editing ? keepPlaceholder : t(`connection.${p}.tokenIdExample`)}
              aria-describedby={`${idPrefix}-token-id-hint`}
              autoComplete="off"
              spellCheck={false}
              required={!editing}
            />
            <FieldHint
              id={`${idPrefix}-token-id-hint`}
              hint={t("connection.tokenIdHint", { example: t(`connection.${p}.tokenIdExample`) })}
              details={t(`connection.${p}.tokenIdDetails`)}
            />
          </div>
          <div className="field">
            <label className="label" htmlFor={`${idPrefix}-token-secret`}>
              {t("connection.tokenSecret")}
            </label>
            <input
              id={`${idPrefix}-token-secret`}
              type="password"
              className="input"
              value={state.tokenSecret}
              onChange={(e) => set({ tokenSecret: e.target.value })}
              placeholder={keepPlaceholder}
              aria-describedby={`${idPrefix}-token-secret-hint`}
              autoComplete="new-password"
              required={!editing}
            />
            <FieldHint
              id={`${idPrefix}-token-secret-hint`}
              hint={t("connection.tokenSecretHint")}
              details={t(`connection.${p}.tokenSecretDetails`)}
            />
          </div>
        </>
      ) : (
        <>
          <p className={`mutedText ${styles.fullRow}`}>{t(`connection.${p}.credentialsIntro`)}</p>
          <div className="field">
            <label className="label" htmlFor={`${idPrefix}-username`}>
              {t("connection.username")}
            </label>
            <input
              id={`${idPrefix}-username`}
              className="input"
              value={state.username}
              onChange={(e) => set({ username: e.target.value })}
              placeholder="root@pam"
              aria-describedby={`${idPrefix}-username-hint`}
              autoComplete="off"
              required={!editing}
            />
            <p id={`${idPrefix}-username-hint`} className="mutedText">
              {t("connection.usernameHint")}
            </p>
          </div>
          <div className="field">
            <label className="label" htmlFor={`${idPrefix}-password`}>
              {t("connection.password")}
            </label>
            <input
              id={`${idPrefix}-password`}
              type="password"
              className="input"
              value={state.password}
              onChange={(e) => set({ password: e.target.value })}
              aria-describedby={`${idPrefix}-password-hint`}
              autoComplete="new-password"
              required={!editing || Boolean(state.username)}
            />
            <p id={`${idPrefix}-password-hint`} className="mutedText">
              {t("connection.passwordHint")}
            </p>
          </div>
          <div className="field">
            <label className="label" htmlFor={`${idPrefix}-token-name`}>
              {t("connection.tokenName")}
            </label>
            <input
              id={`${idPrefix}-token-name`}
              className="input"
              value={state.tokenName}
              onChange={(e) => set({ tokenName: e.target.value })}
              aria-describedby={`${idPrefix}-token-name-hint`}
              autoComplete="off"
            />
            <p id={`${idPrefix}-token-name-hint`} className="mutedText">
              {t("connection.tokenNameHint")}
            </p>
          </div>
        </>
      )}
    </>
  );
}

/** Expandable list of every privilege RESURGAM's token needs, with the
 * reason and the manual setup commands -- visible before connecting. */
export function RequiredPermissionsPanel({
  product,
  fetcher,
  defaultOpen = false,
}: {
  product: InfraProduct;
  fetcher: () => Promise<RequiredPrivilegesOut>;
  defaultOpen?: boolean;
}) {
  const { t } = useTranslation();
  const query = useQuery({ queryKey: ["required-privileges", product], queryFn: fetcher, staleTime: Infinity });
  return (
    <details className={styles.permissions} open={defaultOpen}>
      <summary className={styles.permissionsSummary}>{t("connection.requiredPermissions")}</summary>
      <div className={styles.permissionsBody}>
        <p className="mutedText">{t(`connection.${product}.permissionsIntro`)}</p>
        {query.isError && <p className="mutedText">{t("common.loadError")}</p>}
        {query.data && (
          <>
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">{t("connection.privilege")}</th>
                  <th scope="col">{t("connection.privilegePath")}</th>
                  <th scope="col">{t("connection.privilegeReason")}</th>
                </tr>
              </thead>
              <tbody>
                {query.data.privileges.map((privilege) => (
                  <tr key={privilege.name}>
                    <td>
                      <code>{privilege.name}</code>
                    </td>
                    <td>
                      <code>{privilege.path}</code>
                    </td>
                    {/* Translated per privilege; the backend's English reason is
                        the fallback for a privilege added there later. */}
                    <td>
                      {t(`connection.privilegeReasons.${product}.${privilege.name.replace(/\./g, "_")}`, {
                        defaultValue: privilege.reason,
                      })}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mutedText">
              {t("connection.rolesGranted", { roles: query.data.roles.join(", ") })}
            </p>
            <p className="label">{t("connection.manualCommands")}</p>
            <pre className={styles.commands}>{query.data.manual_commands.join("\n")}</pre>
          </>
        )}
      </div>
    </details>
  );
}

export function formatHeartbeat(iso: string | null | undefined, locale: string): string | null {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  const sameDay = new Date().toDateString() === date.toDateString();
  return sameDay
    ? date.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleString(locale, { dateStyle: "short", timeStyle: "short" });
}

/** Status pill + "Last heartbeat HH:MM" for list tables and detail views. */
export function HealthCell({
  status,
  checkedAt,
  secretSet = true,
}: {
  status: ConnectionHealthStatus;
  checkedAt: string | null;
  /** False for a connection imported from a configuration backup: the token
   * ID is known, the secret still has to be entered (Edit). */
  secretSet?: boolean;
}) {
  const { t, i18n } = useTranslation();
  const { tone, label } = healthStatusPresentation(status, t);
  const heartbeat = formatHeartbeat(checkedAt, i18n.language);
  return (
    <span className={styles.healthCell}>
      {secretSet ? (
        <StatusBadge tone={tone} label={label} />
      ) : (
        <span title={t("connection.secretMissingHint")}>
          <StatusBadge tone="warning" label={t("connection.secretMissing")} />
        </span>
      )}
      <span className="mutedText">
        {heartbeat ? t("connection.lastHeartbeat", { time: heartbeat }) : t("connection.neverChecked")}
      </span>
    </span>
  );
}

/** Result of the (automatic or manual) connection test. */
export function ConnectionTestResult({
  result,
  pending,
  error,
}: {
  result: TestConnectionResponse | null;
  pending?: boolean;
  error?: string | null;
}) {
  const { t } = useTranslation();
  const headingId = useId();
  if (pending) {
    return (
      <div className={styles.testResult} role="status">
        <StatusBadge tone="info" label={t("connection.testing")} />
      </div>
    );
  }
  if (error) {
    return (
      <div className={styles.testResult} role="status">
        <StatusBadge tone="danger" label={t("connection.testFailed")} />
        <p>{error}</p>
      </div>
    );
  }
  if (!result) return null;
  const { tone, label } = healthStatusPresentation(result.status, t);
  return (
    <div className={styles.testResult} role="status" aria-labelledby={headingId}>
      <p id={headingId} className={styles.testHeading}>
        <span>{t("connection.testResult")}</span>
        <StatusBadge tone={tone} label={label} />
        {result.version && <span className="mutedText">{t("connection.version", { version: result.version })}</span>}
      </p>
      {result.status === "ok" && !result.error && <p>{t("connection.testOk")}</p>}
      {result.error && <p>{result.error}</p>}
      {result.missing_privileges.length > 0 && (
        <>
          <p className="label">{t("connection.missingPrivileges")}</p>
          <ul className={styles.missingList}>
            {result.missing_privileges.map((name) => (
              <li key={name}>
                <code>{name}</code>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
