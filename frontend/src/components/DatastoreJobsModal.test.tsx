import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { DatastoreOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { DatastoreJobsModal, changedFields } from "./DatastoreJobsModal";

vi.mock("../api/resources", () => ({
  datastoresApi: { list: vi.fn() },
  pbsInstancesApi: { getGcSchedule: vi.fn(), setGcSchedule: vi.fn() },
  pbsJobsApi: {
    pruneJobs: vi.fn(),
    pruneJobCapabilities: vi.fn(),
    deletePruneJob: vi.fn(),
    verifyJobs: vi.fn(),
    verifyJobCapabilities: vi.fn(),
    syncJobs: vi.fn(),
    syncJobCapabilities: vi.fn(),
    gcScheduleCapabilities: vi.fn(),
    remotes: vi.fn(),
  },
}));

const { datastoresApi, pbsInstancesApi, pbsJobsApi } = await import("../api/resources");

const DATASTORE: DatastoreOut = {
  id: "ds-a", pbs_instance_id: "pbs-1", name: "store-a", path: null, comment: null,
  created_at: "2026-01-01T00:00:00Z", customer_id: "cust-1", role: "primary",
};

describe("changedFields", () => {
  it("keeps only changed fields, emptied ones as null", () => {
    expect(
      changedFields(
        { schedule: "21:00", comment: "old", ns: null, keep_daily: 7 as number | null },
        { schedule: "21:00", comment: "", ns: "", keep_daily: 14 },
      ),
    ).toEqual({ comment: null, keep_daily: 14 });
  });

  it("treats whitespace-only edits of an empty field as unchanged", () => {
    expect(changedFields({ comment: null as string | null }, { comment: "  " })).toEqual({});
  });
});

describe("DatastoreJobsModal", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    const caps = { can_read: true, can_write: true, missing_write_privileges: [] };
    vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE]);
    vi.mocked(pbsJobsApi.pruneJobCapabilities).mockResolvedValue(caps);
    vi.mocked(pbsJobsApi.verifyJobCapabilities).mockResolvedValue(caps);
    vi.mocked(pbsJobsApi.syncJobCapabilities).mockResolvedValue(caps);
    vi.mocked(pbsJobsApi.gcScheduleCapabilities).mockResolvedValue(caps);
    vi.mocked(pbsJobsApi.verifyJobs).mockResolvedValue([]);
    vi.mocked(pbsJobsApi.syncJobs).mockResolvedValue([]);
    vi.mocked(pbsInstancesApi.getGcSchedule).mockResolvedValue({ gc_schedule: null });
    vi.mocked(pbsJobsApi.pruneJobs).mockResolvedValue([
      {
        id: "p1", store: "store-a", schedule: "21:00", ns: null, keep_last: null, keep_hourly: null,
        keep_daily: 7, keep_weekly: null, keep_monthly: null, keep_yearly: null, disable: false, comment: null,
      },
    ]);
  });

  it("shows a prune-job delete error inside the modal", async () => {
    vi.mocked(pbsJobsApi.deletePruneJob).mockRejectedValue(new ApiError(502, "pbs_write_failed", "HTTP 403: forbidden"));
    renderWithProviders(<DatastoreJobsModal datastore={DATASTORE} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText("21:00")).toBeInTheDocument());
    fireEvent.click(within(screen.getByText("21:00").closest("tr")!).getByRole("button", { name: "Delete" }));
    const dialogs = screen.getAllByRole("dialog");
    fireEvent.click(within(dialogs[dialogs.length - 1]).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("HTTP 403: forbidden"));
  });

  it("disables GC Remove when no schedule is set", async () => {
    renderWithProviders(<DatastoreJobsModal datastore={DATASTORE} onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText("Not scheduled (manual only)")).toBeInTheDocument());
    const row = screen.getByText("Not scheduled (manual only)").closest("tr")!;
    await waitFor(() => expect(within(row).getByRole("button", { name: "Edit" })).toBeEnabled());
    expect(within(row).getByRole("button", { name: "Remove" })).toBeDisabled();
  });
});
