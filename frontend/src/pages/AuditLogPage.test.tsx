import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { AuditEventOut, UserOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { AuditLogPage } from "./AuditLogPage";

vi.mock("../api/resources", () => ({
  auditApi: { list: vi.fn() },
  usersApi: { list: vi.fn() },
}));

const { auditApi, usersApi } = await import("../api/resources");

const USER: UserOut = {
  id: "user-1",
  email: "alice@example.com",
  display_name: "Alice",
  is_breakglass: false,
  disabled: false,
  has_password: true,
  roles: [],
  created_at: "2026-01-01T00:00:00Z",
  mfa_required: false,
  mfa_enrolled: false,
};

function event(overrides: Partial<AuditEventOut>): AuditEventOut {
  return {
    id: "event-1",
    occurred_at: "2026-01-02T10:00:00Z",
    actor_user_id: "user-1",
    actor_label: null,
    actor_is_breakglass: false,
    action: "customer_created",
    target_type: "customer",
    target_id: "cust-1",
    correlation_id: null,
    detail: null,
    customer_id: null,
    ...overrides,
  };
}

const EVENTS = [
  event({ id: "e1", action: "customer_created", target_id: "cust-1" }),
  event({ id: "e2", action: "pve_cluster_disabled", target_type: "pve_cluster", target_id: "pve-1", actor_user_id: null }),
];

function setup() {
  vi.mocked(usersApi.list).mockResolvedValue([USER]);
  vi.mocked(auditApi.list).mockResolvedValue(EVENTS);
}

describe("AuditLogPage", () => {
  it("shows every event with actors resolved to a display name", async () => {
    setup();
    renderWithProviders(<AuditLogPage />);

    await waitFor(() => expect(screen.getByText("customer_created")).toBeInTheDocument());
    expect(screen.getByText("pve_cluster_disabled")).toBeInTheDocument();
    const table = within(screen.getByRole("table"));
    expect(table.getByText("Alice (alice@example.com)")).toBeInTheDocument();
    expect(table.getByText("System")).toBeInTheDocument();
  });

  it("requests a bounded default limit", async () => {
    setup();
    renderWithProviders(<AuditLogPage />);
    await waitFor(() => expect(auditApi.list).toHaveBeenCalledWith({ limit: 500 }));
  });

  it("filters by action/target/actor as you type", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<AuditLogPage />);

    await waitFor(() => expect(screen.getByText("customer_created")).toBeInTheDocument());
    await user.type(screen.getByLabelText("Search"), "pve_cluster");

    expect(screen.getByText("pve_cluster_disabled")).toBeInTheDocument();
    expect(screen.queryByText("customer_created")).not.toBeInTheDocument();
  });

  it("shows a distinct empty state when the search matches nothing", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<AuditLogPage />);

    await waitFor(() => expect(screen.getByText("customer_created")).toBeInTheDocument());
    await user.type(screen.getByLabelText("Search"), "does-not-exist-anywhere");
    await waitFor(() => expect(screen.getByText("No audit events match your search.")).toBeInTheDocument());
  });

  it("offers Load more only when the server returned a full page, and bumps the limit", async () => {
    setup();
    vi.mocked(auditApi.list).mockResolvedValue(Array.from({ length: 500 }, (_, i) => event({ id: `e${i}` })));
    const user = userEvent.setup();
    renderWithProviders(<AuditLogPage />);

    await waitFor(() => expect(screen.getByText("Showing the most recent 500 events.")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(auditApi.list).toHaveBeenCalledWith({ limit: 1000 }));
  });

  it("does not offer Load more when fewer rows than the limit come back", async () => {
    setup();
    renderWithProviders(<AuditLogPage />);
    await waitFor(() => expect(screen.getByText("customer_created")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("prefers the server-snapshotted actor_label over the live users list, even for a deleted actor", async () => {
    vi.mocked(usersApi.list).mockResolvedValue([USER]);
    vi.mocked(auditApi.list).mockResolvedValue([
      event({ actor_user_id: null, actor_label: "Offboarded Operator (offboarded@example.com)" }),
    ]);
    renderWithProviders(<AuditLogPage />);

    await waitFor(() =>
      expect(screen.getByText("Offboarded Operator (offboarded@example.com)")).toBeInTheDocument(),
    );
    expect(screen.queryByText("System")).not.toBeInTheDocument();
  });

  it("flags a break-glass actor with a distinct badge", async () => {
    vi.mocked(usersApi.list).mockResolvedValue([USER]);
    vi.mocked(auditApi.list).mockResolvedValue([
      event({ actor_label: "Break-glass (admin) (admin@breakglass.local)", actor_is_breakglass: true }),
    ]);
    renderWithProviders(<AuditLogPage />);

    await waitFor(() => expect(screen.getByText("Break-glass (admin) (admin@breakglass.local)")).toBeInTheDocument());
    expect(screen.getByText("Break-glass")).toBeInTheDocument();
  });
});
