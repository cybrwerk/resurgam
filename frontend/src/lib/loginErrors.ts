import type { TFunction } from "i18next";

import { ApiError } from "../api/client";

/** The text for 429 `login_locked` (too many wrong passwords or codes, see the backend's
 * `auth/attempt_limit.py`): says how many minutes are left. Null for any other error. */
export function lockedMessage(err: unknown, t: TFunction): string | null {
  if (!(err instanceof ApiError) || err.code !== "login_locked") return null;
  const seconds = (err.details as { retry_after_seconds?: number } | undefined)?.retry_after_seconds ?? 15 * 60;
  return t("auth.loginLocked", { count: Math.max(1, Math.ceil(seconds / 60)) });
}
