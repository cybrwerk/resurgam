import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { BackupGroupOut, BackupSnapshotOut, CustomerOut, DatastoreOut, SnapshotIndexStatusOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { FileWorkspacePage } from "./FileWorkspacePage";

vi.mock("../api/resources", () => ({
  customersApi: { list: vi.fn() },
  datastoresApi: { list: vi.fn(), backupGroups: vi.fn() },
  inventoryApi: { snapshots: vi.fn(), enableFileIndex: vi.fn(), disableFileIndex: vi.fn() },
  fileIndexApi: {
    snapshotStatus: vi.fn(),
    browse: vi.fn(),
    search: vi.fn(),
    listJobs: vi.fn(),
    createBulkJob: vi.fn(),
    createJob: vi.fn(),
    cancelJob: vi.fn(),
  },
  fileIndexSettingsApi: { get: vi.fn() },
  fileRestoreApi: { createJob: vi.fn(), getJob: vi.fn(), saveJobContent: vi.fn().mockResolvedValue(undefined) },
}));

const { customersApi, datastoresApi, inventoryApi, fileIndexApi, fileIndexSettingsApi, fileRestoreApi } =
  await import("../api/resources");

const CUSTOMER: CustomerOut = {
  id: "cust-1",
  name: "Acme AG",
  slug: "acme",
  debitor_number: "1000",
  match_code: "ACM",
  disabled: false,
  created_at: "2026-01-01T00:00:00Z",
  report_retention_days: null,
};

const DATASTORE: DatastoreOut = {
  id: "ds-1",
  pbs_instance_id: "pbs-1",
  name: "main-store",
  path: null,
  comment: null,
  created_at: "2026-01-01T00:00:00Z",
  customer_id: "cust-1",
  role: "primary",
};

const GROUP: BackupGroupOut = {
  id: "group-1",
  datastore_id: "ds-1",
  namespace: "",
  backup_type: "vm",
  backup_id: "116",
  resolved_name: "web-01",
  owner: null,
  last_backup_at: null,
  last_verify_status: "ok",
  protection_policy_id: null,
  protection_status: "unknown",
  customer_id: null,
  effective_customer_id: "cust-1",
  file_index_enabled: true,
  file_index_exclude_patterns: [],
  file_index_method: "external_mount",
  guest_exec_live_pve_cluster_id: null,
  superseded_at: null,
  removed_from_pbs_at: null,
  last_synced_at: "2026-01-01T00:00:00Z",
};

function snapshot(overrides: Partial<BackupSnapshotOut> = {}): BackupSnapshotOut {
  return {
    id: "snap-1",
    backup_group_id: "group-1",
    snapshot_time: "2026-03-15T10:00:00Z",
    verify_status: "ok",
    size_bytes: 1024,
    last_synced_at: "2026-03-15T10:00:00Z",
    ...overrides,
  };
}

function setup(status: SnapshotIndexStatusOut = { indexed_snapshot_ids: [] }) {
  vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
  vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE]);
  vi.mocked(datastoresApi.backupGroups).mockResolvedValue([GROUP]);
  vi.mocked(inventoryApi.snapshots).mockResolvedValue([snapshot()]);
  vi.mocked(fileIndexApi.snapshotStatus).mockResolvedValue(status);
  vi.mocked(fileIndexApi.listJobs).mockResolvedValue([]);
  vi.mocked(fileIndexSettingsApi.get).mockRejectedValue(new ApiError(404, "not_found", "Not found"));
}

async function pickCustomerAndGroup(user: ReturnType<typeof userEvent.setup>, groupLabel: RegExp = /VM web-01/) {
  await waitFor(() => expect(screen.getByRole("option", { name: "Acme AG" })).toBeInTheDocument());
  await user.selectOptions(screen.getByLabelText("Tenant"), "cust-1");
  await waitFor(() => expect(screen.getByLabelText("Backup group")).toBeInTheDocument());
  await user.click(screen.getByLabelText("Backup group"));
  await waitFor(() => expect(screen.getByRole("option", { name: groupLabel })).toBeInTheDocument());
  await user.click(screen.getByRole("option", { name: groupLabel }));
}

