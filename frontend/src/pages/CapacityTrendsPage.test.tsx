import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { CapacityForecastOut, DatastoreOut, DatastoreUsageOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { CapacityTrendsPage } from "./CapacityTrendsPage";

vi.mock("../api/resources", () => ({
  datastoresApi: { list: vi.fn(), usage: vi.fn() },
  customersApi: { list: vi.fn() },
}));

const { datastoresApi, customersApi } = await import("../api/resources");

function datastore(id: string, name: string): DatastoreOut {
  return {
    id,
    pbs_instance_id: "pbs-1",
    name,
    path: null,
    comment: null,
    created_at: "2026-01-01T00:00:00Z",
    customer_id: null,
    role: "primary",
  };
}

function usage(forecast: CapacityForecastOut): DatastoreUsageOut {
  return { latest: null, history: [], daily_change_bytes: null, monthly_change_bytes: null, forecast };
}

describe("CapacityTrendsPage", () => {
  it("shows the forecast per datastore", async () => {
    vi.mocked(customersApi.list).mockResolvedValue([]);
    vi.mocked(datastoresApi.list).mockResolvedValue([
      datastore("ds-soon", "soon"),
      datastore("ds-flat", "flat"),
      datastore("ds-new", "new"),
    ]);
    vi.mocked(datastoresApi.usage).mockImplementation(async (id: string) => {
      if (id === "ds-soon") {
        return usage({
          status: "growing",
          basis_days: 30,
          growth_bytes_per_day: 10 * 1024 ** 3,
          days_until_full: 9.6,
          full_at: "2026-12-05T00:00:00Z",
        });
      }
      if (id === "ds-flat") {
        return usage({ status: "not_growing", basis_days: 30, growth_bytes_per_day: -1024, days_until_full: null, full_at: null });
      }
      return usage({ status: "insufficient_history", basis_days: 3, growth_bytes_per_day: null, days_until_full: null, full_at: null });
    });

    renderWithProviders(<CapacityTrendsPage />);

    await waitFor(() => expect(screen.getByText("10 days")).toBeInTheDocument());
    expect(screen.getByText("+10.0 GB")).toBeInTheDocument();
    expect(screen.getByText("Not growing")).toBeInTheDocument();
    expect(screen.getByText("Not enough history (3/7 days)")).toBeInTheDocument();
  });
});
