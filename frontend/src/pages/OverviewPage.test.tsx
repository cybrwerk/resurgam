import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { AlarmOut, BackupGroupOut, CustomerOut, DatastoreOut, DatastoreUsageOut, PbsInstanceOut, TaskRunOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { OverviewPage } from "./OverviewPage";

vi.mock("../api/resources", () => ({
  pbsInstancesApi: { list: vi.fn() },
  pveClustersApi: { list: vi.fn() },
  datastoresApi: { list: vi.fn(), backupGroups: vi.fn(), usage: vi.fn() },
  customersApi: { list: vi.fn() },
  alarmsApi: { list: vi.fn() },
  tasksApi: { list: vi.fn() },
}));

const { pbsInstancesApi, pveClustersApi, datastoresApi, customersApi, alarmsApi, tasksApi } = await import("../api/resources");

const PBS_INSTANCE: PbsInstanceOut = {
  id: "pbs-1",
  display_name: "pbs-main",
  location: null,
  host: "pbs.example.com",
  port: 8007,
  tls_trust_mode: "fingerprint",
  tls_fingerprint_sha256: null,
  version: "3.2",
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
  customer_id: null,
  role: "primary",
};

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

function group(overrides: Partial<BackupGroupOut> = {}): BackupGroupOut {
  return {
    id: "group-1",
    datastore_id: "ds-1",
    namespace: "",
    backup_type: "vm",
    backup_id: "116",
    resolved_name: "web-01",
    owner: null,
    last_backup_at: "2026-01-01T00:00:00Z",
    last_verify_status: "ok",
    protection_policy_id: null,
    protection_status: "ok",
    customer_id: null,
    effective_customer_id: "cust-1",
    file_index_enabled: false,
    file_index_exclude_patterns: [],
    file_index_method: "external_mount",
    guest_exec_live_pve_cluster_id: null,
    last_synced_at: "2026-01-01T00:00:00Z",
    superseded_at: null,
    removed_from_pbs_at: null,
    ...overrides,
  };
}

function setup() {
  vi.mocked(pbsInstancesApi.list).mockResolvedValue([PBS_INSTANCE]);
  vi.mocked(pveClustersApi.list).mockResolvedValue([]);
  vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE]);
  vi.mocked(datastoresApi.backupGroups).mockResolvedValue([group()]);
  vi.mocked(datastoresApi.usage).mockResolvedValue({
    latest: {
      id: "u-1", datastore_id: "ds-1", captured_at: "2026-01-01T00:00:00Z",
      used_bytes: 500_000_000, available_bytes: 500_000_000, total_bytes: 1_000_000_000,
      logical_bytes: 500_000_000, usage_percent: 50, group_count_vm: 1, group_count_ct: 0,
      group_count_host: 0, snapshot_count: 5,
    } as DatastoreUsageOut["latest"],
    history: [],
    daily_change_bytes: null,
    monthly_change_bytes: null,
    forecast: { status: "insufficient_history", basis_days: 0, growth_bytes_per_day: null, days_until_full: null, full_at: null },
  });
  vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
  vi.mocked(alarmsApi.list).mockResolvedValue([]);
  vi.mocked(tasksApi.list).mockResolvedValue([
    { id: "t-1", pbs_instance_id: "pbs-1", upid: "UPID", task_type: "backup", worker_id: null, status: "ok", started_at: "2026-01-01T00:00:00Z", ended_at: "2026-01-01T00:05:00Z", task_user: null, last_synced_at: "2026-01-01T00:05:00Z" },
  ] as TaskRunOut[]);
}

