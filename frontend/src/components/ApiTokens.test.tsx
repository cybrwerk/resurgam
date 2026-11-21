import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiTokenCreatedOut, ApiTokenOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { AllApiTokensPanel, MyApiTokensPanel } from "./ApiTokens";

vi.mock("../api/resources", () => ({
  apiTokensApi: {
    mine: vi.fn(),
    grantable: vi.fn(),
    create: vi.fn(),
    all: vi.fn(),
    revoke: vi.fn(),
  },
}));

const { apiTokensApi } = await import("../api/resources");

const CUSTOMER_A = "11111111-1111-1111-1111-111111111111";

function token(overrides: Partial<ApiTokenOut> = {}): ApiTokenOut {
  return {
    id: "tok-1",
    name: "Zabbix",
    token_prefix: "rsg_AbCdEfGh",
    read_only: true,
    grants: [{ role: "viewer", scope_type: "customer", scope_id: CUSTOMER_A, scope_name: "Customer A" }],
    allowed_cidrs: null,
    created_at: "2026-11-01T10:00:00Z",
    expires_at: "2027-11-01T10:00:00Z",
    last_used_at: null,
    last_used_ip: null,
    status: "active",
    user_id: "user-1",
    user_email: "ops@example.com",
    user_display_name: "Ops",
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(apiTokensApi.mine).mockReset();
  vi.mocked(apiTokensApi.create).mockReset();
  vi.mocked(apiTokensApi.revoke).mockReset();
  vi.mocked(apiTokensApi.grantable).mockResolvedValue({
    can_grant_any: true,
    max_lifetime_days: 548,
    options: [
      { role: "viewer", scope_type: "global", scope_id: null, scope_name: null },
      { role: "viewer", scope_type: "customer", scope_id: CUSTOMER_A, scope_name: "Customer A" },
      { role: "restore_operator", scope_type: "customer", scope_id: CUSTOMER_A, scope_name: "Customer A" },
    ],
  });
});

