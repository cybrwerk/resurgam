import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { CustomerBillingOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { BillingPage } from "./BillingPage";

vi.mock("../api/resources", () => ({
  billingApi: { list: vi.fn(), get: vi.fn(), exportCsv: vi.fn() },
}));

const { billingApi } = await import("../api/resources");

function row(overrides: Partial<CustomerBillingOut> = {}): CustomerBillingOut {
  return {
    customer_id: "cust-1", customer_name: "Acme AG", customer_match_code: "ACM",
    vm_count: 3, ct_count: 1, host_count: 0, estimated_physical_bytes: 1024, estimated_s3_bytes: 0,
    physical_bytes_estimate_complete: true, pricing_plan_id: null, pricing_plan_source: null,
    price_per_vm_month: null, price_per_ct_month: null, price_per_host_month: null,
    price_per_gb_month: null, price_per_s3_gb_month: null, currency: null, estimated_monthly_cost: null,
    ...overrides,
  };
}

describe("BillingPage", () => {
  it("renders billing rows", async () => {
    vi.mocked(billingApi.list).mockResolvedValue([row()]);
    renderWithProviders(<BillingPage />);

    await waitFor(() => expect(screen.getByText("Acme AG (ACM)")).toBeInTheDocument());
  });

  it("shows the S3 share and whether the plan charges it", async () => {
    vi.mocked(billingApi.list).mockResolvedValue([
      row({ estimated_physical_bytes: 3_000_000_000, estimated_s3_bytes: 2_000_000_000, pricing_plan_id: "p1", pricing_plan_source: "global" }),
      row({ customer_id: "cust-2", customer_name: "Beta", customer_match_code: "BET", estimated_s3_bytes: 2_000_000_000, pricing_plan_id: "p1", pricing_plan_source: "global", price_per_s3_gb_month: "0.2500" }),
    ]);
    renderWithProviders(<BillingPage />);

    await waitFor(() => expect(screen.getByText(/in S3 \(not charged\)/)).toBeInTheDocument());
    expect(screen.getAllByText(/^incl\. .* in S3/)).toHaveLength(2);
  });

  it("exports a CSV via the export button", async () => {
    vi.mocked(billingApi.list).mockResolvedValue([row()]);
    vi.mocked(billingApi.exportCsv).mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderWithProviders(<BillingPage />);

    await waitFor(() => expect(screen.getByText("Acme AG (ACM)")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Export CSV" }));

    await waitFor(() => expect(billingApi.exportCsv).toHaveBeenCalled());
  });
});
