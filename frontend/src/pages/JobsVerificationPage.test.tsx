import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { DatastoreOut, PbsVerifyJobOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { JobsVerificationPage } from "./JobsVerificationPage";

vi.mock("../api/resources", () => ({
  datastoresApi: { list: vi.fn() },
  customersApi: { list: vi.fn() },
  pbsInstancesApi: { list: vi.fn() },
  pbsJobsApi: {
    verifyJobs: vi.fn(),
    verifyJobCapabilities: vi.fn(),
    deleteVerifyJob: vi.fn(),
    updateVerifyJob: vi.fn(),
  },
}));

const { datastoresApi, customersApi, pbsJobsApi } = await import("../api/resources");

const DATASTORE: DatastoreOut = {
  id: "ds-a", pbs_instance_id: "pbs-1", name: "store-a", path: null, comment: null,
  created_at: "2026-01-01T00:00:00Z", customer_id: "cust-1", role: "primary",
};

function verifyJob(overrides: Partial<PbsVerifyJobOut>): PbsVerifyJobOut {
  return { id: "v1", store: "store-a", schedule: "sun 10:00", ns: null, outdated_after: 30, ignore_verified: true, comment: null, ...overrides };
}

describe("JobsVerificationPage", () => {
  it("lists verify jobs across datastores in one table", async () => {
    vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE]);
    vi.mocked(customersApi.list).mockResolvedValue([{ id: "cust-1", name: "Acme AG", slug: "acme", debitor_number: "1", match_code: "ACM", disabled: false, created_at: "2026-01-01T00:00:00Z", report_retention_days: null }]);
    vi.mocked(pbsJobsApi.verifyJobCapabilities).mockResolvedValue({ can_read: true, can_write: true });
    vi.mocked(pbsJobsApi.verifyJobs).mockResolvedValue([verifyJob({})]);
    renderWithProviders(<JobsVerificationPage />);

    await waitFor(() => expect(screen.getByText("sun 10:00")).toBeInTheDocument());
    expect(screen.getByText("store-a")).toBeInTheDocument();
    expect(screen.getByText("Acme AG")).toBeInTheDocument();
  });

  it("shows the delete error inline and clears a schedule on edit", async () => {
    vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE]);
    vi.mocked(customersApi.list).mockResolvedValue([]);
    vi.mocked(pbsJobsApi.verifyJobCapabilities).mockResolvedValue({ can_read: true, can_write: true });
    vi.mocked(pbsJobsApi.verifyJobs).mockResolvedValue([verifyJob({})]);
    vi.mocked(pbsJobsApi.deleteVerifyJob).mockRejectedValue(new ApiError(502, "pbs_write_failed", "HTTP 400: job locked"));
    vi.mocked(pbsJobsApi.updateVerifyJob).mockResolvedValue(verifyJob({ schedule: null }));
    renderWithProviders(<JobsVerificationPage />);

    await waitFor(() => expect(screen.getByText("sun 10:00")).toBeInTheDocument());
    const row = screen.getByText("sun 10:00").closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: "Delete" }));
    const dialogs = screen.getAllByRole("dialog");
    fireEvent.click(within(dialogs[dialogs.length - 1]).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("HTTP 400: job locked"));

    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    const forms = screen.getAllByRole("dialog");
    const form = forms[forms.length - 1];
    fireEvent.change(within(form).getByLabelText("Schedule"), { target: { value: "" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(pbsJobsApi.updateVerifyJob).toHaveBeenCalledWith("pbs-1", "v1", { schedule: null }));
  });
});
