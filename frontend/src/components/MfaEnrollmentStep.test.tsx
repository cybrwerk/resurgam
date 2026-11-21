import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import { renderWithProviders } from "../test/testUtils";
import { MfaEnrollmentStep } from "./MfaEnrollmentStep";

const enrollment = {
  totpEnroll: vi.fn(),
  totpConfirm: vi.fn(),
  webauthnRegisterOptions: vi.fn(),
  webauthnRegister: vi.fn(),
  recoveryCodesGenerate: vi.fn(),
};

vi.mock("../api/resources", () => ({
  authApi: { completeMfaEnrollment: vi.fn() },
  mfaEnrollmentApi: vi.fn(() => enrollment),
}));
vi.mock("qrcode", () => ({ default: { toDataURL: vi.fn(() => Promise.resolve("data:image/png;base64,AAAA")) } }));
vi.mock("../lib/webauthn", () => ({ isWebauthnSupported: () => false, registerWebauthnCredential: vi.fn() }));

const { authApi, mfaEnrollmentApi } = await import("../api/resources");

beforeEach(() => {
  vi.clearAllMocks();
});

describe("MfaEnrollmentStep", () => {
  it("sets up an authenticator app, shows recovery codes, then hands over the session", async () => {
    enrollment.totpEnroll.mockResolvedValue({ secret: "JBSWY3DPEHPK3PXP", otpauth_uri: "otpauth://totp/x" });
    enrollment.totpConfirm.mockResolvedValue(undefined);
    enrollment.recoveryCodesGenerate.mockResolvedValue({ codes: ["aaaa-bbbb", "cccc-dddd"] });
    vi.mocked(authApi.completeMfaEnrollment).mockResolvedValue({ access_token: "session", token_type: "bearer", expires_in: 3600 });
    const onDone = vi.fn();
    const u = userEvent.setup();
    renderWithProviders(<MfaEnrollmentStep enrollmentToken="enroll-token" onDone={onDone} onCancel={vi.fn()} />);

    expect(mfaEnrollmentApi).toHaveBeenCalledWith("enroll-token");
    await u.click(screen.getByRole("button", { name: "Set up authenticator app" }));
    expect(await screen.findByText(/JBSWY3DPEHPK3PXP/)).toBeInTheDocument();
    await u.type(screen.getByLabelText("Code"), "123456");
    await u.click(screen.getByRole("button", { name: "Confirm" }));

    expect(await screen.findByTestId("mfa-enrollment-recovery-codes")).toHaveTextContent("aaaa-bbbb");
    expect(enrollment.totpConfirm).toHaveBeenCalledWith("123456");
    expect(screen.getByRole("button", { name: "Copy codes" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download .txt" })).toBeInTheDocument();
    // Continue needs the "saved" confirmation first.
    const continueButton = screen.getByRole("button", { name: "Continue to RESURGAM" });
    expect(continueButton).toBeDisabled();
    await u.click(screen.getByLabelText("I have saved these codes"));
    await u.click(continueButton);
    await waitFor(() => expect(onDone).toHaveBeenCalledWith("session"));
    expect(authApi.completeMfaEnrollment).toHaveBeenCalledWith("enroll-token");
  });

  it("explains a wrong code and an expired setup session", async () => {
    enrollment.totpEnroll.mockResolvedValue({ secret: "S", otpauth_uri: "otpauth://totp/x" });
    enrollment.totpConfirm.mockRejectedValueOnce(new ApiError(401, "invalid_code", "Invalid TOTP code."));
    enrollment.totpConfirm.mockRejectedValueOnce(new ApiError(401, "invalid_session", "expired"));
    const onCancel = vi.fn();
    const u = userEvent.setup();
    renderWithProviders(<MfaEnrollmentStep enrollmentToken="t" onDone={vi.fn()} onCancel={onCancel} />);

    await u.click(screen.getByRole("button", { name: "Set up authenticator app" }));
    await u.type(await screen.findByLabelText("Code"), "000000");
    await u.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();

    await u.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText("The setup session has expired. Sign in again to continue.")).toBeInTheDocument();
    await u.click(screen.getByRole("button", { name: "Back to sign-in" }));
    expect(onCancel).toHaveBeenCalled();
  });

  it("says where to get recovery codes when they could not be created", async () => {
    enrollment.totpEnroll.mockResolvedValue({ secret: "S", otpauth_uri: "otpauth://totp/x" });
    enrollment.totpConfirm.mockResolvedValue(undefined);
    enrollment.recoveryCodesGenerate.mockRejectedValue(new ApiError(500, "boom", "boom"));
    const u = userEvent.setup();
    renderWithProviders(<MfaEnrollmentStep enrollmentToken="t" onDone={vi.fn()} onCancel={vi.fn()} />);

    await u.click(screen.getByRole("button", { name: "Set up authenticator app" }));
    await u.type(await screen.findByLabelText("Code"), "123456");
    await u.click(screen.getByRole("button", { name: "Confirm" }));
    expect(
      await screen.findByText("Recovery codes could not be created — generate them later under Profile.")
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue to RESURGAM" })).toBeEnabled();
  });
});
