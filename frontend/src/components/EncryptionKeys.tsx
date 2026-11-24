import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { customersApi, encryptionKeysApi } from "../api/resources";
import type { EncryptionKeyOut, StepUpConfirmation } from "../api/types";
import { useAuth } from "../contexts/AuthContext";
import { ConfirmActionDialog } from "./ConfirmActionDialog";
import { DataTable, type DataTableColumn } from "./DataTable";
import { shortFingerprint } from "./EncryptionBadge";
import { InlineAlert } from "./InlineAlert";
import { Modal } from "./Modal";
import { Panel } from "./Panel";
import { QueryState } from "./QueryState";
import { StatusBadge } from "./StatusBadge";

/** Encryption key store (ADR 0031, stage 2): the Administration page for
 * platform admins, and the per-tenant panel on a tenant's own panel page. */

export const ENCRYPTION_KEYS_KEY = ["encryption-keys"];

function errorText(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

/** Fresh confirmation fields: password, or a code from the authenticator app.
 * SSO-only accounts get told to sign in again instead (the server says so). */
function StepUpFields({
  value,
  onChange,
  idPrefix,
}: {
  value: StepUpConfirmation;
  onChange: (next: StepUpConfirmation) => void;
  idPrefix: string;
}) {
  const { t } = useTranslation();
  return (
    <>
      <p className="mutedText">{t("encryptionKeys.confirmHint")}</p>
      <div className="field">
        <label className="label" htmlFor={`${idPrefix}-password`}>
          {t("encryptionKeys.confirmPassword")}
        </label>
        <input
          id={`${idPrefix}-password`}
          className="input"
          type="password"
          autoComplete="current-password"
          value={value.password ?? ""}
          onChange={(e) => onChange({ ...value, password: e.target.value })}
        />
      </div>
      <div className="field">
        <label className="label" htmlFor={`${idPrefix}-totp`}>
          {t("encryptionKeys.confirmTotp")}
        </label>
        <input
          id={`${idPrefix}-totp`}
          className="input"
          inputMode="numeric"
          autoComplete="one-time-code"
          value={value.totp_code ?? ""}
          onChange={(e) => onChange({ ...value, totp_code: e.target.value })}
        />
      </div>
    </>
  );
}

function stepUpMessage(err: unknown, t: (key: string) => string): string {
  if (err instanceof ApiError && err.code === "confirmation_required") {
    const method = (err.details as { method?: string } | undefined)?.method;
    return method === "recent_login"
      ? t("encryptionKeys.signInAgain")
      : t("encryptionKeys.confirmMissing");
  }
  return errorText(err, t("common.loadError"));
}

function cleanConfirmation(value: StepUpConfirmation): StepUpConfirmation {
  return {
    password: value.password?.trim() ? value.password : null,
    totp_code: value.totp_code?.trim() ? value.totp_code.trim() : null,
  };
}

function DownloadKeyModal({
  keyRow,
  kind,
  onClose,
}: {
  keyRow: EncryptionKeyOut;
  kind: "keyfile" | "sheet";
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [confirmation, setConfirmation] = useState<StepUpConfirmation>({});
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const download = useMutation({
    mutationFn: () =>
      kind === "sheet"
        ? encryptionKeysApi.keySheet(keyRow.id, cleanConfirmation(confirmation))
        : encryptionKeysApi.download(
            keyRow.id,
            cleanConfirmation(confirmation),
          ),
    onSuccess: () => {
      setError(null);
      setDone(true);
    },
    onError: (err) => setError(stepUpMessage(err, t)),
  });
  const confirmCopy = useMutation({
    mutationFn: () => encryptionKeysApi.confirmCopy(keyRow.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ENCRYPTION_KEYS_KEY });
      onClose();
    },
    onError: (err) => setError(errorText(err, t("common.loadError"))),
  });

  return (
    <Modal
      open
      title={t(
        kind === "sheet"
          ? "encryptionKeys.sheetTitle"
          : "encryptionKeys.downloadTitle",
        {
          key: shortFingerprint(keyRow.fingerprint),
        },
      )}
      onClose={onClose}
    >
      {error && (
        <div style={{ marginBottom: "var(--space-3, 0.75rem)" }}>
          <InlineAlert>{error}</InlineAlert>
        </div>
      )}
      <p>{t("encryptionKeys.downloadWarning")}</p>
      {!done ? (
        <>
          <StepUpFields
            value={confirmation}
            onChange={setConfirmation}
            idPrefix="key-download"
          />
          <div className="formActions">
            <button
              type="button"
              className="button"
              onClick={onClose}
              disabled={download.isPending}
            >
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="button buttonPrimary"
              onClick={() => download.mutate()}
              disabled={download.isPending}
            >
              {t(
                kind === "sheet"
                  ? "encryptionKeys.downloadSheet"
                  : "encryptionKeys.downloadKeyfile",
              )}
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="mutedText">{t("encryptionKeys.storeCopyHint")}</p>
          <div className="formActions">
            <button type="button" className="button" onClick={onClose}>
              {t("common.close")}
            </button>
            {!keyRow.copy_confirmed_at && (
              <button
                type="button"
                className="button buttonPrimary"
                onClick={() => confirmCopy.mutate()}
                disabled={confirmCopy.isPending}
              >
                {t("encryptionKeys.confirmCopy")}
              </button>
            )}
          </div>
        </>
      )}
    </Modal>
  );
}

