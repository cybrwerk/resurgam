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

  it("shows bucket size, cache usage and traffic for an S3 datastore", async () => {
    vi.mocked(customersApi.list).mockResolvedValue([]);
    vi.mocked(datastoresApi.list).mockResolvedValue([
      { ...datastore("ds-s3", "s3-test"), backend_type: "s3", s3_bucket: "pbs-test", s3_endpoint_host: "127.0.0.1" },
    ]);
    vi.mocked(datastoresApi.usage).mockResolvedValue({
      ...usage({ status: "not_applicable", basis_days: 0, growth_bytes_per_day: null, days_until_full: null, full_at: null }),
      backend_type: "s3",
      latest: {
        id: "m-1",
        datastore_id: "ds-s3",
        captured_at: "2026-11-27T10:00:00Z",
        used_bytes: 36 * 1024 ** 3,
        available_bytes: 7 * 1024 ** 3,
        total_bytes: 45 * 1024 ** 3,
        logical_bytes: 0,
        usage_percent: 78.8,
        group_count_vm: 0,
        group_count_ct: 1,
        group_count_host: 0,
        snapshot_count: 1,
        oldest_snapshot_at: null,
        newest_snapshot_at: null,
        last_successful_gc_at: null,
        s3_data_bytes: null,
      },
      s3_traffic_30d: {
        s3_get_requests: 1,
        s3_put_requests: 81,
        s3_post_requests: 0,
        s3_head_requests: 2,
        s3_delete_requests: 0,
        s3_uploaded_bytes: 80 * 1024 ** 2,
        s3_downloaded_bytes: 0,
      },
    });

    renderWithProviders(<CapacityTrendsPage />);

    await waitFor(() => expect(screen.getByText("No fixed size (S3)")).toBeInTheDocument());
    expect(screen.getByText("pbs-test @ 127.0.0.1")).toBeInTheDocument();
    expect(screen.getByText("Bucket size known after the first garbage collection")).toBeInTheDocument();
    expect(screen.getByText("Cache 78.8% (36.0 GB / 45.0 GB)")).toBeInTheDocument();
    expect(screen.getByText("30 d: ↑ 80.0 MB · ↓ 0 B · 84 requests")).toBeInTheDocument();
  });
});
