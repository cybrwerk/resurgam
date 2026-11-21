import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { BackupGroupOut, CustomerOut, DatastoreOut, PbsInstanceOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { ProtectedWorkloadsPage } from "./ProtectedWorkloadsPage";

vi.mock("../api/resources", () => ({
  customersApi: { list: vi.fn() },
  pbsInstancesApi: { list: vi.fn() },
  datastoresApi: { list: vi.fn(), backupGroups: vi.fn() },
  inventoryApi: {
    snapshots: vi.fn(),
    enableFileIndex: vi.fn(),
    disableFileIndex: vi.fn(),
    setFileIndexExcludePatterns: vi.fn(),
    defaultFileIndexExcludePatterns: vi.fn(),
    setFileIndexMethod: vi.fn(),
  },
  fileRestoreApi: { list: vi.fn(), download: vi.fn() },
  pveClustersApi: { list: vi.fn() },
}));

const { customersApi, pbsInstancesApi, datastoresApi, inventoryApi, pveClustersApi } = await import("../api/resources");
vi.mocked(pveClustersApi.list).mockResolvedValue([]);

const CUSTOMER_A: CustomerOut = {
  id: "cust-a",
  name: "Acme AG",
  slug: "acme",
  debitor_number: "1000",
  match_code: "ACM",
  disabled: false,
  created_at: "2026-01-01T00:00:00Z",
  report_retention_days: null,
};
const CUSTOMER_B: CustomerOut = { ...CUSTOMER_A, id: "cust-b", name: "Beta GmbH", slug: "beta" };

const INSTANCE: PbsInstanceOut = {
  id: "pbs-1",
  display_name: "Primary PBS",
  location: null,
  host: "pbs1.internal.example",
  port: 8007,
  tls_trust_mode: "ca",
  tls_fingerprint_sha256: null,
  version: "4.2.1",
  last_health_check_at: null,
  last_health_status: "ok",
  last_collector_sync_at: null,
  disabled: false,
};

const DATASTORE: DatastoreOut = {
  id: "ds-1",
  pbs_instance_id: "pbs-1",
  name: "main-store",
  path: null,
  comment: null,
  created_at: "2026-01-01T00:00:00Z",
  customer_id: "cust-a",
  role: "primary",
};

function group(overrides: Partial<BackupGroupOut>): BackupGroupOut {
  return {
    id: "group-1",
    datastore_id: "ds-1",
    namespace: "",
    backup_type: "vm",
    backup_id: "100",
    resolved_name: null,
    owner: null,
    last_backup_at: "2026-01-05T00:00:00Z",
    last_verify_status: "ok",
    protection_policy_id: null,
    protection_status: "ok",
    customer_id: null,
    effective_customer_id: "cust-a",
    file_index_enabled: false,
    file_index_exclude_patterns: [],
    file_index_method: "external_mount",
    guest_exec_live_pve_cluster_id: null,
    superseded_at: null,
    removed_from_pbs_at: null,
    last_synced_at: "2026-01-05T00:00:00Z",
    ...overrides,
  };
}

const VM_GROUP = group({ id: "group-vm", backup_type: "vm", backup_id: "100" });
const CT_GROUP = group({ id: "group-ct", backup_type: "ct", backup_id: "200" });
const OTHER_CUSTOMER_VM = group({ id: "group-vm-2", backup_type: "vm", backup_id: "101", effective_customer_id: "cust-b" });

function setup() {
  vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER_A, CUSTOMER_B]);
  vi.mocked(pbsInstancesApi.list).mockResolvedValue([INSTANCE]);
  vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE]);
  vi.mocked(datastoresApi.backupGroups).mockResolvedValue([VM_GROUP, CT_GROUP, OTHER_CUSTOMER_VM]);
  vi.mocked(inventoryApi.defaultFileIndexExcludePatterns).mockResolvedValue({ patterns: [] });
}

