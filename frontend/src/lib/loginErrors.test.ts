import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";
import i18n from "../i18n";
import { lockedMessage } from "./loginErrors";

const t = i18n.t.bind(i18n);

describe("lockedMessage", () => {
  it("says how many minutes are left", () => {
    const err = new ApiError(429, "login_locked", "Too many", undefined, { retry_after_seconds: 601 });
    expect(lockedMessage(err, t)).toBe("Too many wrong attempts. Try again in 11 minutes.");
    const last = new ApiError(429, "login_locked", "Too many", undefined, { retry_after_seconds: 30 });
    expect(lockedMessage(last, t)).toBe("Too many wrong attempts. Try again in 1 minute.");
  });

  it("ignores every other error", () => {
    expect(lockedMessage(new ApiError(401, "invalid_credentials", "Invalid"), t)).toBeNull();
    expect(lockedMessage(new Error("network"), t)).toBeNull();
  });
});
