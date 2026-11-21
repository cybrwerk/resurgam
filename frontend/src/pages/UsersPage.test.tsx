import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { CustomerOut, UserOut, UserRoleAssignmentOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { UsersPage } from "./UsersPage";

vi.mock("../api/resources", () => ({
  apiTokensApi: {
    all: vi.fn(() => Promise.resolve([])),
    revoke: vi.fn(),
  },
  usersApi: {
    list: vi.fn(),
    get: vi.fn(),
    create: vi.fn(),
    disable: vi.fn(),
    enable: vi.fn(),
    roleAssignments: vi.fn(),
    assignRole: vi.fn(),
    unassignRole: vi.fn(),
    setPassword: vi.fn(),
    setMfaRequired: vi.fn(),
    resetMfa: vi.fn(),
    mfaPolicy: vi.fn(() => Promise.resolve({ require_mfa_all_local_users: false })),
    setMfaPolicy: vi.fn(),
  },
  customersApi: { list: vi.fn() },
}));

const { usersApi, customersApi } = await import("../api/resources");

const CUSTOMER: CustomerOut = {
  id: "cust-1", name: "Sample Customer AG", slug: "sample-customer", debitor_number: "D-1000", match_code: "SAM",
  disabled: false, report_retention_days: null, created_at: "2026-01-01T00:00:00Z",
};

function role(overrides: Partial<UserRoleAssignmentOut> = {}): UserRoleAssignmentOut {
  return { id: "assignment-1", role: "platform_admin", scope_type: "global", scope_id: null, source: "manual", ...overrides };
}

function user(overrides: Partial<UserOut> = {}): UserOut {
  return {
    id: "user-1", email: "admin@example.com", display_name: "Admin User", is_breakglass: false,
    disabled: false, has_password: true, created_at: "2026-01-01T00:00:00Z", roles: [role()],
    mfa_required: false, mfa_enrolled: false,
    ...overrides,
  };
}

function setup() {
  vi.mocked(usersApi.list).mockResolvedValue([
    user(),
    user({ id: "user-2", email: "nobody@example.com", display_name: "Nobody Yet", roles: [] }),
  ]);
  vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
}

describe("UsersPage", () => {
  it("shows each user's role assignments directly in the list, not just their email/type", async () => {
    setup();
    renderWithProviders(<UsersPage />);

    // A real UX-audit finding (2026-09-23): this list used to show no
    // role at all -- answering "who has platform_admin" meant opening
    // every user one by one.
    await waitFor(() => expect(screen.getByText("admin@example.com")).toBeInTheDocument());
    expect(screen.getByText("platform_admin — global")).toBeInTheDocument();
  });

  it("shows a distinct 'no roles' state for a user with none, instead of a blank cell", async () => {
    setup();
    renderWithProviders(<UsersPage />);

    await waitFor(() => expect(screen.getByText("nobody@example.com")).toBeInTheDocument());
    expect(screen.getByText("No roles")).toBeInTheDocument();
  });

  it("resolves a customer-scoped role to the customer's name, not its bare id", async () => {
    vi.mocked(usersApi.list).mockResolvedValue([
      user({ roles: [role({ role: "customer_admin", scope_type: "customer", scope_id: "cust-1" })] }),
    ]);
    vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
    renderWithProviders(<UsersPage />);

    await waitFor(() => expect(screen.getByText("customer_admin — Sample Customer AG")).toBeInTheDocument());
  });
});

describe("UsersPage — set password", () => {
  it("validates and sets a password for an imported user without one", async () => {
    vi.mocked(usersApi.list).mockResolvedValue([user({ has_password: false, email: "imported@example.com", display_name: "Imported" })]);
    vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
    vi.mocked(usersApi.roleAssignments).mockResolvedValue([]);
    vi.mocked(usersApi.setPassword).mockResolvedValue(user({ has_password: true }));
    const u = userEvent.setup();
    renderWithProviders(<UsersPage />);

    await u.click(await screen.findByText("imported@example.com"));
    expect(screen.getByText(/has no local password/)).toBeInTheDocument();

    await u.type(screen.getByLabelText("New password"), "short");
    await u.type(screen.getByLabelText("Confirm password"), "short");
    await u.click(screen.getByRole("button", { name: "Save password" }));
    expect(screen.getByText("The password must be at least 8 characters long.")).toBeInTheDocument();
    expect(usersApi.setPassword).not.toHaveBeenCalled();

    await u.clear(screen.getByLabelText("New password"));
    await u.type(screen.getByLabelText("New password"), "long-enough-1");
    await u.clear(screen.getByLabelText("Confirm password"));
    await u.type(screen.getByLabelText("Confirm password"), "long-enough-2");
    await u.click(screen.getByRole("button", { name: "Save password" }));
    expect(screen.getByText("The two passwords don't match.")).toBeInTheDocument();

    await u.clear(screen.getByLabelText("Confirm password"));
    await u.type(screen.getByLabelText("Confirm password"), "long-enough-1");
    await u.click(screen.getByRole("button", { name: "Save password" }));
    await waitFor(() => expect(usersApi.setPassword).toHaveBeenCalledWith("user-1", "long-enough-1"));
    expect(await screen.findByText("Password saved.")).toBeInTheDocument();
  });

  it("offers no password setting for the break-glass account", async () => {
    vi.mocked(usersApi.list).mockResolvedValue([user({ is_breakglass: true, email: "admin@breakglass.local" })]);
    vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
    vi.mocked(usersApi.roleAssignments).mockResolvedValue([]);
    const u = userEvent.setup();
    renderWithProviders(<UsersPage />);

    await u.click(await screen.findByText("admin@breakglass.local"));
    expect(screen.queryByLabelText("New password")).not.toBeInTheDocument();
  });

  it("shows the MFA state in the list and requires MFA from the user dialog", async () => {
    vi.mocked(usersApi.list).mockResolvedValue([user({ email: "alice@example.com", display_name: "Alice" })]);
    vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
    vi.mocked(usersApi.roleAssignments).mockResolvedValue([]);
    vi.mocked(usersApi.setMfaRequired).mockResolvedValue(user({ mfa_required: true }));
    const u = userEvent.setup();
    renderWithProviders(<UsersPage />);

    expect((await screen.findAllByText("Off")).length).toBeGreaterThan(0);
    await u.click(screen.getByText("alice@example.com"));
    const section = await screen.findByTestId("user-mfa-section");
    expect(within(section).getByRole("button", { name: "Reset MFA" })).toBeDisabled();
    await u.click(within(section).getByLabelText("Require MFA at next login (applies immediately)"));

    await waitFor(() => expect(usersApi.setMfaRequired).toHaveBeenCalledWith("user-1", true));
    expect(await within(section).findByText("MFA is now required for this user from their next sign-in.")).toBeInTheDocument();
    expect(within(section).getByText("Required · setup at next login")).toBeInTheDocument();
  });

  it("resets a user's MFA after confirmation", async () => {
    vi.mocked(usersApi.list).mockResolvedValue([user({ mfa_required: true, mfa_enrolled: true })]);
    vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
    vi.mocked(usersApi.roleAssignments).mockResolvedValue([]);
    vi.mocked(usersApi.resetMfa).mockResolvedValue(user({ mfa_required: true, mfa_enrolled: false }));
    const u = userEvent.setup();
    renderWithProviders(<UsersPage />);

    expect((await screen.findAllByText("Required · active")).length).toBeGreaterThan(0);
    await u.click(screen.getByText("admin@example.com"));
    const section = await screen.findByTestId("user-mfa-section");
    await u.click(within(section).getByRole("button", { name: "Reset MFA" }));
    const dialog = await screen.findByRole("dialog", { name: "Reset this user's MFA?" });
    await u.click(within(dialog).getByRole("button", { name: "Reset MFA" }));

    await waitFor(() => expect(usersApi.resetMfa).toHaveBeenCalledWith("user-1"));
    expect(await within(section).findByText("MFA was reset.")).toBeInTheDocument();
  });

  it("offers no MFA controls for SSO users", async () => {
    vi.mocked(usersApi.list).mockResolvedValue([user({ has_password: false, email: "sso@example.com" })]);
    vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
    vi.mocked(usersApi.roleAssignments).mockResolvedValue([]);
    const u = userEvent.setup();
    renderWithProviders(<UsersPage />);

    await u.click(await screen.findByText("sso@example.com"));
    expect(screen.queryByTestId("user-mfa-section")).not.toBeInTheDocument();
  });

  it("requires MFA for all local users after confirmation", async () => {
    vi.mocked(usersApi.list).mockResolvedValue([user()]);
    vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
    vi.mocked(usersApi.mfaPolicy).mockResolvedValue({ require_mfa_all_local_users: false });
    vi.mocked(usersApi.setMfaPolicy).mockResolvedValue({ require_mfa_all_local_users: true });
    const u = userEvent.setup();
    renderWithProviders(<UsersPage />);

    const panel = within(await screen.findByTestId("mfa-policy"));
    expect(panel.getByText("Require MFA for all local users")).toBeInTheDocument();
    const toggle = panel.getByRole("button", { name: "Change…" });
    await waitFor(() => expect(toggle).toBeEnabled());
    await u.click(toggle);
    const dialog = await screen.findByRole("dialog", { name: "Require MFA for all local users?" });
    await u.click(within(dialog).getByRole("button", { name: "Require MFA" }));
    await waitFor(() => expect(usersApi.setMfaPolicy).toHaveBeenCalledWith(true));
    expect(await panel.findByText("Required for everyone")).toBeInTheDocument();
  });

  it("shows a user covered only by the global policy as required", async () => {
    vi.mocked(usersApi.list).mockResolvedValue([user({ mfa_required: false, mfa_required_effective: true })]);
    vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
    renderWithProviders(<UsersPage />);
    expect((await screen.findAllByText("Required · setup at next login")).length).toBeGreaterThan(0);
  });

  it("shows the per-user checkbox checked and locked when the global policy applies", async () => {
    vi.mocked(usersApi.list).mockResolvedValue([user({ mfa_required: false, mfa_required_effective: true })]);
    vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
    vi.mocked(usersApi.roleAssignments).mockResolvedValue([]);
    const u = userEvent.setup();
    renderWithProviders(<UsersPage />);

    await u.click(await screen.findByText("admin@example.com"));
    const section = await screen.findByTestId("user-mfa-section");
    const checkbox = within(section).getByLabelText("Require MFA at next login (applies immediately)");
    expect(checkbox).toBeChecked();
    expect(checkbox).toBeDisabled();
    expect(within(section).getByText("Required for all local users by the global MFA policy.")).toBeInTheDocument();
  });

  it("allows Reset MFA when only recovery codes or an unconfirmed app exist", async () => {
    vi.mocked(usersApi.list).mockResolvedValue([user({ mfa_enrolled: false, mfa_reset_possible: true })]);
    vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
    vi.mocked(usersApi.roleAssignments).mockResolvedValue([]);
    const u = userEvent.setup();
    renderWithProviders(<UsersPage />);

    await u.click(await screen.findByText("admin@example.com"));
    const section = await screen.findByTestId("user-mfa-section");
    expect(within(section).getByRole("button", { name: "Reset MFA" })).toBeEnabled();
  });

  it("shows an Active/Disabled status per user", async () => {
    vi.mocked(usersApi.list).mockResolvedValue([
      user(),
      user({ id: "user-2", email: "gone@example.com", disabled: true }),
    ]);
    vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
    renderWithProviders(<UsersPage />);
    expect(await screen.findByText("Disabled")).toBeInTheDocument();
    expect(screen.getAllByText("Active").length).toBeGreaterThan(0);
  });
});

describe("UsersPage — role assignments", () => {
  it("asks before removing a role", async () => {
    vi.mocked(usersApi.list).mockResolvedValue([user()]);
    vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
    vi.mocked(usersApi.roleAssignments).mockResolvedValue([role()]);
    vi.mocked(usersApi.unassignRole).mockResolvedValue(undefined);
    const u = userEvent.setup();
    renderWithProviders(<UsersPage />);

    await u.click(await screen.findByText("admin@example.com"));
    await u.click(await screen.findByRole("button", { name: "Delete" }));
    expect(usersApi.unassignRole).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("dialog", { name: "Remove role?" });
    expect(within(dialog).getByText("Remove role platform_admin — global from Admin User?")).toBeInTheDocument();
    await u.click(within(dialog).getByRole("button", { name: "Remove role" }));
    await waitFor(() => expect(usersApi.unassignRole).toHaveBeenCalledWith("user-1", "assignment-1"));
  });
});