describe("OverviewPage", () => {
  it("shows four KPI tiles filling the row, including customers", async () => {
    setup();
    renderWithProviders(<OverviewPage />);
    await waitFor(() => expect(screen.getByText("Tenants")).toBeInTheDocument());
    expect(screen.getByText("PBS Instances")).toBeInTheDocument();
    expect(screen.getByText("PVE Targets")).toBeInTheDocument();
    expect(screen.getByText("Datastores")).toBeInTheDocument();
  });

  it("shows recent task activity", async () => {
    setup();
    renderWithProviders(<OverviewPage />);
    await waitFor(() => expect(screen.getByText("Recent Activity")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("backup")).toBeInTheDocument());
    expect(screen.getByText("pbs-main")).toBeInTheDocument();
  });

  it("flags an overdue backup group using the shared staleness helper", async () => {
    setup();
    vi.mocked(datastoresApi.backupGroups).mockResolvedValue([group({ last_backup_at: null })]);
    renderWithProviders(<OverviewPage />);
    await waitFor(() => expect(screen.getByText("Overdue")).toBeInTheDocument());
    expect(screen.getByText("web-01")).toBeInTheDocument();
  });

  it("opens every overdue backup in a window, filterable by search", async () => {
    setup();
    const longAgo = new Date(Date.now() - 90 * 24 * 3600 * 1000).toISOString();
    const recentish = new Date(Date.now() - 72 * 3600 * 1000).toISOString();
    vi.mocked(datastoresApi.backupGroups).mockResolvedValue(
      Array.from({ length: 7 }, (_, i) =>
        group({ id: `g-${i}`, backup_id: String(100 + i), resolved_name: `vm-${i}`, last_backup_at: i === 0 ? recentish : longAgo }),
      ),
    );
    const user = userEvent.setup();
    renderWithProviders(<OverviewPage />);

    await user.click(await screen.findByRole("button", { name: "Show all (7)" }));
    const dialog = within(await screen.findByRole("dialog", { name: "Overdue backups (7)" }));
    for (let i = 0; i < 7; i++) expect(dialog.getByText(`vm-${i}`)).toBeInTheDocument();
    expect(dialog.getAllByText("Acme AG").length).toBe(7);
    expect(dialog.getByText("3 days")).toBeInTheDocument();

    await user.type(dialog.getByRole("searchbox"), "vm-3");
    expect(dialog.getByText("vm-3")).toBeInTheDocument();
    expect(dialog.queryByText("vm-4")).not.toBeInTheDocument();
  });

  it("opens the same window from the +N more line", async () => {
    setup();
    vi.mocked(datastoresApi.backupGroups).mockResolvedValue(
      Array.from({ length: 6 }, (_, i) => group({ id: `g-${i}`, resolved_name: `vm-${i}`, last_backup_at: null })),
    );
    const user = userEvent.setup();
    renderWithProviders(<OverviewPage />);
    await user.click(await screen.findByRole("button", { name: "+1 more" }));
    expect(await screen.findByRole("dialog", { name: "Overdue backups (6)" })).toBeInTheDocument();
  });

  it("shows no-overdue empty state when every live group was backed up recently", async () => {
    setup();
    renderWithProviders(<OverviewPage />);
    await waitFor(() => expect(screen.getByText("No overdue backups.")).toBeInTheDocument());
  });

  it("excludes superseded and removed-from-PBS groups from the overdue count", async () => {
    setup();
    vi.mocked(datastoresApi.backupGroups).mockResolvedValue([
      group({ last_backup_at: null, superseded_at: "2026-01-01T00:00:00Z" }),
      group({ id: "group-2", last_backup_at: null, removed_from_pbs_at: "2026-01-01T00:00:00Z" }),
    ]);
    renderWithProviders(<OverviewPage />);
    await waitFor(() => expect(screen.getByText("No overdue backups.")).toBeInTheDocument());
  });

  it("shows datastore usage as a percentage with used/total bytes", async () => {
    setup();
    renderWithProviders(<OverviewPage />);
    await waitFor(() => expect(screen.getByText("Datastore Usage")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText(/50%/)).toBeInTheDocument());
  });

  it("still shows the alarms panel as before", async () => {
    setup();
    vi.mocked(alarmsApi.list).mockResolvedValue([
      { type: "test", severity: "critical", customer_id: "cust-1", customer_name: "Acme AG", customer_match_code: "ACM", message: "Something broke", entity_type: "datastore", entity_id: "ds-1" },
    ] as AlarmOut[]);
    renderWithProviders(<OverviewPage />);
    await waitFor(() => expect(screen.getByText("Something broke")).toBeInTheDocument());
  });
});
