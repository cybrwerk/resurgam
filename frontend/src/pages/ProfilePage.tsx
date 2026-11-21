import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import QRCode from "qrcode";

import { ApiError } from "../api/client";
import { authApi, mfaApi } from "../api/resources";
import type { MfaStatusOut, WebauthnCeremonyOptionsOut } from "../api/types";
import { MyApiTokensPanel } from "../components/ApiTokens";
import { ConfirmActionDialog } from "../components/ConfirmActionDialog";
import { InlineAlert } from "../components/InlineAlert";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { useAuth } from "../contexts/AuthContext";
import { isWebauthnSupported, registerWebauthnCredential } from "../lib/webauthn";
import styles from "./ProfilePage.module.css";

const MFA_STATUS_KEY = ["mfa-status"];

/** Confirmed TOTP + registered security keys. While MFA is required, the
 * last one cannot be removed (server: 409 `mfa_required_last_factor`), so
 * the UI disables its Remove button up front. */
function factorCount(status: MfaStatusOut | undefined): number {
  if (!status) return 0;
  return (status.totp_enabled ? 1 : 0) + status.webauthn_credentials.length;
}

function isLastRequiredFactor(status: MfaStatusOut | undefined): boolean {
  return Boolean(status?.mfa_required) && factorCount(status) <= 1;
}

function ChangePasswordCard() {
  const { t } = useTranslation();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  const mutation = useMutation({
    mutationFn: () => authApi.changePassword(currentPassword, newPassword),
    onSuccess: () => {
      setSuccess(true);
      setCurrentPassword("");
      setNewPassword("");
    },
    onError: (err) => {
      setSuccess(false);
      setError(err instanceof ApiError ? err.message : t("common.loadError"));
    },
  });

  return (
    <Panel title={t("profile.changePasswordTitle")}>
      {error && <InlineAlert>{error}</InlineAlert>}
      {success && (
        <InlineAlert tone="success">{t("profile.passwordChanged")}</InlineAlert>
      )}
      <form
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          setError(null);
          setSuccess(false);
          mutation.mutate();
        }}
      >
        <div className="formGrid">
          <div className="field">
            <label className="label" htmlFor="current-password">
              {t("profile.currentPassword")}
            </label>
            <input
              id="current-password"
              type="password"
              className="input"
              autoComplete="current-password"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="new-password">
              {t("profile.newPassword")}
            </label>
            <input
              id="new-password"
              type="password"
              className="input"
              autoComplete="new-password"
              minLength={8}
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              required
            />
          </div>
        </div>
        <div className="formActions">
          <button type="submit" className="button buttonPrimary" disabled={mutation.isPending}>
            {t("profile.changePassword")}
          </button>
        </div>
      </form>
    </Panel>
  );
}

