import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { DatastoreOut, PbsSyncJobOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { JobsReplicationSyncPage } from "./JobsReplicationSyncPage";

vi.mock("../api/resources", () => ({
  datastoresApi: { list: vi.fn() },
  customersApi: { list: vi.fn() },
  pbsInstancesApi: { list: vi.fn() },
  tasksApi: { list: vi.fn() },
  pbsJobsApi: {
    syncJobs: vi.fn(),
    syncJobCapabilities: vi.fn(),
    deleteSyncJob: vi.fn(),
    updateSyncJob: vi.fn(),
    remotes: vi.fn(),
  },
}));

const { datastoresApi, customersApi, pbsInstancesApi, tasksApi, pbsJobsApi } = await import("../api/resources");

const DATASTORE: DatastoreOut = {
  id: "ds-a", pbs_instance_id: "pbs-1", name: "store-a", path: null, comment: null,
  created_at: "2026-01-01T00:00:00Z", customer_id: "cust-1", role: "primary",
};

function syncJob(overrides: Partial<PbsSyncJobOut> = {}): PbsSyncJobOut {
  return {
    job_id: "s1", local_store: "store-a", remote_store: "main", remote_name: "dr-site", ns: null,
    schedule: "04:00", remove_vanished: false, owner: "root@pam", comment: null, ...overrides,
  };
}

function openDialog(): HTMLElement {
  const dialogs = screen.getAllByRole("dialog");
  return dialogs[dialogs.length - 1];
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE]);
  vi.mocked(customersApi.list).mockResolvedValue([]);
  vi.mocked(pbsInstancesApi.list).mockResolvedValue([]);
  vi.mocked(tasksApi.list).mockResolvedValue([]);
  vi.mocked(pbsJobsApi.remotes).mockResolvedValue([]);
  vi.mocked(pbsJobsApi.syncJobs).mockResolvedValue([syncJob()]);
  vi.mocked(pbsJobsApi.syncJobCapabilities).mockResolvedValue({ can_read: true, can_write: true, missing_write_privileges: [] });
});

describe("JobsReplicationSyncPage", () => {
  it("shows PBS's error inline when deleting a sync job fails", async () => {
    vi.mocked(pbsJobsApi.deleteSyncJob).mockRejectedValue(new ApiError(502, "pbs_write_failed", "HTTP 403: missing Remote.Read"));
    renderWithProviders(<JobsReplicationSyncPage />);

    await waitFor(() => expect(screen.getByText("04:00")).toBeInTheDocument());
    fireEvent.click(within(screen.getByText("04:00").closest("tr")!).getByRole("button", { name: "Delete" }));
    fireEvent.click(within(openDialog()).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("HTTP 403: missing Remote.Read"));
    expect(pbsJobsApi.deleteSyncJob).toHaveBeenCalledWith("pbs-1", "s1");
  });

  it("stops automatic runs by clearing the schedule (PBS has no disable flag for sync jobs)", async () => {
    vi.mocked(pbsJobsApi.updateSyncJob).mockResolvedValue(syncJob({ schedule: null }));
    renderWithProviders(<JobsReplicationSyncPage />);

    await waitFor(() => expect(screen.getByText("04:00")).toBeInTheDocument());
    fireEvent.click(within(screen.getByText("04:00").closest("tr")!).getByRole("button", { name: "Edit" }));
    const form = openDialog();
    expect(within(form).getByText(/PBS can't disable a sync job/)).toBeInTheDocument();
    fireEvent.change(within(form).getByLabelText("Schedule"), { target: { value: "" } });
    fireEvent.change(within(form).getByLabelText("Owner"), { target: { value: "" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(pbsJobsApi.updateSyncJob).toHaveBeenCalledWith("pbs-1", "s1", { schedule: null, owner: null }));
    await waitFor(() => expect(screen.getByText("Sync job on store-a saved.")).toBeInTheDocument());
  });

  it("explains the missing privileges when the token can't edit sync jobs", async () => {
    vi.mocked(pbsJobsApi.syncJobCapabilities).mockResolvedValue({
      can_read: true,
      can_write: false,
      missing_write_privileges: ["Datastore.Backup", "Remote.Audit"],
    });
    renderWithProviders(<JobsReplicationSyncPage />);

    await waitFor(() => expect(screen.getByText("04:00")).toBeInTheDocument());
    const row = screen.getByText("04:00").closest("tr")!;
    await waitFor(() => expect(within(row).getByText(/Requires Datastore.Backup, Remote.Audit/)).toBeInTheDocument());
    expect(within(row).getByRole("button", { name: "Edit" })).toBeDisabled();
    expect(within(row).getByRole("button", { name: "Delete" })).toBeDisabled();
  });
});
