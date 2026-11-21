import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { customersApi, usersApi } from "../api/resources";
import type { CustomerOut, UserOut, UserRoleAssignmentOut } from "../api/types";
import { AllApiTokensPanel } from "../components/ApiTokens";
import { ConfirmActionDialog } from "../components/ConfirmActionDialog";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { InlineAlert } from "../components/InlineAlert";
import { Modal } from "../components/Modal";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { QueryState } from "../components/QueryState";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import styles from "./UsersPage.module.css";

const QUERY_KEY = ["users"];
const ROLE_KEYS = ["viewer", "backup_operator", "restore_operator", "customer_admin", "auditor", "platform_admin"];

function CreateUserForm({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [mfaRequired, setMfaRequired] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const createMutation = useMutation({
    mutationFn: () => usersApi.create({ email, display_name: displayName, password, mfa_required: mfaRequired }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEY });
      onDone();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  return (
    <Panel title={t("users.createTitle")} className={styles.createPanel}>
      <form
        onSubmit={(event: FormEvent) => {
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
            <label className="label" htmlFor="user-email">
              {t("users.email")}
            </label>
            <input id="user-email" type="email" className="input" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </div>
          <div className="field">
            <label className="label" htmlFor="user-display-name">
              {t("users.displayName")}
            </label>
            <input
              id="user-display-name"
              className="input"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="user-password">
              {t("users.password")}
            </label>
            <input
              id="user-password"
              type="password"
              className="input"
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <span className="label">{t("users.mfa")}</span>
            <label className="checkboxField">
              <input type="checkbox" checked={mfaRequired} onChange={(e) => setMfaRequired(e.target.checked)} />{" "}
              {t("users.mfaRequire")}
            </label>
          </div>
        </div>
        <div className="formActions">
          <button type="button" className="button" onClick={onDone}>
            {t("common.cancel")}
          </button>
          <button type="submit" className="button buttonPrimary" disabled={createMutation.isPending}>
            {t("common.create")}
          </button>
        </div>
      </form>
    </Panel>
  );
}

/** Shared by the per-user modal and the main list's own "Rollen" column
 * (a real UX-audit finding, 2026-09-23: the list showed no role at all,
 * so answering "who has platform_admin" meant opening every user one by
 * one) -- one formatting rule, not two copies that can drift apart.
 */
function roleLabel(assignment: UserRoleAssignmentOut, customers: CustomerOut[] | undefined, t: (key: string) => string): string {
  const scope =
    assignment.scope_type === "customer"
      ? (customers?.find((c) => c.id === assignment.scope_id)?.name ?? assignment.scope_id ?? t("common.name"))
      : assignment.scope_type;
  return `${assignment.role} — ${scope}`;
}

function RoleAssignmentsModal({
  user,
  customersQuery,
  onClose,
}: {
  user: UserOut;
  customersQuery: ReturnType<typeof useQuery<CustomerOut[]>>;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const assignmentsKey = ["users", user.id, "role-assignments"];

  const assignmentsQuery = useQuery({ queryKey: assignmentsKey, queryFn: () => usersApi.roleAssignments(user.id) });

  const [role, setRole] = useState(ROLE_KEYS[0]);
  const [scopeType, setScopeType] = useState<"global" | "customer">("global");
  const [customerId, setCustomerId] = useState("");
  const [error, setError] = useState<string | null>(null);

  const assignMutation = useMutation({
    mutationFn: () =>
      usersApi.assignRole(user.id, {
        role,
        scope_type: scopeType,
        scope_id: scopeType === "customer" ? customerId : null,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: assignmentsKey });
      setError(null);
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const [pendingUnassign, setPendingUnassign] = useState<UserRoleAssignmentOut | null>(null);
  const unassignMutation = useMutation({
    mutationFn: (assignmentId: string) => usersApi.unassignRole(user.id, assignmentId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: assignmentsKey });
      void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  return (
    <Modal open title={user.display_name} onClose={onClose}>
      <h3 className={styles.modalSubtitle}>{t("users.roleAssignments")}</h3>
      {error && <InlineAlert>{error}</InlineAlert>}
      <QueryState query={assignmentsQuery} isEmpty={(data) => data.length === 0}>
        {(assignments) =>
          assignments.length === 0 ? (
            <p className="mutedText">{t("users.noAssignments")}</p>
          ) : (
            <ul className={styles.assignmentList}>
              {assignments.map((a) => (
                <li key={a.id} className={styles.assignmentRow}>
                  <span>{roleLabel(a, customersQuery.data, t)}</span>
                  <button type="button" className="button buttonDanger" onClick={() => setPendingUnassign(a)}>
                    {t("common.delete")}
                  </button>
                </li>
              ))}
            </ul>
          )
        }
      </QueryState>
      <ConfirmActionDialog
        open={pendingUnassign !== null}
        title={t("users.removeRoleTitle")}
        body={t("users.removeRoleBody", {
          role: pendingUnassign ? roleLabel(pendingUnassign, customersQuery.data, t) : "",
          user: user.display_name || user.email,
        })}
        danger
        confirmLabel={t("users.removeRole")}
        onCancel={() => setPendingUnassign(null)}
        onConfirm={() => {
          if (pendingUnassign) unassignMutation.mutate(pendingUnassign.id);
          setPendingUnassign(null);
        }}
      />

      <form
        className={styles.assignForm}
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          assignMutation.mutate();
        }}
      >
        <div className="field">
          <label className="label" htmlFor="assign-role">
            {t("users.role")}
          </label>
          <select id="assign-role" className="select" value={role} onChange={(e) => setRole(e.target.value)}>
            {ROLE_KEYS.map((key) => (
              <option key={key} value={key}>
                {key}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label className="label" htmlFor="assign-scope-type">
            {t("users.scopeType")}
          </label>
          <select
            id="assign-scope-type"
            className="select"
            value={scopeType}
            onChange={(e) => setScopeType(e.target.value as "global" | "customer")}
          >
            <option value="global">global</option>
            <option value="customer">{t("users.scopeCustomer")}</option>
          </select>
        </div>
        {scopeType === "customer" && (
          <div className="field">
            <label className="label" htmlFor="assign-customer">
              {t("users.scopeCustomer")}
            </label>
            <select id="assign-customer" className="select" value={customerId} onChange={(e) => setCustomerId(e.target.value)} required>
              <option value="">—</option>
              {customersQuery.data?.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </div>
        )}
        <button type="submit" className="button buttonPrimary" disabled={assignMutation.isPending}>
          {t("users.assignRole")}
        </button>
      </form>

      {/* The break-glass credential is configured on the server, not here. */}
      {!user.is_breakglass && <SetPasswordSection user={user} />}
      {!user.is_breakglass && user.has_password && <MfaSection user={user} />}
    </Modal>
  );
}

/** The MFA status shown in the list and the user dialog. */
export function mfaBadge(user: UserOut, t: (key: string) => string): { tone: StatusTone; label: string } | null {
  if (user.is_breakglass || !user.has_password) return null;
  const required = user.mfa_required || user.mfa_required_effective;
  if (required && !user.mfa_enrolled) return { tone: "warning", label: t("users.mfaPending") };
  if (required) return { tone: "ok", label: t("users.mfaRequiredActive") };
  if (user.mfa_enrolled) return { tone: "info", label: t("users.mfaActive") };
  return { tone: "unknown", label: t("users.mfaOff") };
}

/** Require MFA for a local account (enforced at its next login) and reset
 * its factors when the user lost their phone or security key. */
function MfaSection({ user }: { user: UserOut }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [current, setCurrent] = useState(user);
  // Follow the prop when the list refreshes (e.g. the global policy changed).
  const [prevUser, setPrevUser] = useState(user);
  if (user !== prevUser) {
    setPrevUser(user);
    setCurrent(user);
  }
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmReset, setConfirmReset] = useState(false);

  const done = (updated: UserOut, message: string) => {
    setCurrent(updated);
    setError(null);
    setNotice(message);
    void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
  };
  const fail = (err: unknown) => {
    setNotice(null);
    setError(err instanceof ApiError ? err.message : t("common.loadError"));
  };

  const requireMutation = useMutation({
    mutationFn: (required: boolean) => usersApi.setMfaRequired(user.id, required),
    onSuccess: (updated) => done(updated, updated.mfa_required ? t("users.mfaRequiredSaved") : t("users.mfaOptionalSaved")),
    onError: fail,
  });
  const resetMutation = useMutation({
    mutationFn: () => usersApi.resetMfa(user.id),
    onSuccess: (updated) => done(updated, t("users.mfaResetDone")),
    onError: fail,
  });
  const badge = mfaBadge(current, t);
  // Covered by the global policy alone: the own flag would not change anything.
  const byPolicy = Boolean(current.mfa_required_effective) && !current.mfa_required;
  const resetPossible = current.mfa_reset_possible ?? current.mfa_enrolled;

  return (
    <div className={styles.section} data-testid="user-mfa-section">
      <h3 className={styles.modalSubtitle}>
        {t("users.mfa")} {badge && <StatusBadge tone={badge.tone} label={badge.label} />}
      </h3>
      {error && <InlineAlert>{error}</InlineAlert>}
      {notice && <InlineAlert tone="success">{notice}</InlineAlert>}
      <label className="checkboxField">
        <input
          type="checkbox"
          checked={current.mfa_required || byPolicy}
          disabled={byPolicy || requireMutation.isPending}
          aria-describedby={byPolicy ? `mfa-policy-hint-${current.id}` : undefined}
          onChange={(e) => requireMutation.mutate(e.target.checked)}
        />{" "}
        {t("users.mfaRequireImmediate")}
      </label>
      {byPolicy && (
        <p id={`mfa-policy-hint-${current.id}`} className="mutedText">
          {t("users.mfaRequiredByPolicy")}
        </p>
      )}
      <p className="mutedText">{t("users.mfaRequireHint")}</p>
      <button
        type="button"
        className="button buttonDanger"
        disabled={!resetPossible || resetMutation.isPending}
        onClick={() => setConfirmReset(true)}
      >
        {t("users.mfaReset")}
      </button>
      <ConfirmActionDialog
        open={confirmReset}
        title={t("users.mfaResetTitle")}
        body={t(current.mfa_required || byPolicy ? "users.mfaResetBodyRequired" : "users.mfaResetBodyOptional")}
        danger
        confirmLabel={t("users.mfaReset")}
        onCancel={() => setConfirmReset(false)}
        onConfirm={() => {
          setConfirmReset(false);
          resetMutation.mutate();
        }}
      />
    </div>
  );
}

/** Set or reset a user's local password -- needed after a configuration
 * import, which never carries password hashes (ADR 0024). */
function SetPasswordSection({ user }: { user: UserOut }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const mutation = useMutation({
    mutationFn: () => usersApi.setPassword(user.id, password),
    onSuccess: () => {
      setPassword("");
      setConfirm("");
      setSaved(true);
      queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  return (
    <form
      className={styles.passwordSection}
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        setSaved(false);
        if (password.length < 8) return setError(t("users.passwordTooShort"));
        if (password !== confirm) return setError(t("users.passwordMismatch"));
        setError(null);
        mutation.mutate();
      }}
    >
      <h3 className={styles.modalSubtitle}>{t("users.setPasswordTitle")}</h3>
      <p className="mutedText">{user.has_password ? t("users.setPasswordResetHint") : t("users.setPasswordNewHint")}</p>
      {error && <InlineAlert>{error}</InlineAlert>}
      {saved && <InlineAlert tone="success">{t("users.passwordSaved")}</InlineAlert>}
      <div className="field">
        <label className="label" htmlFor="set-password">
          {t("users.newPassword")}
        </label>
        <input
          id="set-password"
          type="password"
          autoComplete="new-password"
          className="input"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </div>
      <div className="field">
        <label className="label" htmlFor="set-password-confirm">
          {t("users.confirmPassword")}
        </label>
        <input
          id="set-password-confirm"
          type="password"
          autoComplete="new-password"
          className="input"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
        />
      </div>
      <button type="submit" className="button" disabled={mutation.isPending || password === ""}>
        {t("users.setPassword")}
      </button>
    </form>
  );
}

/** Users & Roles -> "Require MFA for all local users" (the global form of
 * the per-user flag). */
function MfaPolicyPanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [confirm, setConfirm] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  const policyQuery = useQuery({ queryKey: ["mfa-policy"], queryFn: usersApi.mfaPolicy });
  const mutation = useMutation({
    mutationFn: (required: boolean) => usersApi.setMfaPolicy(required),
    onSuccess: (policy) => {
      setError(null);
      queryClient.setQueryData(["mfa-policy"], policy);
      void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });
  const enabled = policyQuery.data?.require_mfa_all_local_users ?? false;

  // One slim row (status + label + "Change…"), so the user list stays the
  // page's main content; the effect is spelled out in the confirmation.
  return (
    <div className={styles.policyBar} data-testid="mfa-policy">
      <span className={styles.policyLabel}>{t("users.mfaPolicyLabel")}</span>
      <StatusBadge tone={enabled ? "ok" : "unknown"} label={enabled ? t("users.mfaPolicyOn") : t("users.mfaPolicyOff")} />
      <button
        type="button"
        className="button"
        disabled={!policyQuery.data || mutation.isPending}
        onClick={() => setConfirm(!enabled)}
      >
        {t("users.mfaPolicyChange")}
      </button>
      <span className={`mutedText ${styles.policyHint}`}>{t("users.mfaPolicyHint")}</span>
      {error && (
        <div className={styles.policyError}>
          <InlineAlert>{error}</InlineAlert>
        </div>
      )}
      <ConfirmActionDialog
        open={confirm !== null}
        title={confirm ? t("users.mfaPolicyEnableTitle") : t("users.mfaPolicyDisableTitle")}
        body={confirm ? t("users.mfaPolicyEnableBody") : t("users.mfaPolicyDisableBody")}
        confirmLabel={confirm ? t("users.mfaPolicyEnable") : t("users.mfaPolicyDisable")}
        danger={confirm === false}
        onCancel={() => setConfirm(null)}
        onConfirm={() => {
          if (confirm !== null) mutation.mutate(confirm);
          setConfirm(null);
        }}
      />
    </div>
  );
}

export function UsersPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [selectedUserId, setSelectedUserId] = useState<string | null>(null);

  const query = useQuery({ queryKey: QUERY_KEY, queryFn: usersApi.list });
  const customersQuery = useQuery({ queryKey: ["customers"], queryFn: customersApi.list });
  // Resolved from the list each render, so the dialog follows a refresh.
  const selectedUser = query.data?.find((u) => u.id === selectedUserId) ?? null;
  const invalidate = () => queryClient.invalidateQueries({ queryKey: QUERY_KEY });

  const disableMutation = useMutation({ mutationFn: (id: string) => usersApi.disable(id), onSuccess: invalidate });
  const enableMutation = useMutation({ mutationFn: (id: string) => usersApi.enable(id), onSuccess: invalidate });

  const typeLabelFor = (row: UserOut) => {
    if (row.is_breakglass) return t("users.typeBreakglass");
    if (row.has_password) return t("users.typeLocal");
    return t("users.typeSso");
  };

  const columns: DataTableColumn<UserOut>[] = [
    {
      key: "email",
      header: t("users.email"),
      sortValue: (row) => row.email,
      render: (row) => {
        const badge = mfaBadge(row, t);
        return (
          <>
            {row.email}
            {/* Phones: the MFA column is hidden, its badge sits under the email. */}
            {badge && (
              <span className={styles.mobileMfa} aria-hidden="true">
                <StatusBadge tone={badge.tone} label={badge.label} />
              </span>
            )}
          </>
        );
      },
    },
    {
      key: "displayName",
      header: t("users.displayName"),
      render: (row) => row.display_name,
    },
    { key: "type", header: t("users.type"), render: (row) => typeLabelFor(row) },
    {
      key: "status",
      header: t("common.status"),
      render: (row) => (
        <StatusBadge
          tone={row.disabled ? "unknown" : "ok"}
          label={row.disabled ? t("common.statusDisabled") : t("common.statusActive")}
        />
      ),
    },
    {
      key: "mfa",
      header: t("users.mfa"),
      render: (row) => {
        const badge = mfaBadge(row, t);
        return badge ? <StatusBadge tone={badge.tone} label={badge.label} /> : <span className="mutedText">—</span>;
      },
    },
    {
      key: "roles",
      header: t("users.roles"),
      render: (row) =>
        row.roles.length === 0 ? (
          <span className="mutedText">{t("users.noRoles")}</span>
        ) : (
          row.roles.map((a) => roleLabel(a, customersQuery.data, t)).join(", ")
        ),
    },
    {
      key: "actions",
      header: t("common.actions"),
      render: (row) => (
        <div onClick={(e) => e.stopPropagation()}>
          {row.disabled ? (
            <button type="button" className="button" onClick={() => enableMutation.mutate(row.id)}>
              {t("common.enable")}
            </button>
          ) : (
            <button type="button" className="button buttonDanger" onClick={() => disableMutation.mutate(row.id)}>
              {t("common.disable")}
            </button>
          )}
        </div>
      ),
    },
  ];

  return (
    <div>
      <PageHeader
        title={t("users.title")}
        actions={
          !showCreateForm && (
            <button type="button" className="button buttonPrimary" onClick={() => setShowCreateForm(true)}>
              {t("common.create")}
            </button>
          )
        }
      />

      <MfaPolicyPanel />
      {showCreateForm && <CreateUserForm onDone={() => setShowCreateForm(false)} />}

      <QueryState query={query} isEmpty={(data) => data.length === 0}>
        {(users) => (
          <div className={styles.usersTable}>
            <DataTable columns={columns} rows={users} rowKey={(row) => row.id} onRowClick={(row) => setSelectedUserId(row.id)} />
          </div>
        )}
      </QueryState>

      <div className={styles.tokensSection}>
        <AllApiTokensPanel />
      </div>

      {selectedUser && (
        <RoleAssignmentsModal user={selectedUser} customersQuery={customersQuery} onClose={() => setSelectedUserId(null)} />
      )}
    </div>
  );
}