function TotpCard() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const statusQuery = useQuery({ queryKey: MFA_STATUS_KEY, queryFn: mfaApi.status });
  const [enrolling, setEnrolling] = useState(false);
  const [secret, setSecret] = useState<string | null>(null);
  const [qrDataUrl, setQrDataUrl] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [confirmRemove, setConfirmRemove] = useState(false);

  const enrollMutation = useMutation({
    mutationFn: () => mfaApi.totpEnroll(),
    onSuccess: (result) => {
      setSecret(result.secret);
      setEnrolling(true);
      setError(null);
      QRCode.toDataURL(result.otpauth_uri).then(setQrDataUrl).catch(() => setQrDataUrl(null));
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const confirmMutation = useMutation({
    mutationFn: () => mfaApi.totpConfirm(code),
    onSuccess: () => {
      setEnrolling(false);
      setSecret(null);
      setCode("");
      queryClient.invalidateQueries({ queryKey: MFA_STATUS_KEY });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError"))
  });

  const removeMutation = useMutation({
    mutationFn: () => mfaApi.totpRemove(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: MFA_STATUS_KEY }),
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const enabled = statusQuery.data?.totp_enabled ?? false;
  const lastRequired = isLastRequiredFactor(statusQuery.data);

  return (
    <Panel
      title={t("profile.totpTitle")}
      actions={
        <span className={enabled ? styles.badgeOk : styles.badgeMuted}>
          {enabled ? t("profile.totpEnabled") : t("profile.totpDisabled")}
        </span>
      }
    >
      {error && <InlineAlert>{error}</InlineAlert>}

      {!enabled && !enrolling && (
        <button type="button" className="button buttonPrimary" onClick={() => enrollMutation.mutate()}>
          {t("profile.totpEnroll")}
        </button>
      )}

      {enrolling && secret && (
        <div className={styles.totpEnroll}>
          <p className="mutedText">{t("profile.totpScanQr")}</p>
          {qrDataUrl && <img src={qrDataUrl} alt="TOTP QR code" className={styles.qrCode} />}
          <p className={styles.secretText}>
            {t("profile.totpSecret")}: <code>{secret}</code>
          </p>
          <form
            className={styles.inlineForm}
            onSubmit={(event: FormEvent) => {
              event.preventDefault();
              setError(null);
              confirmMutation.mutate();
            }}
          >
            <input
              className="input"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder={t("profile.totpCode")}
              inputMode="numeric"
              required
            />
            <button type="submit" className="button buttonPrimary" disabled={confirmMutation.isPending}>
              {t("profile.totpConfirm")}
            </button>
          </form>
        </div>
      )}

      {enabled && (
        <>
          <button
            type="button"
            className="button buttonDanger"
            disabled={lastRequired}
            aria-describedby={lastRequired ? "totp-last-factor-hint" : undefined}
            onClick={() => setConfirmRemove(true)}
          >
            {t("profile.totpRemove")}
          </button>
          {lastRequired && (
            <p id="totp-last-factor-hint" className={`mutedText ${styles.hint}`}>
              {t("profile.lastFactorTotpHint")}
            </p>
          )}
        </>
      )}

      <ConfirmActionDialog
        open={confirmRemove}
        title={t("profile.totpConfirmRemoveTitle")}
        body={t("profile.totpConfirmRemoveBody")}
        danger
        confirmLabel={t("profile.totpRemove")}
        onCancel={() => setConfirmRemove(false)}
        onConfirm={() => {
          removeMutation.mutate();
          setConfirmRemove(false);
        }}
      />
    </Panel>
  );
}

function WebauthnCard() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const statusQuery = useQuery({ queryKey: MFA_STATUS_KEY, queryFn: mfaApi.status });
  const [nickname, setNickname] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pendingRemove, setPendingRemove] = useState<{ id: string; nickname: string } | null>(null);

  const removeMutation = useMutation({
    mutationFn: (id: string) => mfaApi.webauthnRemove(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: MFA_STATUS_KEY }),
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const handleRegister = async () => {
    setError(null);
    setBusy(true);
    try {
      const optionsOut: WebauthnCeremonyOptionsOut = await mfaApi.webauthnRegisterOptions(
        nickname || t("auth.mfaEnroll.defaultKeyName")
      );
      const credential = await registerWebauthnCredential(optionsOut.options);
      await mfaApi.webauthnRegister(optionsOut.ceremony_token, credential);
      setNickname("");
      queryClient.invalidateQueries({ queryKey: MFA_STATUS_KEY });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("common.loadError"));
    } finally {
      setBusy(false);
    }
  };

  if (!isWebauthnSupported()) {
    return (
      <Panel title={t("profile.webauthnTitle")}>
        <p className="mutedText">{t("profile.webauthnNotSupported")}</p>
      </Panel>
    );
  }

  const credentials = statusQuery.data?.webauthn_credentials ?? [];
  const lastRequired = isLastRequiredFactor(statusQuery.data);

  return (
    <Panel title={t("profile.webauthnTitle")}>
      {error && <InlineAlert>{error}</InlineAlert>}

      {credentials.length === 0 ? (
        <p className="mutedText">{t("profile.webauthnNone")}</p>
      ) : (
        <ul className={styles.list}>
          {credentials.map((c) => (
            <li key={c.id} className={styles.listRow}>
              <span>{c.nickname}</span>
              <button
                type="button"
                className="button buttonDanger"
                disabled={lastRequired}
                aria-describedby={lastRequired ? "webauthn-last-factor-hint" : undefined}
                onClick={() => setPendingRemove({ id: c.id, nickname: c.nickname })}
              >
                {t("profile.webauthnRemove")}
              </button>
            </li>
          ))}
        </ul>
      )}
      {credentials.length > 0 && lastRequired && (
        <p id="webauthn-last-factor-hint" className={`mutedText ${styles.hint}`}>
          {t("profile.lastFactorKeyHint")}
        </p>
      )}

      <div className={styles.inlineForm}>
        <input
          className="input"
          value={nickname}
          onChange={(e) => setNickname(e.target.value)}
          placeholder={t("profile.webauthnNicknamePlaceholder")}
        />
        <button type="button" className="button buttonPrimary" onClick={handleRegister} disabled={busy}>
          {t("profile.webauthnRegister")}
        </button>
      </div>

      <ConfirmActionDialog
        open={pendingRemove !== null}
        title={t("profile.webauthnConfirmRemoveTitle")}
        body={t("profile.webauthnConfirmRemoveBody", { name: pendingRemove?.nickname })}
        danger
        confirmLabel={t("profile.webauthnRemove")}
        onCancel={() => setPendingRemove(null)}
        onConfirm={() => {
          if (pendingRemove) removeMutation.mutate(pendingRemove.id);
          setPendingRemove(null);
        }}
      />
    </Panel>
  );
}

function RecoveryCodesCard() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const statusQuery = useQuery({ queryKey: MFA_STATUS_KEY, queryFn: mfaApi.status });
  const [codes, setCodes] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmGenerate, setConfirmGenerate] = useState(false);

  const generateMutation = useMutation({
    mutationFn: () => mfaApi.recoveryCodesGenerate(),
    onSuccess: (result) => {
      setCodes(result.codes);
      setError(null);
      queryClient.invalidateQueries({ queryKey: MFA_STATUS_KEY });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  const hasPrimaryMethod = (statusQuery.data?.totp_enabled ?? false) || (statusQuery.data?.webauthn_credentials.length ?? 0) > 0;
  const remaining = statusQuery.data?.recovery_codes_remaining ?? 0;

  return (
    <Panel title={t("profile.recoveryCodesTitle")}>
      {error && <InlineAlert>{error}</InlineAlert>}

      {!hasPrimaryMethod ? (
        <p className="mutedText">{t("profile.recoveryCodesRequirePrimary")}</p>
      ) : (
        <>
          <p className="mutedText">{t("profile.recoveryCodesRemaining", { count: remaining })}</p>
          <button type="button" className="button buttonPrimary" onClick={() => setConfirmGenerate(true)}>
            {t("profile.recoveryCodesGenerate")}
          </button>
        </>
      )}

      {codes && (
        <div className={styles.codesBlock}>
          <p className="mutedText">{t("profile.recoveryCodesDownloadHint")}</p>
          <ul className={styles.codesList}>
            {codes.map((c) => (
              <li key={c}>
                <code>{c}</code>
              </li>
            ))}
          </ul>
        </div>
      )}

      <ConfirmActionDialog
        open={confirmGenerate}
        title={t("profile.recoveryCodesConfirmTitle")}
        body={t("profile.recoveryCodesConfirmBody")}
        confirmLabel={t("profile.recoveryCodesGenerate")}
        onCancel={() => setConfirmGenerate(false)}
        onConfirm={() => {
          generateMutation.mutate();
          setConfirmGenerate(false);
        }}
      />
    </Panel>
  );
}

export function ProfilePage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const statusQuery = useQuery({ queryKey: MFA_STATUS_KEY, queryFn: mfaApi.status });

  const isLocalAccount = !statusQuery.isError || !(statusQuery.error instanceof ApiError) || statusQuery.error.code !== "no_local_account";

  return (
    <div>
      <PageHeader title={t("profile.title")} />

      <div className="stack">
        <Panel title={t("profile.accountSection")}>
          <dl className="detailGrid">
            <dt>{t("profile.email")}</dt>
            <dd>{user?.email}</dd>
            <dt>{t("profile.displayName")}</dt>
            <dd>{user?.display_name}</dd>
          </dl>
        </Panel>

        {!isLocalAccount ? (
          <InlineAlert tone="info">{t("profile.notLocalAccount")}</InlineAlert>
        ) : (
          <>
            <ChangePasswordCard />
            <h2 className={styles.sectionTitle}>{t("profile.mfaSection")}</h2>
            {statusQuery.data?.mfa_required && <InlineAlert tone="info">{t("profile.mfaRequiredByAdmin")}</InlineAlert>}
            <TotpCard />
            <WebauthnCard />
            <RecoveryCodesCard />
          </>
        )}

        <MyApiTokensPanel isBreakglass={user?.is_breakglass ?? false} />
      </div>
    </div>
  );
}
