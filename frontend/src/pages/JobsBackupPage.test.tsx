import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { PveClusterOut, VzdumpJobOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { JobsBackupPage } from "./JobsBackupPage";

vi.mock("../api/resources", () => ({
  pveClustersApi: {
    list: vi.fn(),
    backupJobs: vi.fn(),
    backupJobCapabilities: vi.fn(),
    runBackupJob: vi.fn(),
    taskProgress: vi.fn(),
    nodes: vi.fn(),
    storages: vi.fn(),
    enableBackupJob: vi.fn(),
    disableBackupJob: vi.fn(),
    cancelBackupJobReenable: vi.fn(),
    setBackupJobReenable: vi.fn(),
    deleteBackupJob: vi.fn(),
    assignBackupJobCustomer: vi.fn(),
    unassignBackupJobCustomer: vi.fn(),
  },
  datastoresApi: {
    list: vi.fn(),
  },
  customersApi: {
    list: vi.fn(),
  },
  pbsJobsApi: {
    pruneJobs: vi.fn(),
  },
  pbsInstancesApi: {
    list: vi.fn(),
  },
}));

const { pveClustersApi, datastoresApi, customersApi } = await import("../api/resources");

const CLUSTER_A: PveClusterOut = {
  id: "cluster-a",
  display_name: "Cluster A",
  host: "a.example.com",
  port: 8006,
  tls_trust_mode: "ca",
  tls_fingerprint_sha256: null,
  version: "9.2",
  last_health_check_at: null,
  last_health_status: "ok",
  disabled: false,
  linked_pbs_instance_ids: [],
  linked_datastore_ids: [],
  allowed_target_storages: null,
  restore_network_rules: null,
  require_restore_approval: false,
  is_lab_compute: false,
  lab_sdn_zone_name: null,
  lab_appliance_template_vmid: null,
  lab_snippet_storage_id: null,
  lab_appliance_uplink_bridge: null,
};

const CLUSTER_B: PveClusterOut = { ...CLUSTER_A, id: "cluster-b", display_name: "Cluster B" };

function job(overrides: Partial<VzdumpJobOut>): VzdumpJobOut {
  return {
    id: "backup-1",
    enabled: true,
    schedule: "02:30",
    storage: "pbs-main",
    node: null,
    comment: null,
    all_guests: true,
    vmid: null,
    pool: null,
    exclude: null,
    mode: null,
    compress: null,
    notes_template: null,
    mailnotification: null,
    mailto: null,
    prune_keep_last: null,
    prune_keep_hourly: null,
    prune_keep_daily: null,
    prune_keep_weekly: null,
    prune_keep_monthly: null,
    prune_keep_yearly: null,
    customer_id: null,
    ...overrides,
  };
}

function setup() {
  vi.mocked(pveClustersApi.list).mockResolvedValue([CLUSTER_A, CLUSTER_B]);
  vi.mocked(pveClustersApi.backupJobCapabilities).mockResolvedValue({ can_read: true, can_write: true });
  vi.mocked(pveClustersApi.nodes).mockResolvedValue([{ node: "pve1", status: "online", cpu: null, maxmem: null, uptime: null }]);
  vi.mocked(pveClustersApi.storages).mockResolvedValue([]);
  vi.mocked(datastoresApi.list).mockResolvedValue([]);
  vi.mocked(customersApi.list).mockResolvedValue([]);
}

describe("JobsBackupPage", () => {
  it("lists vzdump jobs from every cluster in one table, with a cluster column", async () => {
    setup();
    vi.mocked(pveClustersApi.backupJobs).mockImplementation((clusterId) =>
      Promise.resolve(clusterId === "cluster-a" ? [job({ id: "job-a", schedule: "02:30" })] : [job({ id: "job-b", schedule: "21:00" })]),
    );
    renderWithProviders(<JobsBackupPage />);

    await waitFor(() => expect(screen.getByText("02:30")).toBeInTheDocument());
    expect(screen.getByText("21:00")).toBeInTheDocument();
    expect(screen.getByText("Cluster A")).toBeInTheDocument();
    expect(screen.getByText("Cluster B")).toBeInTheDocument();
  });

  it("hides jobs whose resolved customer scope doesn't match the selected tenant", async () => {
    setup();
    vi.mocked(pveClustersApi.backupJobs).mockImplementation((clusterId) =>
      Promise.resolve(
        clusterId === "cluster-a"
          ? [job({ id: "job-a", schedule: "02:30", customer_id: "cust-1" })]
          : [job({ id: "job-b", schedule: "21:00", customer_id: "cust-2" })],
      ),
    );
    window.localStorage.setItem("resurgam.selectedCustomerId", "cust-1");
    renderWithProviders(<JobsBackupPage />);

    await waitFor(() => expect(screen.getByText("02:30")).toBeInTheDocument());
    expect(screen.queryByText("21:00")).not.toBeInTheDocument();
    window.localStorage.removeItem("resurgam.selectedCustomerId");
  });

  it("opens the target-cluster picker before the create form", async () => {
    setup();
    vi.mocked(pveClustersApi.backupJobs).mockResolvedValue([]);
    const user = userEvent.setup();
    renderWithProviders(<JobsBackupPage />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Create" })).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByText("Choose a target cluster")).toBeInTheDocument();
  });

  it("runs a job now and follows its task live", async () => {
    setup();
    vi.mocked(pveClustersApi.list).mockResolvedValue([CLUSTER_A]);
    vi.mocked(pveClustersApi.backupJobs).mockResolvedValue([job({ id: "job-a", schedule: "02:30", comment: "Daily All" })]);
    const upid = "UPID:pve1:0000A1B2:00C0FFEE:6708F0A1:vzdump::root@pam:";
    vi.mocked(pveClustersApi.runBackupJob).mockResolvedValue({
      job_id: "job-a",
      tasks: [{ node: "pve1", upid, vmids: [101] }],
      skipped: [{ vmid: 102, reason: "other_node" }],
    });
    vi.mocked(pveClustersApi.taskProgress)
      .mockResolvedValueOnce({
        node: "pve1",
        upid,
        status: "running",
        exitstatus: null,
        lines: [{ n: 1, t: "INFO: starting new backup job" }],
        next_start: 1,
        more: false,
      })
      .mockResolvedValue({
        node: "pve1",
        upid,
        status: "stopped",
        exitstatus: "OK",
        lines: [{ n: 2, t: "INFO: Backup job finished successfully" }],
        next_start: 2,
        more: false,
      });
    const user = userEvent.setup();
    renderWithProviders(<JobsBackupPage />);

    await user.click(await screen.findByRole("button", { name: "Run now" }));
    const dialog = await screen.findByRole("dialog", { name: "Run backup job “Daily All” now?" });
    await user.click(within(dialog).getByRole("button", { name: "Run now" }));
    await waitFor(() => expect(pveClustersApi.runBackupJob).toHaveBeenCalledWith("cluster-a", "job-a"));

    const task = await screen.findByTestId("backup-task-pve1");
    expect(await within(task).findByText(/starting new backup job/)).toBeInTheDocument();
    await waitFor(() => expect(within(task).getByText("Finished")).toBeInTheDocument(), { timeout: 5000 });
    expect(within(task).getByText(/Backup job finished successfully/)).toBeInTheDocument();
    expect(pveClustersApi.taskProgress).toHaveBeenLastCalledWith("cluster-a", "pve1", upid, 1);
    expect(screen.getByText(/the job is bound to one node and these guests run on another: 102/)).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Backup “Daily All” — live status" })).toBeInTheDocument();

    // Closing keeps the run reachable.
    await user.click(screen.getByRole("button", { name: /close/i }));
    await user.click(await screen.findByRole("button", { name: "Show live status" }));
    expect(await screen.findByTestId("backup-task-pve1")).toBeInTheDocument();
  });

  it("keeps reading the log after the task stopped while full pages come back", async () => {
    setup();
    vi.mocked(pveClustersApi.list).mockResolvedValue([CLUSTER_A]);
    vi.mocked(pveClustersApi.backupJobs).mockResolvedValue([job({ id: "job-a" })]);
    const upid = "UPID:pve1:0000A1B2:00C0FFEE:6708F0A1:vzdump::root@pam:";
    vi.mocked(pveClustersApi.runBackupJob).mockResolvedValue({ job_id: "job-a", tasks: [{ node: "pve1", upid, vmids: null }], skipped: [] });
    const page = Array.from({ length: 500 }, (_, i) => ({ n: i + 1, t: `line ${i + 1}` }));
    vi.mocked(pveClustersApi.taskProgress).mockReset();
    vi.mocked(pveClustersApi.taskProgress)
      .mockResolvedValueOnce({ node: "pve1", upid, status: "stopped", exitstatus: "OK", lines: page, next_start: 500, more: true })
      .mockResolvedValueOnce({
        node: "pve1",
        upid,
        status: "stopped",
        exitstatus: "OK",
        lines: [{ n: 501, t: "TASK OK" }],
        next_start: 501,
        more: false,
      });
    const user = userEvent.setup();
    renderWithProviders(<JobsBackupPage />);

    await user.click(await screen.findByRole("button", { name: "Run now" }));
    const dialog = await screen.findByRole("dialog", { name: "Run backup job “job-a” now?" });
    await user.click(within(dialog).getByRole("button", { name: "Run now" }));
    const task = await screen.findByTestId("backup-task-pve1");
    expect(await within(task).findByText(/TASK OK/)).toBeInTheDocument();
    expect(pveClustersApi.taskProgress).toHaveBeenCalledTimes(2);
    expect(pveClustersApi.taskProgress).toHaveBeenLastCalledWith("cluster-a", "pve1", upid, 500);
  });

  it("disables Run now without write privileges and says why", async () => {
    setup();
    vi.mocked(pveClustersApi.list).mockResolvedValue([CLUSTER_A]);
    vi.mocked(pveClustersApi.backupJobCapabilities).mockResolvedValue({ can_read: true, can_write: false });
    vi.mocked(pveClustersApi.backupJobs).mockResolvedValue([job({ id: "job-a" })]);
    renderWithProviders(<JobsBackupPage />);

    const button = await screen.findByRole("button", { name: "Run now" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", "The PVE token lacks the needed privileges");
  });

  describe("disable for N hours", () => {
    async function openDisableDialog() {
      setup();
      vi.mocked(pveClustersApi.list).mockResolvedValue([CLUSTER_A]);
      vi.mocked(pveClustersApi.backupJobs).mockResolvedValue([job({ id: "job-a", schedule: "02:30" })]);
      vi.mocked(pveClustersApi.disableBackupJob).mockResolvedValue(job({ id: "job-a", enabled: false }));
      const user = userEvent.setup();
      renderWithProviders(<JobsBackupPage />);
      await user.click(await screen.findByRole("button", { name: "Disable" }));
      const dialog = await screen.findByRole("dialog", { name: "Disable backup job" });
      return { user, dialog };
    }

    it("disables indefinitely by default", async () => {
      const { user, dialog } = await openDisableDialog();
      expect(within(dialog).getByText("The job stays disabled until someone enables it again.")).toBeInTheDocument();
      await user.click(within(dialog).getByRole("button", { name: "Disable" }));
      await waitFor(() => expect(pveClustersApi.disableBackupJob).toHaveBeenCalledWith("cluster-a", "job-a", null));
    });

    it("passes the chosen duration", async () => {
      const { user, dialog } = await openDisableDialog();
      await user.selectOptions(within(dialog).getByLabelText("Re-enable automatically"), "After 4 hours");
      await user.click(within(dialog).getByRole("button", { name: "Disable" }));
      await waitFor(() => expect(pveClustersApi.disableBackupJob).toHaveBeenCalledWith("cluster-a", "job-a", 4));
    });

    it("accepts a custom duration and blocks invalid ones", async () => {
      const { user, dialog } = await openDisableDialog();
      await user.selectOptions(within(dialog).getByLabelText("Re-enable automatically"), "Custom…");
      const input = within(dialog).getByLabelText("Hours (1–168)");
      const confirm = within(dialog).getByRole("button", { name: "Disable" });

      await user.clear(input);
      await user.type(input, "200");
      expect(confirm).toBeDisabled();
      expect(within(dialog).getByRole("alert")).toHaveTextContent("from 1 to 168");

      await user.clear(input);
      await user.type(input, "36");
      expect(confirm).toBeEnabled();
      await user.click(confirm);
      await waitFor(() => expect(pveClustersApi.disableBackupJob).toHaveBeenCalledWith("cluster-a", "job-a", 36));
    });

    it("shows a pending timer, lets you cancel it, and offers Enable now", async () => {
      setup();
      vi.mocked(pveClustersApi.list).mockResolvedValue([CLUSTER_A]);
      vi.mocked(pveClustersApi.backupJobs).mockResolvedValue([
        job({ id: "job-a", enabled: false, reenable_at: "2026-11-11T18:00:00Z" }),
      ]);
      vi.mocked(pveClustersApi.cancelBackupJobReenable).mockResolvedValue(undefined);
      const user = userEvent.setup();
      renderWithProviders(<JobsBackupPage />);

      expect(await screen.findByText("Disabled (timer)")).toBeInTheDocument();
      expect(screen.getByText(/^Re-enables /)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Enable now" })).toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: "Cancel timer" }));
      await waitFor(() => expect(pveClustersApi.cancelBackupJobReenable).toHaveBeenCalledWith("cluster-a", "job-a"));
    });

    it("changes a pending timer, counted from now", async () => {
      setup();
      vi.mocked(pveClustersApi.list).mockResolvedValue([CLUSTER_A]);
      vi.mocked(pveClustersApi.backupJobs).mockResolvedValue([
        job({ id: "job-a", enabled: false, reenable_at: "2026-11-11T18:00:00Z" }),
      ]);
      vi.mocked(pveClustersApi.setBackupJobReenable).mockResolvedValue(
        job({ id: "job-a", enabled: false, reenable_at: "2026-11-12T18:00:00Z" }),
      );
      const user = userEvent.setup();
      renderWithProviders(<JobsBackupPage />);

      await user.click(await screen.findByRole("button", { name: "Change" }));
      const dialog = await screen.findByRole("dialog", { name: "Change re-enable timer" });
      const select = within(dialog).getByLabelText("Enable again");
      expect(within(select).queryByRole("option", { name: "Never (until someone enables it)" })).not.toBeInTheDocument();
      expect(select).toHaveValue("24");

      await user.selectOptions(select, "After 4 hours");
      await user.click(within(dialog).getByRole("button", { name: "Save" }));
      await waitFor(() => expect(pveClustersApi.setBackupJobReenable).toHaveBeenCalledWith("cluster-a", "job-a", 4));
      expect(pveClustersApi.disableBackupJob).not.toHaveBeenCalled();
    });

    it("shows a failed re-enable attempt", async () => {
      setup();
      vi.mocked(pveClustersApi.list).mockResolvedValue([CLUSTER_A]);
      vi.mocked(pveClustersApi.backupJobs).mockResolvedValue([
        job({
          id: "job-a",
          enabled: false,
          reenable_at: "2026-11-11T18:00:00Z",
          reenable_last_error: "PVE not reachable: connection refused",
        }),
      ]);
      renderWithProviders(<JobsBackupPage />);
      expect(
        await screen.findByText("Re-enable failed, retrying every minute: PVE not reachable: connection refused"),
      ).toBeInTheDocument();
    });

    it("plain disabled jobs keep the plain Enable button and no timer", async () => {
      setup();
      vi.mocked(pveClustersApi.list).mockResolvedValue([CLUSTER_A]);
      vi.mocked(pveClustersApi.backupJobs).mockResolvedValue([job({ id: "job-a", enabled: false })]);
      renderWithProviders(<JobsBackupPage />);
      expect(await screen.findByRole("button", { name: "Enable" })).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Cancel timer" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Change" })).not.toBeInTheDocument();
    });
  });
});
