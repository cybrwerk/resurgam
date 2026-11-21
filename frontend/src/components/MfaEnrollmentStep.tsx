import { useMemo, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import QRCode from "qrcode";

import { ApiError } from "../api/client";
import { authApi, mfaEnrollmentApi } from "../api/resources";
import { useBranding } from "../contexts/BrandingContext";
import { isWebauthnSupported, registerWebauthnCredential } from "../lib/webauthn";
import { CopyButton } from "./CopyButton";
import { InlineAlert } from "./InlineAlert";
import styles from "./MfaEnrollmentStep.module.css";

interface MfaEnrollmentStepProps {
  enrollmentToken: string;
  /** The real session token, once a factor is set up. */
  onDone: (accessToken: string) => void;
  /** Back to the sign-in form (expired setup session, or the user gives up). */
  onCancel: () => void;
}

type Stage = "choose" | "totp" | "codes";

/** Saves the recovery codes as a plain .txt file (one code per line). */
function downloadCodes(codes: string[]) {
  const blob = new Blob([`${codes.join("\n")}\n`], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "resurgam-recovery-codes.txt";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/** Forced MFA enrollment at login: an administrator requires MFA and the
 * account has none yet. The enrollment token reaches only the enrollment
 * endpoints; once a factor is proven it is swapped for a session. */
export function MfaEnrollmentStep({ enrollmentToken, onDone, onCancel }: MfaEnrollmentStepProps) {
  const { t } = useTranslation();
  const { appName } = useBranding();
  const api = useMemo(() => mfaEnrollmentApi(enrollmentToken), [enrollmentToken]);
  const [stage, setStage] = useState<Stage>("choose");
  const [secret, setSecret] = useState<string | null>(null);
  const [qrDataUrl, setQrDataUrl] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [nickname, setNickname] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
  const [codesFailed, setCodesFailed] = useState(false);
  const [codesSaved, setCodesSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expired, setExpired] = useState(false);
  const [busy, setBusy] = useState(false);

  const fail = (err: unknown) => {
    if (err instanceof ApiError && (err.code === "invalid_session" || err.code === "invalid_enrollment_token")) {
      setExpired(true);
      setError(t("auth.mfaEnroll.expired"));
    } else if (err instanceof ApiError && err.code === "invalid_code") {
      setError(t("auth.mfaInvalidCode"));
    } else {
      setError(err instanceof ApiError ? err.message : t("common.loadError"));
    }
  };

  const run = async (action: () => Promise<void>) => {
    setError(null);
    setBusy(true);
    try {
      await action();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  };

  /** A factor is set up: hand out recovery codes before the session. */
  const factorReady = async () => {
    try {
      setRecoveryCodes((await api.recoveryCodesGenerate()).codes);
      setCodesFailed(false);
    } catch {
      // Optional -- can be generated later on the profile page; say so.
      setRecoveryCodes([]);
      setCodesFailed(true);
    }
    setStage("codes");
  };

  const startTotp = () =>
    run(async () => {
      const result = await api.totpEnroll();
      setSecret(result.secret);
      setQrDataUrl(await QRCode.toDataURL(result.otpauth_uri).catch(() => null));
      setStage("totp");
    });

  const confirmTotp = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      await api.totpConfirm(code.trim());
      await factorReady();
    });
  };

  const registerKey = () =>
    run(async () => {
      const options = await api.webauthnRegisterOptions(nickname.trim() || t("auth.mfaEnroll.defaultKeyName"));
      const credential = await registerWebauthnCredential(options.options);
      await api.webauthnRegister(options.ceremony_token, credential);
      await factorReady();
    });

  const finish = () =>
    run(async () => {
      const session = await authApi.completeMfaEnrollment(enrollmentToken);
      onDone(session.access_token);
    });

  return (
    <div data-testid="mfa-enrollment">
      <h1 className={styles.title}>{t("auth.mfaEnroll.title")}</h1>
      {error && <InlineAlert>{error}</InlineAlert>}

      {expired ? (
        <button type="button" className={`button buttonPrimary ${styles.fullWidth}`} onClick={onCancel}>
          {t("auth.mfaEnroll.backToSignIn")}
        </button>
      ) : stage === "choose" ? (
        <>
          <p className="mutedText">{t("auth.mfaEnroll.intro")}</p>
          <button
            type="button"
            className={`button buttonPrimary ${styles.fullWidth}`}
            disabled={busy}
            onClick={() => void startTotp()}
          >
            {t("auth.mfaEnroll.useApp")}
          </button>
          {isWebauthnSupported() && (
            <div className={styles.keyBlock}>
              <div className={styles.divider}>{t("auth.or")}</div>
              <div className="field">
                <label className="label" htmlFor="mfa-key-name">
                  {t("auth.mfaEnroll.keyName")}
                </label>
                <input
                  id="mfa-key-name"
                  className="input"
                  value={nickname}
                  placeholder={t("profile.webauthnNicknamePlaceholder")}
                  onChange={(e) => setNickname(e.target.value)}
                />
              </div>
              <button type="button" className={`button ${styles.fullWidth}`} disabled={busy} onClick={() => void registerKey()}>
                {t("auth.mfaEnroll.useKey")}
              </button>
            </div>
          )}
          <button type="button" className={`button buttonGhost ${styles.fullWidth}`} onClick={onCancel}>
            {t("common.cancel")}
          </button>
        </>
      ) : stage === "totp" ? (
        <form onSubmit={confirmTotp}>
          <p className="mutedText">{t("profile.totpScanQr")}</p>
          {qrDataUrl && <img src={qrDataUrl} alt={t("auth.mfaEnroll.qrAlt")} className={styles.qrCode} />}
          {secret && (
            <p className={styles.secret}>
              {t("profile.totpSecret")}: <code>{secret}</code>
            </p>
          )}
          <div className="field">
            <label className="label" htmlFor="mfa-enroll-code">
              {t("auth.mfaCode")}
            </label>
            <input
              id="mfa-enroll-code"
              className="input"
              value={code}
              inputMode="numeric"
              autoComplete="one-time-code"
              onChange={(e) => setCode(e.target.value)}
              autoFocus
              required
            />
          </div>
          <button type="submit" className={`button buttonPrimary ${styles.fullWidth}`} disabled={busy}>
            {t("profile.totpConfirm")}
          </button>
          <button type="button" className={`button buttonGhost ${styles.fullWidth}`} onClick={() => setStage("choose")}>
            {t("auth.mfaEnroll.back")}
          </button>
        </form>
      ) : (
        <>
          <InlineAlert tone="success">{t("auth.mfaEnroll.done")}</InlineAlert>
          {codesFailed && <InlineAlert>{t("auth.mfaEnroll.recoveryFailed")}</InlineAlert>}
          {recoveryCodes.length > 0 && (
            <>
              <p className="mutedText">{t("auth.mfaEnroll.recoveryIntro")}</p>
              <ul className={styles.codes} data-testid="mfa-enrollment-recovery-codes">
                {recoveryCodes.map((c) => (
                  <li key={c} className="mono">
                    {c}
                  </li>
                ))}
              </ul>
              <div className={styles.codeActions}>
                <CopyButton text={recoveryCodes.join("\n")} label={t("auth.mfaEnroll.copyCodes")} />
                <button type="button" className="button" onClick={() => downloadCodes(recoveryCodes)}>
                  {t("auth.mfaEnroll.downloadCodes")}
                </button>
              </div>
              <label className={styles.savedCheck}>
                <input
                  type="checkbox"
                  checked={codesSaved}
                  onChange={(e) => setCodesSaved(e.target.checked)}
                  data-testid="mfa-enrollment-codes-saved"
                />
                {t("auth.mfaEnroll.codesSaved")}
              </label>
            </>
          )}
          <button
            type="button"
            className={`button buttonPrimary ${styles.fullWidth}`}
            disabled={busy || (recoveryCodes.length > 0 && !codesSaved)}
            onClick={() => void finish()}
          >
            {t("auth.mfaEnroll.continue", { appName: appName ?? "RESURGAM" })}
          </button>
        </>
      )}
    </div>
  );
}
