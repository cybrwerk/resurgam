import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { authApi, setupApi } from "../api/resources";
import type { SetupStatusOut } from "../api/types";
import { InlineAlert } from "./InlineAlert";
import { StatusBadge } from "./StatusBadge";
import styles from "./SetupWizard.module.css";

interface SetupWizardProps {
  status: SetupStatusOut;
  onComplete: (accessToken: string) => void;
}

type Step = "welcome" | "account";

/** First-boot setup wizard (manifest §5.1/§17 Milestone 5), raised
 * directly by the user ("a guiding wizard on first boot would be
 * great!"). Shown by LoginPage in place of the normal login form
 * whenever GET /setup/status says no real admin account exists yet.
 * Two steps: a welcome/system-check (the nested-KVM functional test
 * manifest §5.2 requires, shown as an informational warning rather than
 * a hard gate -- everything except VM-path file restore still works
 * without it), then creating the first admin account. On success, logs
 * the new account straight in rather than bouncing back to a separate
 * login step.
 */
export function SetupWizard({ status, onComplete }: SetupWizardProps) {
  const { t } = useTranslation();
  const [step, setStep] = useState<Step>("welcome");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    if (password !== confirmPassword) {
      setError(t("setup.passwordMismatch"));
      return;
    }
    setSubmitting(true);
    try {
      await setupApi.complete({ email, display_name: displayName, password });
      const login = await authApi.localLogin(email, password);
      if (login.access_token) onComplete(login.access_token);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("common.loadError"));
    } finally {
      setSubmitting(false);
    }
  };

  if (step === "welcome") {
    return (
      <div>
        <h1 className={styles.title}>{t("setup.welcomeTitle")}</h1>
        <p className="mutedText">{t("setup.welcomeBody")}</p>

        <div className={styles.checkRow}>
          <StatusBadge
            tone={status.kvm_available ? "ok" : "warning"}
            label={status.kvm_available ? t("setup.kvmAvailable") : t("setup.kvmUnavailable")}
          />
        </div>
        {!status.kvm_available && <p className={styles.checkDetail}>{status.kvm_detail}</p>}

        <button type="button" className={`button buttonPrimary ${styles.fullWidth}`} onClick={() => setStep("account")}>
          {t("setup.continue")}
        </button>
      </div>
    );
  }

  return (
    <div>
      <h1 className={styles.title}>{t("setup.accountTitle")}</h1>
      <p className="mutedText">{t("setup.accountBody")}</p>
      {error && <InlineAlert>{error}</InlineAlert>}
      <form onSubmit={handleSubmit}>
        <div className="field">
          <label className="label" htmlFor="setup-display-name">
            {t("setup.displayName")}
          </label>
          <input
            id="setup-display-name"
            className="input"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            autoComplete="name"
            required
          />
        </div>
        <div className="field">
          <label className="label" htmlFor="setup-email">
            {t("auth.email")}
          </label>
          <input
            id="setup-email"
            type="email"
            className="input"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="username"
            required
          />
        </div>
        <div className="field">
          <label className="label" htmlFor="setup-password">
            {t("auth.password")}
          </label>
          <input
            id="setup-password"
            type="password"
            className="input"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
            minLength={8}
            required
          />
        </div>
        <div className="field">
          <label className="label" htmlFor="setup-password-confirm">
            {t("setup.confirmPassword")}
          </label>
          <input
            id="setup-password-confirm"
            type="password"
            className="input"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            autoComplete="new-password"
            minLength={8}
            required
          />
        </div>
        <button type="submit" className={`button buttonPrimary ${styles.fullWidth}`} disabled={submitting}>
          {submitting ? t("setup.creatingAccount") : t("setup.createAccount")}
        </button>
      </form>
    </div>
  );
}
