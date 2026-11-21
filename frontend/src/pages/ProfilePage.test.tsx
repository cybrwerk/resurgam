import { screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { MfaStatusOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { ProfilePage } from "./ProfilePage";

vi.mock("../api/resources", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/resources")>();
  return { ...actual, mfaApi: { ...actual.mfaApi, status: vi.fn() } };
});
vi.mock("../lib/webauthn", () => ({ isWebauthnSupported: () => true, registerWebauthnCredential: vi.fn() }));

const { mfaApi } = await import("../api/resources");

function status(overrides: Partial<MfaStatusOut> = {}): MfaStatusOut {
  return { totp_enabled: true, webauthn_credentials: [], recovery_codes_remaining: 0, mfa_required: true, ...overrides };
}

beforeEach(() => vi.clearAllMocks());

describe("ProfilePage — last required factor", () => {
  it("disables removing the only authenticator app while MFA is required, and says why", async () => {
    vi.mocked(mfaApi.status).mockResolvedValue(status());
    renderWithProviders(<ProfilePage />);
    expect(
      await screen.findByText("Add a security key first — your administrator requires a second factor.")
    ).toBeInTheDocument();
    const removeButtons = screen.getAllByRole("button", { name: "Remove" });
    expect(removeButtons[0]).toBeDisabled();
  });

  it("disables removing the only security key", async () => {
    vi.mocked(mfaApi.status).mockResolvedValue(
      status({ totp_enabled: false, webauthn_credentials: [{ id: "k1", nickname: "YubiKey", created_at: "2026-01-01T00:00:00Z" }] })
    );
    renderWithProviders(<ProfilePage />);
    const row = (await screen.findByText("YubiKey")).closest("li") as HTMLElement;
    expect(within(row).getByRole("button", { name: "Remove" })).toBeDisabled();
  });

  it("keeps Remove enabled with two factors or without the requirement", async () => {
    vi.mocked(mfaApi.status).mockResolvedValue(
      status({ webauthn_credentials: [{ id: "k1", nickname: "YubiKey", created_at: "2026-01-01T00:00:00Z" }] })
    );
    renderWithProviders(<ProfilePage />);
    await screen.findByText("YubiKey");
    for (const button of screen.getAllByRole("button", { name: "Remove" })) expect(button).toBeEnabled();
    expect(screen.queryByText(/your administrator requires a second factor/)).not.toBeInTheDocument();
  });
});
