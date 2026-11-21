import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Navigate } from "react-router-dom";

import { InlineAlert } from "../components/InlineAlert";
import { useAuth } from "../contexts/AuthContext";

/** Reads the session token from the URL fragment the backend's OIDC
 * callback redirected here with (never sent to any server — read
 * client-side only, see backend ADR "real OIDC via authentik"). */
export function AuthCallbackPage() {
  const { t } = useTranslation();
  const { loginWithToken } = useAuth();
  const [status, setStatus] = useState<"processing" | "done" | "failed">("processing");

  useEffect(() => {
    const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    const accessToken = fragment.get("access_token");
    if (!accessToken) {
      setStatus("failed");
      return;
    }
    loginWithToken(accessToken)
      .then(() => setStatus("done"))
      .catch(() => setStatus("failed"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (status === "done") {
    return <Navigate to="/" replace />;
  }

  if (status === "failed") {
    return <InlineAlert>{t("auth.callbackFailed")}</InlineAlert>;
  }

  return <p className="mutedText">{t("auth.callbackProcessing")}</p>;
}