describe("FileWorkspacePage", () => {
  it("shows a pick-a-customer hint before anything is selected", async () => {
    setup();
    renderWithProviders(<FileWorkspacePage />);
    await waitFor(() => expect(screen.getByText("Pick a tenant to get started.")).toBeInTheDocument());
    expect(screen.queryByLabelText("Backup group")).not.toBeInTheDocument();
  });

  it("keeps browsing detached in its own window -- nothing renders until a restore point is picked", async () => {
    // Raised directly by the user: the page felt overloaded with the
    // calendar, tree/browser, and indexing jobs all stacked at once.
    setup({ indexed_snapshot_ids: ["snap-1"] });
    const user = userEvent.setup();
    renderWithProviders(<FileWorkspacePage />);

    await pickCustomerAndGroup(user);
    await waitFor(() => expect(screen.getByText(/March 2026|März 2026/)).toBeInTheDocument());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("opens the File Browser window and browses a green (indexed) snapshot via the index panel", async () => {
    setup({ indexed_snapshot_ids: ["snap-1"] });
    vi.mocked(fileIndexApi.browse).mockResolvedValue([]);
    const user = userEvent.setup();
    renderWithProviders(<FileWorkspacePage />);

    await pickCustomerAndGroup(user);
    await waitFor(() => expect(screen.getByText(/March 2026|März 2026/)).toBeInTheDocument());

    const day15 = screen.getAllByText("15", { selector: "span" }).map((el) => el.closest("button")).find(Boolean);
    await user.click(day15!);

    const dialog = await screen.findByRole("dialog");
    await waitFor(() =>
      expect(fileIndexApi.browse).toHaveBeenCalledWith({ backupGroupId: "group-1", backupSnapshotId: "snap-1", path: "/" }),
    );
    // The disk tree also browses the index for a green snapshot.
    expect(fileIndexApi.browse).toHaveBeenCalled();

    await user.click(within(dialog).getByLabelText("Close"));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("gates a yellow (not-yet-indexed) snapshot behind a Run button instead of mounting it immediately", async () => {
    // Raised directly by the user: mounting a not-yet-indexed snapshot
    // is a real, potentially slow live operation (proxmox-file-restore)
    // and must be an explicit action, not something that fires the
    // instant a yellow day is clicked.
    setup({ indexed_snapshot_ids: [] });
    const user = userEvent.setup();
    renderWithProviders(<FileWorkspacePage />);

    await pickCustomerAndGroup(user);
    const day15 = screen.getAllByText("15", { selector: "span" }).map((el) => el.closest("button")).find(Boolean);
    await user.click(day15!);

    await waitFor(() => expect(screen.getByText("This snapshot hasn't been indexed yet.")).toBeInTheDocument());
    expect(fileRestoreApi.createJob).not.toHaveBeenCalled();
    expect(screen.queryByText("Disks")).not.toBeInTheDocument();
  });

  it("opens a not-yet-indexed container snapshot directly -- no mount, no Run gate", async () => {
    // User report (2026-10-07): CT browsing reads the PBS catalog and never
    // needed indexing or a mount; the VM-only "Run" gate had been applied
    // to every backup type.
    setup({ indexed_snapshot_ids: [] });
    vi.mocked(datastoresApi.backupGroups).mockResolvedValue([{ ...GROUP, backup_type: "ct", backup_id: "1100" }]);
    const user = userEvent.setup();
    renderWithProviders(<FileWorkspacePage />);

    await pickCustomerAndGroup(user, /CT web-01/);
    const day15 = screen.getAllByText("15", { selector: "span" }).map((el) => el.closest("button")).find(Boolean);
    await user.click(day15!);

    // Straight into the browser (a container has a root directory, not disks).
    await waitFor(() => expect(screen.getByText("Root filesystem")).toBeInTheDocument());
    expect(screen.queryByText("This snapshot hasn't been indexed yet.")).not.toBeInTheDocument();
  });

  it("mounts a yellow snapshot via the live file-restore job queue only after Run is clicked", async () => {
    setup({ indexed_snapshot_ids: [] });
    vi.mocked(fileRestoreApi.createJob).mockResolvedValue({
      id: "job-1", job_type: "list", status: "completed", workload_label: "web-01", path: "/",
      result: [], result_filename: null, error_code: null, error_message: null,
      expires_at: "2026-01-01T00:10:00Z", started_at: null, completed_at: null, created_at: "2026-01-01T00:00:00Z",
    });
    const user = userEvent.setup();
    renderWithProviders(<FileWorkspacePage />);

    await pickCustomerAndGroup(user);
    const day15 = screen.getAllByText("15", { selector: "span" }).map((el) => el.closest("button")).find(Boolean);
    await user.click(day15!);
    await waitFor(() => expect(screen.getByRole("button", { name: "Run" })).toBeInTheDocument());
    expect(fileRestoreApi.createJob).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() => expect(fileRestoreApi.createJob).toHaveBeenCalledWith("group-1", "snap-1", "list", "/"));
    expect(fileIndexApi.browse).not.toHaveBeenCalled();
  });

  it("picks up the sidebar's already-selected tenant automatically", async () => {
    setup();
    window.localStorage.setItem("resurgam.selectedCustomerId", "cust-1");
    renderWithProviders(<FileWorkspacePage />);

    await waitFor(() => expect(screen.getByLabelText("Tenant")).toHaveValue("cust-1"));
    await waitFor(() => expect(screen.getByLabelText("Backup group")).toBeInTheDocument());
  });
});
