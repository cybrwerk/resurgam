import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { CustomerOut, WhoAmIResponse } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { GlobalSearch } from "./GlobalSearch";

vi.mock("../api/resources", () => ({
  authApi: { whoami: vi.fn() },
  customersApi: { list: vi.fn() },
}));

const { authApi, customersApi } = await import("../api/resources");

const CUSTOMER_A: CustomerOut = {
  id: "cust-a", name: "Acme AG", slug: "acme", debitor_number: "1000", match_code: "ACM",
  disabled: false, created_at: "2026-01-01T00:00:00Z", report_retention_days: null,
};

function whoami(overrides: Partial<WhoAmIResponse> = {}): WhoAmIResponse {
  return {
    user_id: "u1", email: "a@example.com", display_name: "A", is_breakglass: false,
    assignments: [{ role: "platform_admin", scope_type: "global", scope_id: null }],
    ...overrides,
  };
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

async function renderSearch(user: WhoAmIResponse = whoami()) {
  window.localStorage.setItem("resurgam.token", "fake-token");
  vi.mocked(authApi.whoami).mockResolvedValue(user);
  vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER_A]);
  const result = renderWithProviders(
    <>
      <GlobalSearch />
      <LocationProbe />
    </>,
  );
  await waitFor(() => expect(screen.getByText("Search…")).toBeInTheDocument());
  return result;
}

describe("GlobalSearch", () => {
  it("opens via the trigger button", async () => {
    const user = userEvent.setup();
    await renderSearch();

    await user.click(screen.getByText("Search…"));
    expect(screen.getByPlaceholderText("Search pages or tenants…")).toBeInTheDocument();
  });

  it("closes after a result is chosen", async () => {
    // jsdom's <dialog> polyfill doesn't apply the real UA stylesheet
    // (dialog:not([open]) { display: none }), so a presence/visibility
    // query on its contents can't tell open from closed here -- the
    // `open` attribute itself is the one thing jsdom keeps accurate.
    const user = userEvent.setup();
    const { container } = await renderSearch();

    await user.click(screen.getByText("Search…"));
    await user.type(screen.getByPlaceholderText("Search pages or tenants…"), "Acme");
    await waitFor(() => expect(screen.getByText("Acme AG")).toBeInTheDocument());
    await user.click(screen.getByText("Acme AG"));

    await waitFor(() => expect(container.querySelector("dialog")).not.toHaveAttribute("open"));
  });

  it("opens via Ctrl+K from anywhere on the page", async () => {
    const user = userEvent.setup();
    await renderSearch();

    await user.keyboard("{Control>}k{/Control}");
    await waitFor(() => expect(screen.getByPlaceholderText("Search pages or tenants…")).toBeInTheDocument());
  });

  it("filters results by page name and navigates to it on Enter", async () => {
    const user = userEvent.setup();
    await renderSearch();

    await user.click(screen.getByText("Search…"));
    await user.type(screen.getByPlaceholderText("Search pages or tenants…"), "Recovery Lab");

    await waitFor(() => expect(screen.getByText("Recovery Lab")).toBeInTheDocument());
    expect(screen.queryByText("Virtual Machines")).not.toBeInTheDocument();

    await user.keyboard("{Enter}");
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/recovery/recovery-lab"));
  });

  it("includes customers by name and navigates to their panel", async () => {
    const user = userEvent.setup();
    await renderSearch();

    await user.click(screen.getByText("Search…"));
    await user.type(screen.getByPlaceholderText("Search pages or tenants…"), "Acme");

    await waitFor(() => expect(screen.getByText("Acme AG")).toBeInTheDocument());
    await user.click(screen.getByText("Acme AG"));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/customers/cust-a/panel"));
  });

  it("hides platform_admin-only pages for a user without that role", async () => {
    const user = userEvent.setup();
    await renderSearch(whoami({ assignments: [{ role: "restore_operator", scope_type: "global", scope_id: null }] }));

    await user.click(screen.getByText("Search…"));
    await user.type(screen.getByPlaceholderText("Search pages or tenants…"), "Credentials");

    await waitFor(() => expect(screen.getByText("No results.")).toBeInTheDocument());
  });

  it("moves the active result with arrow keys before confirming with Enter", async () => {
    const user = userEvent.setup();
    await renderSearch();

    await user.click(screen.getByText("Search…"));
    await user.type(screen.getByPlaceholderText("Search pages or tenants…"), "Backups");
    await waitFor(() => expect(screen.getByText("Virtual Machines")).toBeInTheDocument());

    // First result (Virtual Machines) is active by default; arrow down
    // twice lands on the third (Hosts).
    await user.keyboard("{ArrowDown}{ArrowDown}{Enter}");
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/backups/hosts"));
  });
});