/** Client-side check of a PBS keyfile before upload: it must be the JSON
 * PBS/PVE write, and not password-protected (`kdf` set) -- RESURGAM reads
 * backups unattended and never has a key password, same as PVE itself.
 * Returns an i18n key for the problem, or null when it looks usable. The
 * server checks again (and reads the fingerprint). */
export function keyfileProblem(content: string): string | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(content);
  } catch {
    return "encryptionKeys.invalidJson";
  }
  if (typeof parsed !== "object" || parsed === null || !("data" in parsed)) {
    return "encryptionKeys.invalidJson";
  }
  if ((parsed as { kdf?: unknown }).kdf)
    return "encryptionKeys.passwordProtected";
  return null;
}

function UploadKeyModal({
  customerId,
  onClose,
}: {
  customerId?: string;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [content, setContent] = useState("");
  const [label, setLabel] = useState("");
  const [customer, setCustomer] = useState(customerId ?? "");
  const [error, setError] = useState<string | null>(null);
  const customersQuery = useQuery({
    queryKey: ["customers"],
    queryFn: customersApi.list,
    enabled: !customerId,
  });
  const problem = content.trim() ? keyfileProblem(content.trim()) : null;

  const upload = useMutation({
    mutationFn: () =>
      encryptionKeysApi.upload({
        keyfile_content: content.trim(),
        label,
        customer_id: customer || null,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ENCRYPTION_KEYS_KEY });
      queryClient.invalidateQueries({ queryKey: ["datastores"] });
      onClose();
    },
    onError: (err) => setError(errorText(err, t("common.loadError"))),
  });

  const readFile = (file: File | undefined) => {
    if (!file) return;
    file
      .text()
      .then(setContent)
      .catch(() => setError(t("encryptionKeys.readFailed")));
  };

  return (
    <Modal open title={t("encryptionKeys.uploadTitle")} onClose={onClose}>
      {error && (
        <div style={{ marginBottom: "var(--space-3, 0.75rem)" }}>
          <InlineAlert>{error}</InlineAlert>
        </div>
      )}
      <p className="mutedText">{t("encryptionKeys.uploadIntro")}</p>
      <div className="field">
        <label className="label" htmlFor="key-upload-file">
          {t("encryptionKeys.keyfile")}
        </label>
        <input
          id="key-upload-file"
          className="input"
          type="file"
          accept=".enc,.json,.key,application/json,text/plain"
          onChange={(e) => readFile(e.target.files?.[0])}
        />
      </div>
      <div className="field">
        <label className="label" htmlFor="key-upload-content">
          {t("encryptionKeys.paste")}
        </label>
        <textarea
          id="key-upload-content"
          className="input"
          rows={5}
          spellCheck={false}
          autoComplete="off"
          value={content}
          onChange={(e) => setContent(e.target.value)}
          aria-invalid={problem !== null}
          placeholder='{"kdf": null, "created": "…", "data": "…", "fingerprint": "…"}'
        />
        {problem && (
          <span className="mutedText" role="alert">
            {t(problem)}
          </span>
        )}
      </div>
      <div className="field">
        <label className="label" htmlFor="key-upload-label">
          {t("encryptionKeys.label")}
        </label>
        <input
          id="key-upload-label"
          className="input"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder={t("encryptionKeys.labelPlaceholder")}
        />
      </div>
      {!customerId && (
        <div className="field">
          <label className="label" htmlFor="key-upload-customer">
            {t("encryptionKeys.tenant")}
          </label>
          <select
            id="key-upload-customer"
            className="input"
            value={customer}
            onChange={(e) => setCustomer(e.target.value)}
          >
            <option value="">{t("encryptionKeys.noTenant")}</option>
            {(customersQuery.data ?? []).map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </div>
      )}
      <div className="formActions">
        <button
          type="button"
          className="button"
          onClick={onClose}
          disabled={upload.isPending}
        >
          {t("common.cancel")}
        </button>
        <button
          type="button"
          className="button buttonPrimary"
          onClick={() => upload.mutate()}
          disabled={
            upload.isPending || content.trim() === "" || problem !== null
          }
        >
          {t("encryptionKeys.upload")}
        </button>
      </div>
    </Modal>
  );
}

function ExportKeysModal({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const [passphrase, setPassphrase] = useState("");
  const [repeat, setRepeat] = useState("");
  const [confirmation, setConfirmation] = useState<StepUpConfirmation>({});
  const [error, setError] = useState<string | null>(null);
  const tooShort = passphrase.length > 0 && passphrase.length < 12;
  const mismatch = repeat.length > 0 && repeat !== passphrase;

  const exportAll = useMutation({
    mutationFn: () =>
      encryptionKeysApi.exportAll(passphrase, cleanConfirmation(confirmation)),
    onSuccess: onClose,
    onError: (err) => setError(stepUpMessage(err, t)),
  });

  return (
    <Modal open title={t("encryptionKeys.exportTitle")} onClose={onClose}>
      {error && (
        <div style={{ marginBottom: "var(--space-3, 0.75rem)" }}>
          <InlineAlert>{error}</InlineAlert>
        </div>
      )}
      <p className="mutedText">{t("encryptionKeys.exportIntro")}</p>
      <div className="field">
        <label className="label" htmlFor="key-export-passphrase">
          {t("encryptionKeys.passphrase")}
        </label>
        <input
          id="key-export-passphrase"
          className="input"
          type="password"
          autoComplete="new-password"
          value={passphrase}
          onChange={(e) => setPassphrase(e.target.value)}
          aria-invalid={tooShort}
        />
        {tooShort && (
          <span className="mutedText">
            {t("encryptionKeys.passphraseTooShort")}
          </span>
        )}
      </div>
      <div className="field">
        <label className="label" htmlFor="key-export-repeat">
          {t("encryptionKeys.passphraseRepeat")}
        </label>
        <input
          id="key-export-repeat"
          className="input"
          type="password"
          autoComplete="new-password"
          value={repeat}
          onChange={(e) => setRepeat(e.target.value)}
          aria-invalid={mismatch}
        />
        {mismatch && (
          <span className="mutedText">
            {t("encryptionKeys.passphraseMismatch")}
          </span>
        )}
      </div>
      <StepUpFields
        value={confirmation}
        onChange={setConfirmation}
        idPrefix="key-export"
      />
      <div className="formActions">
        <button
          type="button"
          className="button"
          onClick={onClose}
          disabled={exportAll.isPending}
        >
          {t("common.cancel")}
        </button>
        <button
          type="button"
          className="button buttonPrimary"
          onClick={() => exportAll.mutate()}
          disabled={
            exportAll.isPending ||
            passphrase.length < 12 ||
            repeat !== passphrase
          }
        >
          {t("encryptionKeys.export")}
        </button>
      </div>
    </Modal>
  );
}

function ImportKeysModal({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [content, setContent] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{
    imported: number;
    skipped: number;
  } | null>(null);

  const importAll = useMutation({
    mutationFn: () => encryptionKeysApi.importAll(content, passphrase),
    onSuccess: (data) => {
      setError(null);
      setResult({
        imported: data.imported.length,
        skipped: data.skipped.length,
      });
      queryClient.invalidateQueries({ queryKey: ENCRYPTION_KEYS_KEY });
      queryClient.invalidateQueries({ queryKey: ["datastores"] });
    },
    onError: (err) => setError(errorText(err, t("common.loadError"))),
  });

  return (
    <Modal open title={t("encryptionKeys.importTitle")} onClose={onClose}>
      {error && (
        <div style={{ marginBottom: "var(--space-3, 0.75rem)" }}>
          <InlineAlert>{error}</InlineAlert>
        </div>
      )}
      {result ? (
        <>
          <p>{t("encryptionKeys.importResult", result)}</p>
          <div className="formActions">
            <button
              type="button"
              className="button buttonPrimary"
              onClick={onClose}
            >
              {t("common.close")}
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="mutedText">{t("encryptionKeys.importIntro")}</p>
          <div className="field">
            <label className="label" htmlFor="key-import-file">
              {t("encryptionKeys.exportFile")}
            </label>
            <input
              id="key-import-file"
              className="input"
              type="file"
              accept=".json,application/json"
              onChange={(e) =>
                e.target.files?.[0]
                  ?.text()
                  .then(setContent)
                  .catch(() => setError(t("encryptionKeys.readFailed")))
              }
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="key-import-passphrase">
              {t("encryptionKeys.passphrase")}
            </label>
            <input
              id="key-import-passphrase"
              className="input"
              type="password"
              autoComplete="off"
              value={passphrase}
              onChange={(e) => setPassphrase(e.target.value)}
            />
          </div>
          <div className="formActions">
            <button
              type="button"
              className="button"
              onClick={onClose}
              disabled={importAll.isPending}
            >
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="button buttonPrimary"
              onClick={() => importAll.mutate()}
              disabled={importAll.isPending || !content || !passphrase}
            >
              {t("encryptionKeys.import")}
            </button>
          </div>
        </>
      )}
    </Modal>
  );
}

function EditKeyModal({
  keyRow,
  onClose,
}: {
  keyRow: EncryptionKeyOut;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [label, setLabel] = useState(keyRow.label);
  const [customer, setCustomer] = useState(keyRow.customer_id ?? "");
  const [error, setError] = useState<string | null>(null);
  const customersQuery = useQuery({
    queryKey: ["customers"],
    queryFn: customersApi.list,
  });

  const save = useMutation({
    mutationFn: () =>
      encryptionKeysApi.update(
        keyRow.id,
        customer
          ? { label, customer_id: customer }
          : { label, clear_customer: true },
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ENCRYPTION_KEYS_KEY });
      onClose();
    },
    onError: (err) => setError(errorText(err, t("common.loadError"))),
  });

  return (
    <Modal
      open
      title={t("encryptionKeys.editTitle", {
        key: shortFingerprint(keyRow.fingerprint),
      })}
      onClose={onClose}
    >
      {error && (
        <div style={{ marginBottom: "var(--space-3, 0.75rem)" }}>
          <InlineAlert>{error}</InlineAlert>
        </div>
      )}
      <div className="field">
        <label className="label" htmlFor="key-edit-label">
          {t("encryptionKeys.label")}
        </label>
        <input
          id="key-edit-label"
          className="input"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
        />
      </div>
      <div className="field">
        <label className="label" htmlFor="key-edit-customer">
          {t("encryptionKeys.tenant")}
        </label>
        <select
          id="key-edit-customer"
          className="input"
          value={customer}
          onChange={(e) => setCustomer(e.target.value)}
        >
          <option value="">{t("encryptionKeys.noTenant")}</option>
          {(customersQuery.data ?? []).map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
      </div>
      <div className="formActions">
        <button
          type="button"
          className="button"
          onClick={onClose}
          disabled={save.isPending}
        >
          {t("common.cancel")}
        </button>
        <button
          type="button"
          className="button buttonPrimary"
          onClick={() => save.mutate()}
          disabled={save.isPending}
        >
          {t("common.save")}
        </button>
      </div>
    </Modal>
  );
}

/** Platform admins only: may this tenant's admins download their own keys? */
export function TenantKeyDownloadSwitch({
  customerId,
}: {
  customerId: string;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { hasRole } = useAuth();
  const isPlatformAdmin = hasRole("platform_admin");
  const queryKey = ["encryption-keys", "tenant-permission", customerId];
  const permission = useQuery({
    queryKey,
    queryFn: () => encryptionKeysApi.tenantPermission(customerId),
  });
  const toggle = useMutation({
    mutationFn: (allowed: boolean) =>
      encryptionKeysApi.setTenantPermission(customerId, allowed),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
      queryClient.invalidateQueries({ queryKey: ENCRYPTION_KEYS_KEY });
    },
  });
  const allowed = permission.data?.allowed ?? false;
  if (!isPlatformAdmin) {
    return (
      <p className="mutedText">
        {allowed
          ? t("encryptionKeys.tenantDownloadAllowed")
          : t("encryptionKeys.tenantDownloadNotAllowed")}
      </p>
    );
  }
  return (
    <label
      className="checkboxLabel"
      style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}
    >
      <input
        type="checkbox"
        checked={allowed}
        disabled={permission.isLoading || toggle.isPending}
        onChange={(e) => toggle.mutate(e.target.checked)}
      />
      {t("encryptionKeys.tenantDownloadSwitch")}
    </label>
  );
}

/** The key table with its actions. `customerId` narrows it to one tenant
 * (the tenant panel); without it, every key the caller may see. */
export function EncryptionKeysPanel({ customerId }: { customerId?: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { hasRole } = useAuth();
  const isPlatformAdmin = hasRole("platform_admin");
  const [uploading, setUploading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [importing, setImporting] = useState(false);
  const [downloading, setDownloading] = useState<{
    key: EncryptionKeyOut;
    kind: "keyfile" | "sheet";
  } | null>(null);
  const [editing, setEditing] = useState<EncryptionKeyOut | null>(null);
  const [deleting, setDeleting] = useState<EncryptionKeyOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  const keysQuery = useQuery({
    queryKey: ENCRYPTION_KEYS_KEY,
    queryFn: encryptionKeysApi.list,
  });
  const missingQuery = useQuery({
    queryKey: ["encryption-keys", "missing"],
    queryFn: encryptionKeysApi.missing,
    enabled: isPlatformAdmin && !customerId,
  });

  const remove = useMutation({
    mutationFn: (key: EncryptionKeyOut) =>
      encryptionKeysApi.remove(key.id, key.backup_count > 0),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ENCRYPTION_KEYS_KEY });
      queryClient.invalidateQueries({ queryKey: ["datastores"] });
    },
    onError: (err) => setError(errorText(err, t("common.loadError"))),
  });

  const columns: DataTableColumn<EncryptionKeyOut>[] = [
    {
      key: "fingerprint",
      header: t("encryptionKeys.fingerprint"),
      render: (r) => (
        <span
          title={r.fingerprint}
          style={{ fontFamily: "var(--font-mono, monospace)" }}
        >
          {shortFingerprint(r.fingerprint)}
        </span>
      ),
      sortValue: (r) => r.fingerprint,
    },
    {
      key: "label",
      header: t("encryptionKeys.label"),
      render: (r) => r.label || "—",
      sortValue: (r) => r.label,
    },
    ...(customerId
      ? []
      : [
          {
            key: "tenant",
            header: t("encryptionKeys.tenant"),
            render: (r: EncryptionKeyOut) => r.customer_name ?? "—",
            sortValue: (r: EncryptionKeyOut) => r.customer_name ?? "",
          },
        ]),
    {
      key: "backups",
      header: t("encryptionKeys.backups"),
      render: (r) => r.backup_count,
      sortValue: (r) => r.backup_count,
    },
    {
      key: "copy",
      header: t("encryptionKeys.copy"),
      render: (r) =>
        r.copy_confirmed_at ? (
          <span title={new Date(r.copy_confirmed_at).toLocaleString()}>
            <StatusBadge tone="ok" label={t("encryptionKeys.copyConfirmed")} />
          </span>
        ) : (
          <StatusBadge tone="warning" label={t("encryptionKeys.copyMissing")} />
        ),
    },
    {
      key: "actions",
      header: t("common.actions"),
      render: (r) => (
        <span
          style={{ display: "inline-flex", gap: "0.25rem", flexWrap: "wrap" }}
        >
          {r.can_download && (
            <>
              <button
                type="button"
                className="button buttonGhost"
                onClick={() => setDownloading({ key: r, kind: "sheet" })}
              >
                {t("encryptionKeys.keySheet")}
              </button>
              <button
                type="button"
                className="button buttonGhost"
                onClick={() => setDownloading({ key: r, kind: "keyfile" })}
              >
                {t("encryptionKeys.keyfile")}
              </button>
            </>
          )}
          {isPlatformAdmin && (
            <>
              <button
                type="button"
                className="button buttonGhost"
                onClick={() => setEditing(r)}
              >
                {t("common.edit")}
              </button>
              <button
                type="button"
                className="button buttonGhost"
                onClick={() => setDeleting(r)}
              >
                {t("common.delete")}
              </button>
            </>
          )}
        </span>
      ),
    },
  ];

  const missing = missingQuery.data ?? [];

  return (
    <Panel
      title={t("encryptionKeys.panelTitle")}
      padded={false}
      actions={
        isPlatformAdmin ? (
          <span style={{ display: "inline-flex", gap: "0.25rem" }}>
            <button
              type="button"
              className="button buttonPrimary"
              onClick={() => setUploading(true)}
            >
              {t("encryptionKeys.upload")}
            </button>
            {!customerId && (
              <>
                <button
                  type="button"
                  className="button"
                  onClick={() => setExporting(true)}
                >
                  {t("encryptionKeys.export")}
                </button>
                <button
                  type="button"
                  className="button"
                  onClick={() => setImporting(true)}
                >
                  {t("encryptionKeys.import")}
                </button>
              </>
            )}
          </span>
        ) : undefined
      }
    >
      <div className="panelBody">
        {customerId && <TenantKeyDownloadSwitch customerId={customerId} />}
        {error && <InlineAlert>{error}</InlineAlert>}
        {missing.length > 0 && (
          <InlineAlert>
            {t("encryptionKeys.missingAlert", { count: missing.length })}{" "}
            <span style={{ fontFamily: "var(--font-mono, monospace)" }}>
              {missing.map(shortFingerprint).join(", ")}
            </span>
          </InlineAlert>
        )}
      </div>
      <QueryState
        query={keysQuery}
        isEmpty={(data) =>
          data.filter((k) => !customerId || k.customer_id === customerId)
            .length === 0
        }
      >
        {(data) => (
          <DataTable
            columns={columns}
            rows={data.filter(
              (k) => !customerId || k.customer_id === customerId,
            )}
            rowKey={(r) => r.id}
          />
        )}
      </QueryState>

      {uploading && (
        <UploadKeyModal
          customerId={customerId}
          onClose={() => setUploading(false)}
        />
      )}
      {exporting && <ExportKeysModal onClose={() => setExporting(false)} />}
      {importing && <ImportKeysModal onClose={() => setImporting(false)} />}
      {editing && (
        <EditKeyModal keyRow={editing} onClose={() => setEditing(null)} />
      )}
      {downloading && (
        <DownloadKeyModal
          keyRow={downloading.key}
          kind={downloading.kind}
          onClose={() => setDownloading(null)}
        />
      )}
      <ConfirmActionDialog
        open={deleting !== null}
        title={t("encryptionKeys.deleteTitle", {
          key: deleting ? shortFingerprint(deleting.fingerprint) : "",
        })}
        body={
          deleting && deleting.backup_count > 0
            ? t("encryptionKeys.deleteInUseBody", {
                count: deleting.backup_count,
              })
            : t("encryptionKeys.deleteBody")
        }
        confirmLabel={t("common.delete")}
        danger
        onConfirm={() => {
          if (deleting) remove.mutate(deleting);
          setDeleting(null);
        }}
        onCancel={() => setDeleting(null)}
      />
    </Panel>
  );
}