describe("ProtectedWorkloadsPage", () => {
  it("shows a loading state while requests are in flight", () => {
    vi.mocked(customersApi.list).mockReturnValue(new Promise(() => {}));
    vi.mocked(pbsInstancesApi.list).mockReturnValue(new Promise(() => {}));
    vi.mocked(datastoresApi.list).mockReturnValue(new Promise(() => {}));
    renderWithProviders(<ProtectedWorkloadsPage titleKey="nav.backupsVms" backupType="vm" />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows a forbidden message on a 403, not a crash", async () => {
    vi.mocked(customersApi.list).mockRejectedValue(new ApiError(403, "forbidden", "no access"));
    vi.mocked(pbsInstancesApi.list).mockResolvedValue([]);
    vi.mocked(datastoresApi.list).mockResolvedValue([]);
    renderWithProviders(<ProtectedWorkloadsPage titleKey="nav.backupsVms" backupType="vm" />);
    await waitFor(() => expect(screen.getByText("You don't have access to this.")).toBeInTheDocument());
  });

  it("shows only groups matching the page's backup type", async () => {
    setup();
    renderWithProviders(<ProtectedWorkloadsPage titleKey="nav.backupsVms" backupType="vm" />);

    await waitFor(() => expect(screen.getByText("100")).toBeInTheDocument());
    expect(screen.getByText("101")).toBeInTheDocument();
    // the ct group must not show up on the vm page
    expect(screen.queryByText("200")).not.toBeInTheDocument();
  });

  it("filters by customer", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<ProtectedWorkloadsPage titleKey="nav.backupsVms" backupType="vm" />);

    await waitFor(() => expect(screen.getByRole("option", { name: "Acme AG" })).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Filter by tenant"), "cust-a");

    await waitFor(() => expect(screen.getByText("100")).toBeInTheDocument());
    expect(screen.queryByText("101")).not.toBeInTheDocument();
  });

  it("shows datastore, customer, and status columns for a real row", async () => {
    setup();
    renderWithProviders(<ProtectedWorkloadsPage titleKey="nav.backupsHosts" backupType="ct" />);

    await waitFor(() => expect(screen.getByText("200")).toBeInTheDocument());
    const table = within(screen.getByRole("table"));
    expect(table.getByText("Acme AG")).toBeInTheDocument();
    expect(table.getByText("Primary PBS / main-store")).toBeInTheDocument();
  });

  it("shows an empty state when nothing matches", async () => {
    setup();
    renderWithProviders(<ProtectedWorkloadsPage titleKey="nav.backupsHosts" backupType="host" />);
    await waitFor(() => expect(screen.getByText("Nothing here yet.")).toBeInTheDocument());
  });

  it("filters rows by a free-text search across workload, customer, and datastore", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<ProtectedWorkloadsPage titleKey="nav.backupsVms" backupType="vm" />);

    await waitFor(() => expect(screen.getByText("100")).toBeInTheDocument());
    expect(screen.getByText("101")).toBeInTheDocument();

    await user.type(screen.getByLabelText("Search"), "100");
    expect(screen.getByText("100")).toBeInTheDocument();
    expect(screen.queryByText("101")).not.toBeInTheDocument();
  });

  it("shows a distinct empty state when a search matches nothing", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<ProtectedWorkloadsPage titleKey="nav.backupsVms" backupType="vm" />);

    await waitFor(() => expect(screen.getByText("100")).toBeInTheDocument());
    await user.type(screen.getByLabelText("Search"), "does-not-exist-anywhere");
    await waitFor(() => expect(screen.getByText("No workloads match your search.")).toBeInTheDocument());
  });

  it("sorts rows when a sortable column header is clicked", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<ProtectedWorkloadsPage titleKey="nav.backupsVms" backupType="vm" />);

    await waitFor(() => expect(screen.getByText("100")).toBeInTheDocument());
    const table = screen.getByRole("table");
    const workloadCells = () => within(table).getAllByRole("row").slice(1).map((row) => row.textContent ?? "");

    await user.click(screen.getByRole("button", { name: /Workload/ }));
    const ascending = workloadCells();
    expect(ascending[0]).toContain("100");
    expect(ascending[1]).toContain("101");

    await user.click(screen.getByRole("button", { name: /Workload/ }));
    const descending = workloadCells();
    expect(descending[0]).toContain("101");
    expect(descending[1]).toContain("100");
  });

  it("opens the restore points modal for the clicked workload", async () => {
    setup();
    vi.mocked(inventoryApi.snapshots).mockResolvedValue([]);
    renderWithProviders(<ProtectedWorkloadsPage titleKey="nav.backupsVms" backupType="vm" />);

    await waitFor(() => expect(screen.getByText("100")).toBeInTheDocument());
    screen.getByText("100").closest("tr")?.click();

    await waitFor(() => expect(inventoryApi.snapshots).toHaveBeenCalledWith("group-vm"));
    expect(screen.getByText("Restore points — 100")).toBeInTheDocument();
  });

  it("toggles per-VM File Indexing directly from the inventory row, without opening the restore points modal", async () => {
    setup();
    vi.mocked(inventoryApi.enableFileIndex).mockResolvedValue({ ...VM_GROUP, file_index_enabled: true });
    const user = userEvent.setup();
    renderWithProviders(<ProtectedWorkloadsPage titleKey="nav.backupsVms" backupType="vm" />);

    await waitFor(() => expect(screen.getByText("100")).toBeInTheDocument());
    const row = screen.getByText("100").closest("tr")!;
    const checkbox = within(row).getByRole("checkbox");
    expect(checkbox).not.toBeChecked();

    await user.click(checkbox);
    await waitFor(() => expect(inventoryApi.enableFileIndex).toHaveBeenCalledWith("group-vm"));
    expect(screen.queryByText("Restore points — 100")).not.toBeInTheDocument();
  });

  it("edits File Index exclusion patterns from the inventory row, without opening the restore points modal", async () => {
    setup();
    vi.mocked(inventoryApi.setFileIndexExcludePatterns).mockResolvedValue({
      ...VM_GROUP,
      file_index_exclude_patterns: ["^/usr/src"],
    });
    const user = userEvent.setup();
    renderWithProviders(<ProtectedWorkloadsPage titleKey="nav.backupsVms" backupType="vm" />);

    await waitFor(() => expect(screen.getByText("100")).toBeInTheDocument());
    const row = screen.getByText("100").closest("tr")!;
    await user.click(within(row).getByText("No exclusions"));

    expect(screen.getByText("File Index settings")).toBeInTheDocument();
    expect(screen.queryByText("Restore points — 100")).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("Exclusion patterns"), "^/usr/src");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(inventoryApi.setFileIndexExcludePatterns).toHaveBeenCalledWith("group-vm", ["^/usr/src"]));
    await waitFor(() => expect(screen.queryByText("File Index settings")).not.toBeInTheDocument());
  });

  it("sorts the stalest (or never-backed-up) workloads to the top by default", async () => {
    const fresh = group({ id: "group-fresh", backup_id: "300", last_backup_at: new Date().toISOString() });
    const neverBackedUp = group({ id: "group-never", backup_id: "301", last_backup_at: null });
    const yearsStale = group({ id: "group-years-stale", backup_id: "302", last_backup_at: "2020-01-01T00:00:00Z" });
    vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER_A, CUSTOMER_B]);
    vi.mocked(pbsInstancesApi.list).mockResolvedValue([INSTANCE]);
    vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE]);
    vi.mocked(datastoresApi.backupGroups).mockResolvedValue([fresh, neverBackedUp, yearsStale]);
    vi.mocked(inventoryApi.defaultFileIndexExcludePatterns).mockResolvedValue({ patterns: [] });
    renderWithProviders(<ProtectedWorkloadsPage titleKey="nav.backupsVms" backupType="vm" />);

    await waitFor(() => expect(screen.getByText("300")).toBeInTheDocument());
    const rows = screen.getAllByRole("row").slice(1); // drop the header row
    // The workload name is the first line of the Workload cell (its storage
    // location is the muted second line).
    const workloadIds = rows.map((row) => within(row).getAllByRole("cell")[1].querySelector("span")?.textContent);
    // Never-backed-up and years-stale (order between the two doesn't
    // matter -- both are maximally stale) come before the fresh one.
    expect(workloadIds.indexOf("301")).toBeLessThan(workloadIds.indexOf("300"));
    expect(workloadIds.indexOf("302")).toBeLessThan(workloadIds.indexOf("300"));

    // Danger tone (years/never stale) vs no special styling (fresh).
    expect(screen.getByText("—").className).toContain("stale-danger");
    const staleDate = screen.getByText(new Date("2020-01-01T00:00:00Z").toLocaleString());
    expect(staleDate.className).toContain("stale-danger");
  });
});
