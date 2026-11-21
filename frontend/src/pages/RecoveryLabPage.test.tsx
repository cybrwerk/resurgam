import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type {
  ApplicationGroupOut,
  BackupGroupOut,
  CustomerOut,
  DatastoreOut,
  PveClusterOut,
  PveNodeOut,
  PveStorageOut,
  RestoreTestPlanOut,
} from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { RecoveryLabPage } from "./RecoveryLabPage";

vi.mock("../api/resources", () => ({
  applicationGroupsApi: { list: vi.fn(), get: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn() },
  customersApi: { list: vi.fn() },
  datastoresApi: { list: vi.fn(), backupGroups: vi.fn() },
  pveClustersApi: { list: vi.fn(), nodes: vi.fn(), storages: vi.fn() },
  restoreTestPlansApi: { create: vi.fn(), list: vi.fn(), get: vi.fn(), execute: vi.fn(), poll: vi.fn(), cleanup: vi.fn() },
}));

const { applicationGroupsApi, customersApi, datastoresApi, pveClustersApi, restoreTestPlansApi } =
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

const VM_GROUP: BackupGroupOut = {
  id: "group-vm",
  datastore_id: "ds-1",
  namespace: "",
  backup_type: "vm",
  backup_id: "116",
  resolved_name: "db-server",
  owner: null,
  last_backup_at: null,
  last_verify_status: "ok",
  protection_policy_id: null,
  protection_status: "unknown",
  customer_id: null,
  effective_customer_id: "cust-1",
  file_index_enabled: false,
  file_index_exclude_patterns: [],
  file_index_method: "external_mount",
  guest_exec_live_pve_cluster_id: null,
  superseded_at: null,
  removed_from_pbs_at: null,
  last_synced_at: "2026-01-01T00:00:00Z",
};

const APPLICATION_GROUP: ApplicationGroupOut = {
  id: "app-group-1",
  customer_id: "cust-1",
  name: "Order Processing",
  created_at: "2026-01-01T00:00:00Z",
  members: [
    {
      id: "member-1", backup_group_id: "group-vm", startup_order: 0, check_types: ["guest_agent", "icmp"],
      expected_ip: null, backup_group_superseded: false,
    },
  ],
  lab_network_mode: "dhcp",
  lab_subnet_cidr: null,
  lab_gateway_ip: null,
  lab_dns_ip: null,
  lab_dhcp_range_start: null,
  lab_dhcp_range_end: null,
  lab_schedule_enabled: false,
  lab_schedule_interval_hours: null,
  lab_next_scheduled_run_at: null,
  lab_schedule_pve_cluster_id: null,
  lab_schedule_target_node: null,
  lab_schedule_target_storage: null,
};

const OTHER_APPLICATION_GROUP: ApplicationGroupOut = {
  ...APPLICATION_GROUP,
  id: "app-group-2",
  name: "Database Tier",
};

const LAB_CLUSTER: PveClusterOut = {
  id: "pve-1",
  display_name: "Lab Cluster",
  host: "pve-lab.internal.example",
  port: 8006,
  tls_trust_mode: "ca",
  tls_fingerprint_sha256: null,
  allowed_target_storages: null,
  restore_network_rules: null,
  require_restore_approval: false,
  version: "9.2",
  last_health_check_at: null,
  last_health_status: "ok",
  disabled: false,
  linked_pbs_instance_ids: [],
  linked_datastore_ids: [],
  is_lab_compute: true,
  lab_sdn_zone_name: null,
  lab_appliance_template_vmid: 9000,
  lab_snippet_storage_id: "local",
  lab_appliance_uplink_bridge: "vmbr0",
};

const NODE: PveNodeOut = { node: "pve-node-1", status: "online", cpu: 0.1, maxmem: 1000, uptime: 1000 };
const STORAGE: PveStorageOut = {
  storage: "local-lvm",
  type: "lvmthin",
  content: ["images"],
  active: true,
  shared: false,
  avail_bytes: null,
  used_bytes: null,
  total_bytes: null,
  pbs_server: null,
  pbs_datastore: null,
  pbs_namespace: null,
};

const PLANNED_RUN: RestoreTestPlanOut = {
  id: "run-1",
  created_at: "2026-01-02T00:00:00Z",
  application_group_id: "app-group-1",
  customer_id: "cust-1",
  pve_cluster_id: "pve-1",
  vnet_name: "lababcd1234",
  appliance_vmid: 9001,
  status: "planned",
  created_by_user_id: null,
  requested_at: null,
  completed_at: null,
  error: null,
  members: [
    {
      id: "member-run-1",
      application_group_member_id: "member-1",
      backup_snapshot_id: "snap-1",
      target_vmid: 9002,
      target_node: "pve-node-1",
      target_storage: "local-lvm",
      upid: null,
      member_status: "pending",
      probe_results: [],
      resolved_ip: null,
      started_at: null,
      completed_at: null,
      error: null,
    },
  ],
};

