import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { DatastoreOut, PbsPruneJobOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { JobsPruneGcPage } from "./JobsPruneGcPage";

vi.mock("../api/resources", () => ({
  datastoresApi: { list: vi.fn() },
  customersApi: { list: vi.fn() },
  pbsInstancesApi: { getGcSchedule: vi.fn(), setGcSchedule: vi.fn() },
  pbsJobsApi: {
    pruneJobs: vi.fn(),
    pruneJobCapabilities: vi.fn(),
    gcScheduleCapabilities: vi.fn(),
    deletePruneJob: vi.fn(),
    updatePruneJob: vi.fn(),
  },
  pveClustersApi: { list: vi.fn() },
}));

const { datastoresApi, customersApi, pbsInstancesApi, pbsJobsApi } = await import("../api/resources");

const DATASTORE_A: DatastoreOut = {
  id: "ds-a", pbs_instance_id: "pbs-1", name: "store-a", path: null, comment: null,
  created_at: "2026-01-01T00:00:00Z", customer_id: "cust-1", role: "primary",
};
const DATASTORE_B: DatastoreOut = { ...DATASTORE_A, id: "ds-b", name: "store-b", customer_id: "cust-2" };

function pruneJob(overrides: Partial<PbsPruneJobOut>): PbsPruneJobOut {
  return {
    id: "p1", store: "store-a", schedule: "21:00", ns: null, keep_last: null, keep_hourly: null,
    keep_daily: 7, keep_weekly: null, keep_monthly: null, keep_yearly: null, disable: false, comment: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(customersApi.list).mockResolvedValue([]);
  vi.mocked(pbsJobsApi.gcScheduleCapabilities).mockResolvedValue({ can_read: true, can_write: true, missing_write_privileges: [] });
});

function openDialog(): HTMLElement {
  const dialogs = screen.getAllByRole("dialog");
  return dialogs[dialogs.length - 1];
}

describe("JobsPruneGcPage", () => {
  it("lists prune jobs from every datastore, filtered by the selected tenant", async () => {
    vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE_A, DATASTORE_B]);
    vi.mocked(customersApi.list).mockResolvedValue([]);
    vi.mocked(pbsJobsApi.pruneJobCapabilities).mockResolvedValue({ can_read: true, can_write: true });
    vi.mocked(pbsInstancesApi.getGcSchedule).mockResolvedValue({ gc_schedule: null });
    vi.mocked(pbsJobsApi.pruneJobs).mockImplementation((_instanceId, datastoreName) =>
      Promise.resolve(datastoreName === "store-a" ? [pruneJob({ id: "p-a", schedule: "21:00" })] : [pruneJob({ id: "p-b", schedule: "12:00" })]),
    );
    window.localStorage.setItem("resurgam.selectedCustomerId", "cust-1");
    renderWithProviders(<JobsPruneGcPage />);

    await waitFor(() => expect(screen.getByText("21:00")).toBeInTheDocument());
    expect(screen.queryByText("12:00")).not.toBeInTheDocument();
    window.localStorage.removeItem("resurgam.selectedCustomerId");
  });

  it("shows PBS's error inline when deleting a prune job fails", async () => {
    vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE_A]);
    vi.mocked(pbsJobsApi.pruneJobCapabilities).mockResolvedValue({ can_read: true, can_write: true });
    vi.mocked(pbsInstancesApi.getGcSchedule).mockResolvedValue({ gc_schedule: null });
    vi.mocked(pbsJobsApi.pruneJobs).mockResolvedValue([pruneJob({})]);
    vi.mocked(pbsJobsApi.deletePruneJob).mockRejectedValue(new ApiError(502, "pbs_write_failed", "HTTP 403: permission check failed"));
    renderWithProviders(<JobsPruneGcPage />);

    await waitFor(() => expect(screen.getByText("21:00")).toBeInTheDocument());
    const row = screen.getByText("21:00").closest("tr")!;
    fireEvent.click(within(row).getByRole("button", { name: "Delete" }));
    fireEvent.click(within(openDialog()).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("HTTP 403: permission check failed"));
    expect(pbsJobsApi.deletePruneJob).toHaveBeenCalledWith("pbs-1", "p1");
  });

  it("confirms a successful prune job delete", async () => {
    vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE_A]);
    vi.mocked(pbsJobsApi.pruneJobCapabilities).mockResolvedValue({ can_read: true, can_write: true });
    vi.mocked(pbsInstancesApi.getGcSchedule).mockResolvedValue({ gc_schedule: null });
    vi.mocked(pbsJobsApi.pruneJobs).mockResolvedValue([pruneJob({})]);
    vi.mocked(pbsJobsApi.deletePruneJob).mockResolvedValue(undefined);
    renderWithProviders(<JobsPruneGcPage />);

    await waitFor(() => expect(screen.getByText("21:00")).toBeInTheDocument());
    fireEvent.click(within(screen.getByText("21:00").closest("tr")!).getByRole("button", { name: "Delete" }));
    fireEvent.click(within(openDialog()).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(screen.getByText("Job on store-a deleted.")).toBeInTheDocument());
  });

  it("explains which PBS privilege is missing when Edit/Delete are disabled", async () => {
    vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE_A]);
    vi.mocked(pbsJobsApi.pruneJobCapabilities).mockResolvedValue({
      can_read: true,
      can_write: false,
      missing_write_privileges: ["Datastore.Modify"],
    });
    vi.mocked(pbsInstancesApi.getGcSchedule).mockResolvedValue({ gc_schedule: null });
    vi.mocked(pbsJobsApi.pruneJobs).mockResolvedValue([pruneJob({})]);
    renderWithProviders(<JobsPruneGcPage />);

    await waitFor(() => expect(screen.getByText("21:00")).toBeInTheDocument());
    const row = screen.getByText("21:00").closest("tr")!;
    await waitFor(() => expect(within(row).getByRole("button", { name: "Edit" })).toBeDisabled());
    expect(within(row).getByRole("button", { name: "Delete" })).toBeDisabled();
    await waitFor(() => expect(within(row).getByText(/Requires Datastore.Modify on the PBS token/)).toBeInTheDocument());
  });

  it("clears an emptied field on edit by sending null, and only for changed fields", async () => {
    vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE_A]);
    vi.mocked(pbsJobsApi.pruneJobCapabilities).mockResolvedValue({ can_read: true, can_write: true });
    vi.mocked(pbsInstancesApi.getGcSchedule).mockResolvedValue({ gc_schedule: null });
    vi.mocked(pbsJobsApi.pruneJobs).mockResolvedValue([pruneJob({ keep_weekly: 4, comment: "old" })]);
    vi.mocked(pbsJobsApi.updatePruneJob).mockResolvedValue(pruneJob({}));
    renderWithProviders(<JobsPruneGcPage />);

    await waitFor(() => expect(screen.getByText("21:00")).toBeInTheDocument());
    fireEvent.click(within(screen.getByText("21:00").closest("tr")!).getByRole("button", { name: "Edit" }));
    const form = openDialog();
    fireEvent.change(within(form).getByLabelText("Keep weekly"), { target: { value: "" } });
    fireEvent.change(within(form).getByLabelText("Comment"), { target: { value: "" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(pbsJobsApi.updatePruneJob).toHaveBeenCalledWith("pbs-1", "p1", { keep_weekly: null, comment: null }));
    await waitFor(() => expect(screen.getByText("Prune job on store-a saved.")).toBeInTheDocument());
  });

  it("edits the GC schedule through a validated form", async () => {
    vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE_A]);
    vi.mocked(pbsJobsApi.pruneJobCapabilities).mockResolvedValue({ can_read: true, can_write: true });
    vi.mocked(pbsJobsApi.pruneJobs).mockResolvedValue([]);
    vi.mocked(pbsInstancesApi.getGcSchedule).mockResolvedValue({ gc_schedule: "daily" });
    vi.mocked(pbsInstancesApi.setGcSchedule).mockResolvedValue({ gc_schedule: "sat 03:00" });
    renderWithProviders(<JobsPruneGcPage />);

    await waitFor(() => expect(screen.getByText("daily")).toBeInTheDocument());
    const row = screen.getByText("daily").closest("tr")!;
    await waitFor(() => expect(within(row).getByRole("button", { name: "Edit" })).toBeEnabled());
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    const form = openDialog();
    fireEvent.change(within(form).getByLabelText("GC schedule"), { target: { value: "  " } });
    fireEvent.submit(within(form).getByLabelText("GC schedule").closest("form")!);
    expect(within(form).getByRole("alert")).toHaveTextContent(/Enter a schedule/);
    expect(pbsInstancesApi.setGcSchedule).not.toHaveBeenCalled();

    fireEvent.change(within(form).getByLabelText("GC schedule"), { target: { value: "sat 03:00" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(pbsInstancesApi.setGcSchedule).toHaveBeenCalledWith("pbs-1", "store-a", { gc_schedule: "sat 03:00" }));
    await waitFor(() => expect(screen.getByText("Garbage-collection schedule for store-a saved.")).toBeInTheDocument());
  });

  it("removes the GC schedule after an in-page confirmation and reports failures", async () => {
    vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE_A]);
    vi.mocked(pbsJobsApi.pruneJobCapabilities).mockResolvedValue({ can_read: true, can_write: true });
    vi.mocked(pbsJobsApi.pruneJobs).mockResolvedValue([]);
    vi.mocked(pbsInstancesApi.getGcSchedule).mockResolvedValue({ gc_schedule: "daily" });
    vi.mocked(pbsInstancesApi.setGcSchedule).mockRejectedValueOnce(new ApiError(502, "pbs_write_failed", "HTTP 500: datastore locked"));
    renderWithProviders(<JobsPruneGcPage />);

    await waitFor(() => expect(screen.getByText("daily")).toBeInTheDocument());
    const row = screen.getByText("daily").closest("tr")!;
    await waitFor(() => expect(within(row).getByRole("button", { name: "Remove" })).toBeEnabled());
    fireEvent.click(within(row).getByRole("button", { name: "Remove" }));
    fireEvent.click(within(openDialog()).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("HTTP 500: datastore locked"));
    expect(pbsInstancesApi.setGcSchedule).toHaveBeenCalledWith("pbs-1", "store-a", { gc_schedule: null });

    vi.mocked(pbsInstancesApi.setGcSchedule).mockResolvedValueOnce({ gc_schedule: null });
    fireEvent.click(within(row).getByRole("button", { name: "Remove" }));
    fireEvent.click(within(openDialog()).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(screen.getByText(/Garbage-collection schedule for store-a removed/)).toBeInTheDocument());
  });

  it("gates GC editing on the token's Datastore.Modify privilege", async () => {
    vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE_A]);
    vi.mocked(pbsJobsApi.pruneJobCapabilities).mockResolvedValue({ can_read: true, can_write: true });
    vi.mocked(pbsJobsApi.pruneJobs).mockResolvedValue([]);
    vi.mocked(pbsInstancesApi.getGcSchedule).mockResolvedValue({ gc_schedule: "daily" });
    vi.mocked(pbsJobsApi.gcScheduleCapabilities).mockResolvedValue({
      can_read: true,
      can_write: false,
      missing_write_privileges: ["Datastore.Modify"],
    });
    renderWithProviders(<JobsPruneGcPage />);

    await waitFor(() => expect(screen.getByText("daily")).toBeInTheDocument());
    const row = screen.getByText("daily").closest("tr")!;
    await waitFor(() => expect(within(row).getByText(/Requires Datastore.Modify/)).toBeInTheDocument());
    expect(within(row).getByRole("button", { name: "Edit" })).toBeDisabled();
    expect(within(row).getByRole("button", { name: "Remove" })).toBeDisabled();
  });
});
