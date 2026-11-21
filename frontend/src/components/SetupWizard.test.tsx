import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "../test/testUtils";
import { SetupWizard } from "./SetupWizard";

vi.mock("../api/resources", () => ({
  setupApi: { complete: vi.fn() },
  authApi: { localLogin: vi.fn() },
}));

const { setupApi, authApi } = await import("../api/resources");

describe("SetupWizard", () => {
  it("shows the KVM check as a warning, not a hard block, when unavailable", async () => {
    renderWithProviders(
      <SetupWizard status={{ needs_setup: true, kvm_available: false, kvm_detail: "No /dev/kvm." }} onComplete={vi.fn()} />,
    );
    expect(screen.getByText("Nested virtualization unavailable")).toBeInTheDocument();
    expect(screen.getByText("No /dev/kvm.")).toBeInTheDocument();
    // Still lets the operator continue -- everything except VM-path
    // restore works without nested KVM.
    expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled();
  });

  it("shows the KVM check as healthy with no detail text when available", () => {
    renderWithProviders(
      <SetupWizard status={{ needs_setup: true, kvm_available: true, kvm_detail: "ok" }} onComplete={vi.fn()} />,
    );
    expect(screen.getByText("Nested virtualization available")).toBeInTheDocument();
    expect(screen.queryByText("ok")).not.toBeInTheDocument();
  });

  it("creates the first admin account, logs in, and calls onComplete with the token", async () => {
    vi.mocked(setupApi.complete).mockResolvedValue({
      id: "u1", email: "admin@example.com", display_name: "First Admin", is_breakglass: false,
      disabled: false, has_password: true, created_at: "2026-01-01T00:00:00Z", roles: [], mfa_required: false, mfa_enrolled: false,
    });
    vi.mocked(authApi.localLogin).mockResolvedValue({
      mfa_required: false, access_token: "real-token", token_type: "bearer", expires_in: 900,
      challenge_token: null, available_methods: [],
    });
    const onComplete = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(
      <SetupWizard status={{ needs_setup: true, kvm_available: true, kvm_detail: "ok" }} onComplete={onComplete} />,
    );

    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.type(screen.getByLabelText("Display name"), "First Admin");
    await user.type(screen.getByLabelText("Email"), "admin@example.com");
    await user.type(screen.getByLabelText("Password"), "a-real-password");
    await user.type(screen.getByLabelText("Confirm password"), "a-real-password");
    await user.click(screen.getByRole("button", { name: "Create account and sign in" }));

    await waitFor(() =>
      expect(setupApi.complete).toHaveBeenCalledWith({
        email: "admin@example.com", display_name: "First Admin", password: "a-real-password",
      }),
    );
    await waitFor(() => expect(authApi.localLogin).toHaveBeenCalledWith("admin@example.com", "a-real-password"));
    await waitFor(() => expect(onComplete).toHaveBeenCalledWith("real-token"));
  });

  it("rejects a mismatched password confirmation before calling the API", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <SetupWizard status={{ needs_setup: true, kvm_available: true, kvm_detail: "ok" }} onComplete={vi.fn()} />,
    );

    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.type(screen.getByLabelText("Display name"), "First Admin");
    await user.type(screen.getByLabelText("Email"), "admin@example.com");
    await user.type(screen.getByLabelText("Password"), "a-real-password");
    await user.type(screen.getByLabelText("Confirm password"), "different-password");
    await user.click(screen.getByRole("button", { name: "Create account and sign in" }));

    expect(await screen.findByText("Passwords don't match.")).toBeInTheDocument();
    expect(setupApi.complete).not.toHaveBeenCalled();
  });
});