function mockCommonQueries() {
  vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
  vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE]);
  vi.mocked(datastoresApi.backupGroups).mockResolvedValue([VM_GROUP]);
  vi.mocked(pveClustersApi.list).mockResolvedValue([LAB_CLUSTER]);
  vi.mocked(pveClustersApi.nodes).mockResolvedValue([NODE]);
  vi.mocked(pveClustersApi.storages).mockResolvedValue([STORAGE]);
}

describe("RecoveryLabPage — Application Groups", () => {
  it("lists existing groups and creates a new one with a member", async () => {
    mockCommonQueries();
    vi.mocked(applicationGroupsApi.list).mockResolvedValue([APPLICATION_GROUP]);
    vi.mocked(applicationGroupsApi.create).mockResolvedValue(APPLICATION_GROUP);
    vi.mocked(restoreTestPlansApi.list).mockResolvedValue([]);
    const user = userEvent.setup();
    renderWithProviders(<RecoveryLabPage />);

    await user.click(screen.getByRole("tab", { name: "Application Groups" }));
    await waitFor(() => expect(screen.getByText("Order Processing")).toBeInTheDocument());
    expect(screen.getByText("Acme AG")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "New Application Group" }));
    const modal = await screen.findByRole("dialog", { name: "New Application Group" });

    await user.selectOptions(within(modal).getByLabelText("Tenant"), "cust-1");
    await user.type(within(modal).getByLabelText("Name"), "Web Stack");
    await user.click(within(modal).getByRole("button", { name: "Add member" }));

    await waitFor(() => expect(within(modal).getByLabelText("Backup group")).toBeInTheDocument());
    await user.selectOptions(within(modal).getByLabelText("Backup group"), "group-vm");
    await user.click(within(modal).getByLabelText("Guest agent reachable"));

    await user.click(within(modal).getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(applicationGroupsApi.create).toHaveBeenCalledWith({
        customer_id: "cust-1",
        name: "Web Stack",
        members: [{ backup_group_id: "group-vm", startup_order: 0, check_types: ["guest_agent"], expected_ip: null }],
        lab_network_mode: "dhcp",
        lab_subnet_cidr: null,
        lab_gateway_ip: null,
        lab_dns_ip: null,
        lab_dhcp_range_start: null,
        lab_dhcp_range_end: null,
        lab_schedule_enabled: false,
        lab_schedule_interval_hours: null,
        lab_schedule_pve_cluster_id: null,
        lab_schedule_target_node: null,
        lab_schedule_target_storage: null,
      }),
    );
  });

  it("gates Save on a complete schedule target once the recurring schedule is enabled", async () => {
    mockCommonQueries();
    vi.mocked(applicationGroupsApi.list).mockResolvedValue([APPLICATION_GROUP]);
    vi.mocked(applicationGroupsApi.create).mockResolvedValue(APPLICATION_GROUP);
    vi.mocked(restoreTestPlansApi.list).mockResolvedValue([]);
    const user = userEvent.setup();
    renderWithProviders(<RecoveryLabPage />);

    await user.click(screen.getByRole("tab", { name: "Application Groups" }));
    await user.click(screen.getByRole("button", { name: "New Application Group" }));
    const modal = await screen.findByRole("dialog", { name: "New Application Group" });

    await user.selectOptions(within(modal).getByLabelText("Tenant"), "cust-1");
    await user.type(within(modal).getByLabelText("Name"), "Web Stack");
    await user.click(within(modal).getByRole("button", { name: "Add member" }));
    await waitFor(() => expect(within(modal).getByLabelText("Backup group")).toBeInTheDocument());
    await user.selectOptions(within(modal).getByLabelText("Backup group"), "group-vm");
    await user.click(within(modal).getByLabelText("Guest agent reachable"));

    const saveButton = within(modal).getByRole("button", { name: "Save" });
    await user.click(within(modal).getByLabelText("Run automatically on a schedule"));
    // Enabled, but no interval/cluster/node/storage chosen yet.
    expect(saveButton).toBeDisabled();

    await user.type(within(modal).getByLabelText("Interval (hours)"), "6");
    await user.selectOptions(within(modal).getByLabelText("Lab-enabled PVE cluster"), "pve-1");
    await waitFor(() => expect(within(modal).getByLabelText("Node")).toBeInTheDocument());
    await user.selectOptions(within(modal).getByLabelText("Node"), "pve-node-1");
    await waitFor(() => expect(within(modal).getByLabelText("Storage")).toBeInTheDocument());
    await user.selectOptions(within(modal).getByLabelText("Storage"), "local-lvm");

    expect(saveButton).not.toBeDisabled();
    await user.click(saveButton);

    await waitFor(() =>
      expect(applicationGroupsApi.create).toHaveBeenCalledWith(
        expect.objectContaining({
          lab_schedule_enabled: true,
          lab_schedule_interval_hours: 6,
          lab_schedule_pve_cluster_id: "pve-1",
          lab_schedule_target_node: "pve-node-1",
          lab_schedule_target_storage: "local-lvm",
        }),
      ),
    );
  });
});

