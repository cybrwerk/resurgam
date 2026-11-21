import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { useLocation } from "react-router-dom";

import type { CustomerOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { CustomerSwitcher } from "./CustomerSwitcher";

vi.mock("../api/resources", () => ({
  customersApi: { list: vi.fn() },
}));

const { customersApi } = await import("../api/resources");

const CUSTOMER_A: CustomerOut = {
  id: "cust-a", name: "Acme AG", slug: "acme", debitor_number: "1000", match_code: "ACM",
  disabled: false, created_at: "2026-01-01T00:00:00Z", report_retention_days: null,
};
const CUSTOMER_B: CustomerOut = { ...CUSTOMER_A, id: "cust-b", name: "Beta GmbH", slug: "beta" };

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

function renderSwitcher() {
  return renderWithProviders(
    <>
      <CustomerSwitcher />
      <LocationProbe />
    </>,
  );
}

describe("CustomerSwitcher", () => {
  it("lists 'All tenants' plus every real customer", async () => {
    vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER_A, CUSTOMER_B]);
    renderSwitcher();

    await waitFor(() => expect(screen.getByText("Acme AG")).toBeInTheDocument());
    expect(screen.getByText("Beta GmbH")).toBeInTheDocument();
    expect(screen.getByText("All tenants")).toBeInTheDocument();
  });

  it("navigates to that customer's panel when one is picked, and back to Overview for 'All tenants'", async () => {
    vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER_A, CUSTOMER_B]);
    const user = userEvent.setup();
    renderSwitcher();

    await waitFor(() => expect(screen.getByText("Acme AG")).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Tenant"), "cust-a");
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/customers/cust-a/panel"));

    await user.selectOptions(screen.getByLabelText("Tenant"), "");
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/"));
  });

  it("remembers the selection across a remount (persisted per-viewer)", async () => {
    vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER_A, CUSTOMER_B]);
    const user = userEvent.setup();
    const { unmount } = renderSwitcher();

    await waitFor(() => expect(screen.getByText("Acme AG")).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Tenant"), "cust-b");
    unmount();

    renderSwitcher();
    await waitFor(() => expect((screen.getByLabelText("Tenant") as HTMLSelectElement).value).toBe("cust-b"));
  });
});