describe("MyApiTokensPanel", () => {
  it("lists the user's tokens with their permissions", async () => {
    vi.mocked(apiTokensApi.mine).mockResolvedValue([
      token({ last_used_at: "2026-11-10T08:00:00Z", last_used_ip: "192.0.2.10", allowed_cidrs: ["192.0.2.0/24"] }),
    ]);
    renderWithProviders(<MyApiTokensPanel isBreakglass={false} />);

    expect(await screen.findByText("Zabbix")).toBeInTheDocument();
    const table = screen.getByRole("table");
    expect(within(table).getByText("rsg_AbCdEfGh…")).toBeInTheDocument();
    expect(within(table).getByText("Read-only")).toBeInTheDocument();
    expect(within(table).getByText("viewer — Customer A")).toBeInTheDocument();
    expect(within(table).getByText("only from 192.0.2.0/24")).toBeInTheDocument();
    expect(within(table).getByText("192.0.2.10")).toBeInTheDocument();
  });

  it("creates a scoped read-only token and shows it once", async () => {
    vi.mocked(apiTokensApi.mine).mockResolvedValue([]);
    const created: ApiTokenCreatedOut = { ...token(), token: "rsg_SECRET-value-shown-once" };
    vi.mocked(apiTokensApi.create).mockResolvedValue(created);
    const user = userEvent.setup();
    renderWithProviders(<MyApiTokensPanel isBreakglass={false} />);

    expect(await screen.findByText("You have no API tokens yet.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create token" }));
    const dialog = await screen.findByRole("dialog", { name: "Create API token" });

    await user.type(within(dialog).getByLabelText("Name"), "Zabbix");
    await user.selectOptions(within(dialog).getByLabelText("Expires after"), "365 days");
    const scope = await within(dialog).findByLabelText("Scope of role 1");
    await user.selectOptions(scope, "Customer A");
    await user.type(within(dialog).getByLabelText("Allowed client addresses (optional)"), "192.0.2.10, 10.0.0.0/24");
    await user.click(within(dialog).getByRole("button", { name: "Create token" }));

    await waitFor(() =>
      expect(apiTokensApi.create).toHaveBeenCalledWith({
        name: "Zabbix",
        expires_in_days: 365,
        read_only: true,
        grants: [{ role: "viewer", scope_type: "customer", scope_id: CUSTOMER_A }],
        allowed_cidrs: ["192.0.2.10", "10.0.0.0/24"],
      }),
    );
    expect(await screen.findByTestId("api-token-secret")).toHaveTextContent("rsg_SECRET-value-shown-once");
  });

  it("offers the 18-month maximum and a custom lifetime capped at it", async () => {
    vi.mocked(apiTokensApi.mine).mockResolvedValue([]);
    const user = userEvent.setup();
    renderWithProviders(<MyApiTokensPanel isBreakglass={false} />);
    await user.click(await screen.findByRole("button", { name: "Create token" }));
    const dialog = await screen.findByRole("dialog", { name: "Create API token" });
    await user.type(within(dialog).getByLabelText("Name"), "x");
    await within(dialog).findByLabelText("Scope of role 1");

    expect(within(dialog).getByRole("option", { name: "548 days (18 months, maximum)" })).toBeInTheDocument();
    await user.selectOptions(within(dialog).getByLabelText("Expires after"), "Custom…");
    const days = within(dialog).getByLabelText("Days (1–548)");
    await user.clear(days);
    await user.type(days, "600");
    expect(within(dialog).getByRole("button", { name: "Create token" })).toBeDisabled();
    await user.clear(days);
    await user.type(days, "500");
    expect(within(dialog).getByRole("button", { name: "Create token" })).toBeEnabled();
  });

  it("sends read & write when chosen and keeps several roles", async () => {
    vi.mocked(apiTokensApi.mine).mockResolvedValue([]);
    vi.mocked(apiTokensApi.create).mockResolvedValue({ ...token(), token: "rsg_x" });
    const user = userEvent.setup();
    renderWithProviders(<MyApiTokensPanel isBreakglass={false} />);
    await user.click(await screen.findByRole("button", { name: "Create token" }));
    const dialog = await screen.findByRole("dialog", { name: "Create API token" });
    await user.type(within(dialog).getByLabelText("Name"), "Automation");
    await within(dialog).findByLabelText("Scope of role 1");
    await user.click(within(dialog).getByLabelText(/^Read & write/));
    await user.click(within(dialog).getByRole("button", { name: "Add role" }));
    await user.selectOptions(within(dialog).getByLabelText("Role 2"), "restore_operator");
    await user.click(within(dialog).getByRole("button", { name: "Create token" }));

    await waitFor(() => expect(apiTokensApi.create).toHaveBeenCalled());
    const body = vi.mocked(apiTokensApi.create).mock.calls[0][0];
    expect(body.read_only).toBe(false);
    expect(body.allowed_cidrs).toBeNull();
    expect(body.grants).toEqual([
      { role: "viewer", scope_type: "global", scope_id: null },
      { role: "restore_operator", scope_type: "customer", scope_id: CUSTOMER_A },
    ]);
  });

  it("revokes a token after confirmation", async () => {
    vi.mocked(apiTokensApi.mine).mockResolvedValue([token()]);
    vi.mocked(apiTokensApi.revoke).mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderWithProviders(<MyApiTokensPanel isBreakglass={false} />);
    await user.click(await screen.findByRole("button", { name: "Revoke" }));
    const dialog = await screen.findByRole("dialog", { name: "Revoke token “Zabbix”?" });
    await user.click(within(dialog).getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(apiTokensApi.revoke).toHaveBeenCalledWith("tok-1"));
  });

  it("explains that break-glass can't create tokens", async () => {
    renderWithProviders(<MyApiTokensPanel isBreakglass />);
    expect(await screen.findByText("The emergency access account can't create API tokens.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create token" })).not.toBeInTheDocument();
    expect(apiTokensApi.mine).not.toHaveBeenCalled();
  });
});

describe("AllApiTokensPanel", () => {
  it("lists every user's tokens with their owner", async () => {
    vi.mocked(apiTokensApi.all).mockResolvedValue([token(), token({ id: "tok-2", name: "Old", status: "expired" })]);
    renderWithProviders(<AllApiTokensPanel />);
    expect(await screen.findByText("Zabbix")).toBeInTheDocument();
    expect(screen.getAllByText("ops@example.com")).toHaveLength(2);
    expect(screen.getByText("Expired")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete" })).toBeInTheDocument();
  });
});
