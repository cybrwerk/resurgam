import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { PricingPlanOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { PricingPlansPage } from "./PricingPlansPage";

vi.mock("../api/resources", () => ({
  pricingPlansApi: { list: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn() },
  customersApi: { list: vi.fn() },
}));

const { pricingPlansApi, customersApi } = await import("../api/resources");

const plan: PricingPlanOut = {
  id: "p1", customer_id: null, name: "Standard",
  price_per_vm_month: "10.00", price_per_ct_month: "4.00", price_per_host_month: "0.00",
  price_per_gb_month: "0.0500", price_per_s3_gb_month: null, currency: "CHF", created_at: "2026-11-27T00:00:00Z",
};

describe("PricingPlansPage S3 price", () => {
  it("shows an unset S3 price as not charged and saves an entered one", async () => {
    vi.mocked(pricingPlansApi.list).mockResolvedValue([plan]);
    vi.mocked(customersApi.list).mockResolvedValue([]);
    vi.mocked(pricingPlansApi.update).mockResolvedValue({ ...plan, price_per_s3_gb_month: "0.0200" });
    const user = userEvent.setup();
    renderWithProviders(<PricingPlansPage />);

    await waitFor(() => expect(screen.getByText("Not charged")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.type(screen.getByLabelText("Price per GB in S3/month"), "0.02");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(pricingPlansApi.update).toHaveBeenCalledWith("p1", expect.objectContaining({ price_per_s3_gb_month: "0.02" })),
    );
  });

  it("sends null when the S3 price is left empty on create", async () => {
    vi.mocked(pricingPlansApi.list).mockResolvedValue([]);
    vi.mocked(customersApi.list).mockResolvedValue([]);
    vi.mocked(pricingPlansApi.create).mockResolvedValue(plan);
    const user = userEvent.setup();
    renderWithProviders(<PricingPlansPage />);

    await user.click(await screen.findByRole("button", { name: "Create" }));
    await user.type(screen.getByLabelText("Name"), "Standard");
    await user.type(screen.getByLabelText("Price per VM/month"), "10");
    await user.type(screen.getByLabelText("Price per GB/month"), "0.05");
    const createButtons = screen.getAllByRole("button", { name: "Create" });
    await user.click(createButtons[createButtons.length - 1]);

    await waitFor(() =>
      expect(pricingPlansApi.create).toHaveBeenCalledWith(expect.objectContaining({ price_per_s3_gb_month: null })),
    );
  });
});