describe("RecoveryLabPage — Lab Runs", () => {
  it("creates a run against a lab-enabled cluster and executes it", async () => {
    mockCommonQueries();
    vi.mocked(applicationGroupsApi.list).mockResolvedValue([APPLICATION_GROUP]);
    vi.mocked(restoreTestPlansApi.list).mockResolvedValue([]);
    vi.mocked(restoreTestPlansApi.create).mockResolvedValue(PLANNED_RUN);
    vi.mocked(restoreTestPlansApi.execute).mockResolvedValue({
      plan: { ...PLANNED_RUN, status: "running" },
      appliance_run_token: "plaintext-run-token",
    });
    const user = userEvent.setup();
    renderWithProviders(<RecoveryLabPage />);

    await user.click(screen.getByRole("button", { name: "New Lab run" }));
    await user.selectOptions(screen.getByLabelText("Application Group"), "app-group-1");
    await user.selectOptions(screen.getByLabelText("Lab-enabled PVE cluster"), "pve-1");
    await waitFor(() => expect(screen.getByLabelText("Node")).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Node"), "pve-node-1");
    await waitFor(() => expect(screen.getByLabelText("Storage")).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Storage"), "local-lvm");

    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(restoreTestPlansApi.create).toHaveBeenCalledWith({
        application_group_id: "app-group-1",
        pve_cluster_id: "pve-1",
        target_node: "pve-node-1",
        target_storage: "local-lvm",
      }),
    );

    const modal = await screen.findByRole("dialog", { name: "Order Processing" });
    await user.click(within(modal).getByRole("button", { name: "Execute" }));

    await waitFor(() => expect(screen.getByText("plaintext-run-token")).toBeInTheDocument());
    expect(restoreTestPlansApi.execute).toHaveBeenCalledWith("run-1");
  });

  it("shows a working state while cleanup runs, then a completion confirmation", async () => {
    // Regression test: cleanup now destroys real VMs before deleting the
    // vnet (raised directly by the user), which can take real time -- the
    // UI must show visible progress instead of just a silently disabled
    // button (raised directly by the user after a real cleanup completed
    // with no feedback at all).
    mockCommonQueries();
    const failedRun: RestoreTestPlanOut = { ...PLANNED_RUN, status: "failed" };
    vi.mocked(applicationGroupsApi.list).mockResolvedValue([APPLICATION_GROUP]);
    vi.mocked(restoreTestPlansApi.list).mockResolvedValue([failedRun]);

    let resolveCleanup: (value: RestoreTestPlanOut) => void = () => {};
    vi.mocked(restoreTestPlansApi.cleanup).mockReturnValue(
      new Promise((resolve) => {
        resolveCleanup = resolve;
      }),
    );

    const user = userEvent.setup();
    renderWithProviders(<RecoveryLabPage />);

    // Grouped Lab Runs view: a failed (non-active) run's group defaults
    // to collapsed, so its section must be expanded before its row is
    // reachable -- "Order Processing" is now the group header's own
    // toggle, not a cell inside the run row.
    await user.click(await screen.findByText("Order Processing"));
    await user.click(await screen.findByText("Failed"));
    const modal = await screen.findByRole("dialog", { name: "Order Processing" });
    await user.click(within(modal).getByRole("button", { name: "Clean up" }));

    expect(restoreTestPlansApi.cleanup).toHaveBeenCalledWith("run-1");
    // Both the inline status banner and the button itself say "Cleaning
    // up…" -- two independent signals, not just a silently disabled button.
    await waitFor(() => expect(within(modal).getAllByText("Cleaning up…")).toHaveLength(2));
    expect(within(modal).getByRole("button", { name: "Cleaning up…" })).toBeDisabled();

    resolveCleanup({ ...failedRun, status: "completed", error: null });

    expect(await within(modal).findByText("Cleanup complete.")).toBeInTheDocument();
    expect(within(modal).queryByText("Cleaning up…")).not.toBeInTheDocument();
  });

  it("groups runs by Application Group, expanded only where a run is active", async () => {
    // Raised directly by the user: the Lab Runs table should be grouped
    // (tree-like) under its owning Application Group rather than one
    // flat list.
    mockCommonQueries();
    vi.mocked(applicationGroupsApi.list).mockResolvedValue([APPLICATION_GROUP, OTHER_APPLICATION_GROUP]);
    const runningRun: RestoreTestPlanOut = {
      ...PLANNED_RUN, id: "run-2", application_group_id: "app-group-2", status: "running",
      created_at: "2026-01-01T00:00:00Z",
    };
    const failedRun: RestoreTestPlanOut = { ...PLANNED_RUN, id: "run-1", status: "failed" };
    vi.mocked(restoreTestPlansApi.list).mockResolvedValue([failedRun, runningRun]);
    renderWithProviders(<RecoveryLabPage />);

    await waitFor(() => expect(screen.getByText("Order Processing")).toBeInTheDocument());
    expect(screen.getByText("Database Tier")).toBeInTheDocument();
    expect(screen.getAllByText("1 run")).toHaveLength(2);

    // Database Tier has the active (running) run -- expanded by default,
    // its row visible without any click.
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();

    // Order Processing's only run is failed (not active) -- collapsed by
    // default, its row not present until the section is expanded.
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
  });
});
