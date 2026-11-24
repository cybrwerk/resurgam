import { useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Navigate, useLocation, useSearchParams } from "react-router-dom";

import { API_BASE_URL } from "../api/client";
import { authApi, setupApi } from "../api/resources";
import { ApiError } from "../api/client";
import { InlineAlert } from "../components/InlineAlert";
import { MfaEnrollmentStep } from "../components/MfaEnrollmentStep";
import { SetupWizard } from "../components/SetupWizard";
import { useAuth } from "../contexts/AuthContext";
import { useBranding } from "../contexts/BrandingContext";
import { lockedMessage } from "../lib/loginErrors";
import { getWebauthnAssertion, isWebauthnSupported } from "../lib/webauthn";
import styles from "./LoginPage.module.css";

const OIDC_ERROR_KEYS: Record<string, string> = {
  oidc_login_failed: "auth.ssoFailed",
  oidc_provisioning_failed: "auth.ssoProvisioningFailed",
};

interface MfaChallenge {
  challengeToken: string;
  availableMethods: string[];
}

function MfaStep({ challenge, onDone }: { challenge: MfaChallenge; onDone: (token: string) => void }) {
  const { t } = useTranslation();
  const [method, setMethod] = useState(challenge.availableMethods[0]);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const methodLabel: Record<string, string> = {
    totp: t("auth.mfaMethodTotp"),
    recovery_code: t("auth.mfaMethodRecoveryCode"),
    webauthn: t("auth.mfaMethodWebauthn"),
  };

  const handleCodeSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const response =
        method === "totp"
          ? await authApi.verifyTotp(challenge.challengeToken, code)
          : await authApi.verifyRecoveryCode(challenge.challengeToken, code);
      onDone(response.access_token);
    } catch (err) {
      setError(lockedMessage(err, t) ?? (err instanceof ApiError && err.code === "invalid_code" ? t("auth.mfaInvalidCode") : t("common.loadError")));
    } finally {
      setSubmitting(false);
    }
  };

  const handleWebauthn = async () => {
    setError(null);
    setSubmitting(true);
    try {
      const optionsOut = await authApi.webauthnAuthenticateOptions(challenge.challengeToken);
      const credential = await getWebauthnAssertion(optionsOut.options);
      const response = await authApi.webauthnAuthenticate(optionsOut.ceremony_token, credential);
      onDone(response.access_token);
    } catch (err) {
      setError(err instanceof ApiError ? t("auth.mfaWebauthnFailed") : t("common.loadError"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div>
      <h1 className={styles.title}>{t("auth.mfaTitle")}</h1>
      {error && <InlineAlert>{error}</InlineAlert>}

      {challenge.availableMethods.length > 1 && (
        <div className={styles.divider}>{t("auth.mfaChooseMethod")}</div>
      )}
      {challenge.availableMethods.length > 1 && (
        <div className="field">
          <select className="select" value={method} onChange={(e) => setMethod(e.target.value)}>
            {challenge.availableMethods.map((m) => (
              <option key={m} value={m}>
                {methodLabel[m] ?? m}
              </option>
            ))}
          </select>
        </div>
      )}

      {method === "webauthn" ? (
        <button
          type="button"
          className={`button buttonPrimary ${styles.fullWidth}`}
          onClick={handleWebauthn}
          disabled={submitting || !isWebauthnSupported()}
        >
          {t("auth.mfaUseWebauthn")}
        </button>
      ) : (
        <form onSubmit={handleCodeSubmit}>
          <div className="field">
            <label className="label" htmlFor="mfa-code">
              {t("auth.mfaCode")}
            </label>
            <input
              id="mfa-code"
              className="input"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              autoFocus
              required
            />
          </div>
          <button type="submit" className={`button buttonPrimary ${styles.fullWidth}`} disabled={submitting}>
            {t("auth.mfaVerify")}
          </button>
        </form>
      )}
    </div>
  );
}

export function LoginPage() {
  const { t } = useTranslation();
  const { logoConfigured, logoUrl, appName } = useBranding();
  const { token, loginWithToken } = useAuth();
  const location = useLocation();
  const [searchParams] = useSearchParams();

  const [email, setEmail] = useState("");
  const [localPassword, setLocalPassword] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const [localSubmitting, setLocalSubmitting] = useState(false);
  const [mfaChallenge, setMfaChallenge] = useState<MfaChallenge | null>(null);
  const [enrollmentToken, setEnrollmentToken] = useState<string | null>(null);

  const [showBreakglass, setShowBreakglass] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [breakglassError, setBreakglassError] = useState<string | null>(null);
  const [breakglassSubmitting, setBreakglassSubmitting] = useState(false);

  // First-boot setup wizard (manifest §5.1/§17 Milestone 5) -- shown in
  // place of the normal login form until a real admin account exists.
  // Unauthenticated on purpose, see GET /setup/status's own docstring.
  const setupStatusQuery = useQuery({ queryKey: ["setup-status"], queryFn: setupApi.status });

  if (token) {
    const from = (location.state as { from?: { pathname: string } } | null)?.from;
    return <Navigate to={from?.pathname ?? "/"} replace />;
  }

  const oidcErrorCode = searchParams.get("oidc_error");
  const oidcErrorMessage = oidcErrorCode ? t(OIDC_ERROR_KEYS[oidcErrorCode] ?? "auth.ssoFailed") : null;

  const handleLocalSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setLocalError(null);
    setLocalSubmitting(true);
    try {
      const response = await authApi.localLogin(email, localPassword);
      if (response.mfa_enrollment_required && response.enrollment_token) {
        setEnrollmentToken(response.enrollment_token);
      } else if (response.mfa_required && response.challenge_token) {
        setMfaChallenge({ challengeToken: response.challenge_token, availableMethods: response.available_methods });
      } else if (response.access_token) {
        await loginWithToken(response.access_token);
      }
    } catch (err) {
      setLocalError(lockedMessage(err, t) ?? (err instanceof ApiError && err.code === "invalid_credentials" ? t("auth.invalidCredentials") : t("common.loadError")));
    } finally {
      setLocalSubmitting(false);
    }
  };

  const handleBreakglassSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setBreakglassError(null);
    setBreakglassSubmitting(true);
    try {
      const response = await authApi.breakglassLogin(username, password);
      await loginWithToken(response.access_token);
    } catch (err) {
      const locked = lockedMessage(err, t);
      if (locked) {
        setBreakglassError(locked);
      } else if (err instanceof ApiError && err.code === "breakglass_disabled") {
        setBreakglassError(t("auth.breakglassDisabled"));
      } else if (err instanceof ApiError && err.code === "invalid_credentials") {
        setBreakglassError(t("auth.invalidCredentials"));
      } else {
        setBreakglassError(t("common.loadError"));
      }
    } finally {
      setBreakglassSubmitting(false);
    }
  };

  const handleSso = () => {
    window.location.href = `${API_BASE_URL}/auth/oidc/login`;
  };

  return (
    <div className={styles.page}>
      <div className={`panelPadded ${styles.card}`}>
        {logoConfigured ? (
          <img src={logoUrl} alt={appName ?? "RESURGAM Backup Center"} className={styles.brandLogo} />
        ) : (
          <div className={styles.brandLockup} role="img" aria-label={appName ?? "RESURGAM Backup Center"}>
            <img src="/logo-mark.svg" alt="" className={styles.brandMark} />
            <span className={styles.brandText}>
              <span className={styles.brandName}>{appName ?? "RESURGAM"}</span>
              {!appName && <span className={styles.brandSub}>Backup Center</span>}
              {!appName && <span className={styles.brandTag}>a cybrwerk project</span>}
            </span>
          </div>
        )}
        {logoConfigured && appName && <h2 className={styles.appName}>{appName}</h2>}

        {oidcErrorMessage && <InlineAlert>{oidcErrorMessage}</InlineAlert>}

        {setupStatusQuery.data?.needs_setup ? (
          <SetupWizard status={setupStatusQuery.data} onComplete={(accessToken) => void loginWithToken(accessToken)} />
        ) : enrollmentToken ? (
          <MfaEnrollmentStep
            enrollmentToken={enrollmentToken}
            onDone={(accessToken) => void loginWithToken(accessToken)}
            onCancel={() => {
              setEnrollmentToken(null);
              setLocalPassword("");
            }}
          />
        ) : mfaChallenge ? (
          <MfaStep challenge={mfaChallenge} onDone={(accessToken) => void loginWithToken(accessToken)} />
        ) : (
          <>
            <h1 className={styles.title}>{t("auth.localSignInTitle")}</h1>
            {localError && <InlineAlert>{localError}</InlineAlert>}
            <form onSubmit={handleLocalSubmit}>
              <div className="field">
                <label className="label" htmlFor="email">
                  {t("auth.email")}
                </label>
                <input
                  id="email"
                  type="email"
                  className="input"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  autoComplete="username"
                />
              </div>
              <div className="field">
                <label className="label" htmlFor="local-password">
                  {t("auth.password")}
                </label>
                <input
                  id="local-password"
                  type="password"
                  className="input"
                  value={localPassword}
                  onChange={(e) => setLocalPassword(e.target.value)}
                  autoComplete="current-password"
                />
              </div>
              <button type="submit" className={`button buttonPrimary ${styles.fullWidth}`} disabled={localSubmitting}>
                {localSubmitting ? t("auth.signingIn") : t("auth.signIn")}
              </button>
            </form>

            <div className={styles.divider}>{t("auth.or")}</div>

            <button type="button" className={`button ${styles.fullWidth}`} onClick={handleSso}>
              {t("auth.signInSso")}
            </button>

            {!showBreakglass ? (
              <button
                type="button"
                className={`button buttonGhost ${styles.fullWidth}`}
                onClick={() => setShowBreakglass(true)}
              >
                {t("auth.emergencyAccess")}
              </button>
            ) : (
              <div>
                <div className={styles.divider}>{t("auth.emergencyAccess")}</div>
                {breakglassError && <InlineAlert>{breakglassError}</InlineAlert>}
                <form onSubmit={handleBreakglassSubmit}>
                  <div className="field">
                    <label className="label" htmlFor="username">
                      {t("auth.username")}
                    </label>
                    <input
                      id="username"
                      className="input"
                      value={username}
                      onChange={(e) => setUsername(e.target.value)}
                      autoComplete="username"
                    />
                  </div>
                  <div className="field">
                    <label className="label" htmlFor="password">
                      {t("auth.password")}
                    </label>
                    <input
                      id="password"
                      type="password"
                      className="input"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      autoComplete="current-password"
                    />
                  </div>
                  <button type="submit" className={`button ${styles.fullWidth}`} disabled={breakglassSubmitting}>
                    {breakglassSubmitting ? t("auth.signingIn") : t("auth.signIn")}
                  </button>
                </form>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
