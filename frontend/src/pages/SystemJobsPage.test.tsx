import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { FileIndexJobOut, FileRestoreJobOut, RestorePlanOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { SystemJobsPage } from "./SystemJobsPage";

vi.mock("../api/resources", () => ({
  fileRestoreApi: { listJobs: vi.fn(), cancelJob: vi.fn() },
  fileIndexApi: { listJobs: vi.fn(), cancelJob: vi.fn() },
  restorePlansApi: { list: vi.fn(), abortExecution: vi.fn() },
}));

const { fileRestoreApi, fileIndexApi, restorePlansApi } = await import("../api/resources");

function fileRestoreJob(overrides: Partial<FileRestoreJobOut> = {}): FileRestoreJobOut {
  return {
    id: "fr-job-1", job_type: "list", status: "pending", workload_label: "Office-PC (116)", path: "/etc",
    result: null, result_filename: null, error_code: null, error_message: null,
    expires_at: "2026-01-01T00:10:00Z", started_at: null, completed_at: null, created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function fileIndexJob(overrides: Partial<FileIndexJobOut> = {}): FileIndexJobOut {
  return {
    id: "fi-job-1", status: "running", trigger: "manual", file_index_method: "external_mount",
    guest_exec_target_vmid: null, guest_exec_target_node: null, backup_group_id: "group-1",
    workload_label: "Office-PC (116)", backup_snapshot_id: "snap-1",
    entries_indexed: 5, current_path: "/etc", cancel_requested: false, error_code: null, error_message: null,
    paused_at: null, started_at: "2026-01-01T00:00:00Z", completed_at: null, created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function restorePlan(overrides: Partial<RestorePlanOut> = {}): RestorePlanOut {
  return {
    id: "plan-1", backup_snapshot_id: "snap-1", backup_group_id: "group-1", customer_id: "cust-1",
    pve_cluster_id: "pve-1", target_node: "pve1", target_storage: "local-lvm", vmid_intent: "new", target_vmid: 200,
    temp_name: "restore-200", network_mode: "none", network_bridge: null, start_after_restore: false,
    network_danger_acknowledged: false, checks_result: [{ check: "vmid", status: "pass", message: "VMID 200 is free." }],
    checks_performed_at: "2026-01-01T00:00:00Z", generated_command: "qmrestore ... 200 --storage local-lvm",
    created_by_user_id: "user-1", created_at: "2026-01-01T00:00:00Z",
    execution_status: null, upid: null, executed_at: null, execution_started_by_user_id: null,
    execution_exit_status: null, execution_completed_at: null, execution_error: null, execution_last_confirmed_at: null,
    approval_status: null, approved_by_user_id: null, approved_at: null, approval_notes: null,
    ...overrides,
  };
}

function setup() {
  vi.mocked(fileRestoreApi.listJobs).mockResolvedValue([fileRestoreJob()]);
  vi.mocked(fileIndexApi.listJobs).mockResolvedValue([fileIndexJob()]);
  vi.mocked(restorePlansApi.list).mockResolvedValue([restorePlan()]);
}

describe("SystemJobsPage", () => {
  it("renders all three job sections as tabs", async () => {
    setup();
    renderWithProviders(<SystemJobsPage />);

    await waitFor(() => expect(screen.getByRole("tab", { name: "File Restore Jobs" })).toBeInTheDocument());
    expect(screen.getByRole("tab", { name: "File Index Jobs" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Restore Plan Executions" })).toBeInTheDocument();
  });

  it("shows which workload each job is for, not just its status/path", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<SystemJobsPage />);

    await waitFor(() => expect(within(screen.getByTestId("file-restore-jobs")).getByText("Office-PC (116)")).toBeInTheDocument());
    await user.click(screen.getByRole("tab", { name: "File Index Jobs" }));
    await waitFor(() => expect(within(screen.getByTestId("file-index-jobs")).getByText("Office-PC (116)")).toBeInTheDocument());
  });

  it("cancels a pending File Restore job", async () => {
    setup();
    vi.mocked(fileRestoreApi.cancelJob).mockResolvedValue(fileRestoreJob({ status: "cancelled" }));
    const user = userEvent.setup();
    renderWithProviders(<SystemJobsPage />);

    await waitFor(() => expect(screen.getByText("/etc")).toBeInTheDocument());
    const fileRestoreSection = screen.getByTestId("file-restore-jobs");
    await user.click(within(fileRestoreSection).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(fileRestoreApi.cancelJob).toHaveBeenCalledWith("fr-job-1"));
  });

  it("offers Cancel for a running File Restore job too (the worker stops it at its next check)", async () => {
    setup();
    vi.mocked(fileRestoreApi.listJobs).mockResolvedValue([fileRestoreJob({ status: "running" })]);
    renderWithProviders(<SystemJobsPage />);

    expect(await screen.findByRole("button", { name: "Cancel" })).toBeEnabled();
  });

  it("shows File Restore job details in a modal", async () => {
    setup();
    vi.mocked(fileRestoreApi.listJobs).mockResolvedValue([
      fileRestoreJob({ status: "failed", error_code: "file_restore_failed", error_message: "boom" }),
    ]);
    const user = userEvent.setup();
    renderWithProviders(<SystemJobsPage />);

    await waitFor(() => expect(screen.getByText("/etc")).toBeInTheDocument());
    await user.click(screen.getAllByRole("button", { name: "Details" })[0]);
    await waitFor(() => expect(screen.getByText("boom")).toBeInTheDocument());
    // A real bug found live: the modal used to show the raw backend
    // status string ("failed") even though the list row right above it
    // already renders a humanized "Failed" badge for the same job.
    expect(screen.getByRole("dialog")).toHaveTextContent("Failed");
    expect(screen.queryByText("failed", { selector: "dd" })).not.toBeInTheDocument();
  });

  it("cancels a File Index job through the confirm dialog", async () => {
    setup();
    vi.mocked(fileIndexApi.cancelJob).mockResolvedValue(fileIndexJob({ status: "cancelled" }));
    const user = userEvent.setup();
    renderWithProviders(<SystemJobsPage />, { route: "/?tab=file-index" });

    await waitFor(() => expect(screen.getByText("Manual")).toBeInTheDocument());
    const fileIndexSection = screen.getByTestId("file-index-jobs");
    await user.click(within(fileIndexSection).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.getByText("Cancel this index job?")).toBeInTheDocument());
    // The dialog's own confirm button reuses the "Cancel" label too --
    // it's the last "Cancel"-named button once the dialog is open.
    const allCancelButtons = screen.getAllByRole("button", { name: "Cancel" });
    await user.click(allCancelButtons[allCancelButtons.length - 1]);
    await waitFor(() => expect(fileIndexApi.cancelJob).toHaveBeenCalledWith("fi-job-1"));
  });

  it("shows File Index job details with the same humanized status/trigger as the list row", async () => {
    setup();
    vi.mocked(fileIndexApi.listJobs).mockResolvedValue([
      fileIndexJob({ status: "failed", trigger: "scheduled", error_code: "worker_heartbeat_timeout", error_message: "boom" }),
    ]);
    const user = userEvent.setup();
    renderWithProviders(<SystemJobsPage />, { route: "/?tab=file-index" });

    const fileIndexSection = screen.getByTestId("file-index-jobs");
    await waitFor(() => expect(within(fileIndexSection).getByText("Scheduled")).toBeInTheDocument());
    await user.click(within(fileIndexSection).getByRole("button", { name: "Details" }));

    await waitFor(() => expect(screen.getByText("boom")).toBeInTheDocument());
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Failed");
    expect(dialog).toHaveTextContent("Scheduled");
    expect(within(dialog).queryByText("failed", { selector: "dd" })).not.toBeInTheDocument();
    expect(within(dialog).queryByText("scheduled", { selector: "dd" })).not.toBeInTheDocument();
  });

  it("does not list a restore plan that has never been executed", async () => {
    setup();
    renderWithProviders(<SystemJobsPage />, { route: "/?tab=restore-plans" });

    await waitFor(() => expect(screen.getByTestId("restore-plan-executions")).toHaveTextContent("Nothing here yet"));
    expect(screen.queryByText("200")).not.toBeInTheDocument();
  });

  it("offers Abort only for an in-flight restore execution, gated behind a typed VMID confirmation", async () => {
    setup();
    vi.mocked(restorePlansApi.list).mockResolvedValue([restorePlan({ execution_status: "restore_running", upid: "UPID:x" })]);
    vi.mocked(restorePlansApi.abortExecution).mockResolvedValue(restorePlan({ execution_status: "restore_failed" }));
    const user = userEvent.setup();
    renderWithProviders(<SystemJobsPage />, { route: "/?tab=restore-plans" });

    await waitFor(() => expect(screen.getByText("200")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Abort execution" }));

    await waitFor(() => expect(screen.getByText("Abort this restore execution?")).toBeInTheDocument());
    // The dialog reuses "Abort execution" as its own confirm button label,
    // so after opening there are two -- the second is the actual confirm.
    const buttons = screen.getAllByRole("button", { name: "Abort execution" });
    const submitButton = buttons[buttons.length - 1];
    expect(submitButton).toBeDisabled();

    await user.type(screen.getByLabelText("Type the target VMID (200) to confirm"), "999");
    expect(submitButton).toBeDisabled();

    await user.clear(screen.getByLabelText("Type the target VMID (200) to confirm"));
    await user.type(screen.getByLabelText("Type the target VMID (200) to confirm"), "200");
    expect(submitButton).not.toBeDisabled();

    await user.click(submitButton);
    await waitFor(() => expect(restorePlansApi.abortExecution).toHaveBeenCalledWith("plan-1"));
  });

  it("does not offer Abort for a completed restore execution", async () => {
    setup();
    vi.mocked(restorePlansApi.list).mockResolvedValue([restorePlan({ execution_status: "completed", upid: "UPID:x" })]);
    renderWithProviders(<SystemJobsPage />, { route: "/?tab=restore-plans" });

    await waitFor(() => expect(screen.getByText("200")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Abort execution" })).not.toBeInTheDocument();
  });
});
