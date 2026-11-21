import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type {
  BackupGroupOut,
  BackupSnapshotOut,
  CustomerOut,
  DatastoreOut,
  PveClusterOut,
  RestorePlanOut,
} from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { RestoreCenterPage } from "./RestoreCenterPage";

vi.mock("../api/resources", () => ({
  customersApi: { list: vi.fn() },
  datastoresApi: { list: vi.fn(), backupGroups: vi.fn() },
  inventoryApi: { snapshots: vi.fn() },
  pveClustersApi: { list: vi.fn(), nodes: vi.fn(), storages: vi.fn(), guests: vi.fn(), checkVmid: vi.fn() },
  restorePlansApi: {
    create: vi.fn(),
    list: vi.fn(),
    get: vi.fn(),
    recheck: vi.fn(),
    approve: vi.fn(),
    reject: vi.fn(),
    execute: vi.fn(),
    pollExecution: vi.fn(),
    moveInstantRecovery: vi.fn(),
    discardInstantRecovery: vi.fn(),
  },
}));

const { customersApi, datastoresApi, inventoryApi, pveClustersApi, restorePlansApi } =
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
  resolved_name: null,
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

const SNAPSHOT: BackupSnapshotOut = {
  id: "snap-1",
  backup_group_id: "group-vm",
  snapshot_time: "2026-01-02T00:00:00Z",
  verify_status: "ok",
  size_bytes: 1024,
  last_synced_at: "2026-01-02T00:00:00Z",
};

const PVE_CLUSTER: PveClusterOut = {
  id: "pve-1",
  display_name: "Primary PVE",
  host: "pve1.internal.example",
  port: 8006,
  tls_trust_mode: "ca",
  tls_fingerprint_sha256: null,
  allowed_target_storages: null,
  restore_network_rules: null,
  require_restore_approval: false,
  version: "9.2.11",
  last_health_check_at: null,
  last_health_status: "ok",
  disabled: false,
  linked_pbs_instance_ids: [],
  linked_datastore_ids: [],
  is_lab_compute: false,
  lab_sdn_zone_name: null,
  lab_appliance_template_vmid: null,
  lab_snippet_storage_id: null,
  lab_appliance_uplink_bridge: null,
};

const CREATED_PLAN: RestorePlanOut = {
  id: "plan-1",
  backup_snapshot_id: "snap-1",
  backup_group_id: "group-vm",
  customer_id: "cust-1",
  pve_cluster_id: "pve-1",
  target_node: "pve1",
  target_storage: "local-lvm",
  vmid_intent: "new",
  target_vmid: 200,
  temp_name: "restore-116-abc123",
  network_mode: "none",
  network_bridge: null,
  start_after_restore: false,
  network_danger_acknowledged: false,
  checks_result: [{ check: "vmid", status: "pass", message: "VMID 200 is free." }],
  checks_performed_at: "2026-01-03T00:00:00Z",
  generated_command: "qmrestore pbs-main:backup/vm/116/2026-01-02T00:00:00Z 200 --storage local-lvm --unique 1",
  created_by_user_id: "user-1",
  created_at: "2026-01-03T00:00:00Z",
  execution_status: null,
  upid: null,
  executed_at: null,
  execution_started_by_user_id: null,
  execution_exit_status: null,
  execution_completed_at: null,
  execution_error: null,
  execution_last_confirmed_at: null,
  approval_status: null,
  approved_by_user_id: null,
  approved_at: null,
  approval_notes: null,
};

function setup() {
  vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
  vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE]);
  vi.mocked(datastoresApi.backupGroups).mockResolvedValue([VM_GROUP]);
  vi.mocked(inventoryApi.snapshots).mockResolvedValue([SNAPSHOT]);
  vi.mocked(pveClustersApi.list).mockResolvedValue([PVE_CLUSTER]);
  vi.mocked(pveClustersApi.nodes).mockResolvedValue([{ node: "pve1", status: "online", cpu: null, maxmem: null, uptime: null }]);
  vi.mocked(pveClustersApi.storages).mockResolvedValue([
    { storage: "local-lvm", type: "lvmthin", content: ["images"], active: true, shared: null, avail_bytes: null, used_bytes: null, total_bytes: null, pbs_server: null, pbs_datastore: null, pbs_namespace: null },
  ]);
  vi.mocked(pveClustersApi.checkVmid).mockResolvedValue({ vmid: 200, free: true });
  vi.mocked(restorePlansApi.list).mockResolvedValue([]);
}

const IR_CLUSTER: PveClusterOut = {
  ...PVE_CLUSTER,
  id: "pve-ir",
  display_name: "Production PVE",
  instant_recovery_enabled: true,
  lab_agent_node: "pve1",
};

const RUNNING_PLAN: RestorePlanOut = {
  ...CREATED_PLAN,
  pve_cluster_id: "pve-ir",
  method: "instant",
  generated_command: "# Instant Recovery of vm/116 (ADR 0030)",
  execution_status: "running_from_backup",
  instant_recovery: {
    original_vm: { status: "running", cluster_name: "Production PVE", node: "pve2", unreachable_clusters: [] },
    keep_mac: false,
    disks: ["scsi0", "scsi1"],
    imported_volumes: ["tpmstate0"],
    skipped_devices: ["hostpci0"],
    running_since: "2026-01-03T00:05:00Z",
    last_heartbeat_at: new Date().toISOString(),
    overlay_bytes: 3 * 1024 ** 3,
    overlay_storage_total_bytes: 100 * 1024 ** 3,
    overlay_storage_free_bytes: 15 * 1024 ** 3,
    move_disks: [],
    move_bwlimit_kib: null,
    last_error: null,
    ended_at: null,
  },
};

async function goToStep1AndPickSnapshot(user: ReturnType<typeof userEvent.setup>) {
  await waitFor(() => expect(screen.getByRole("option", { name: "Acme AG" })).toBeInTheDocument());
  await user.selectOptions(screen.getByLabelText("Tenant"), "cust-1");
  await user.click(screen.getByLabelText("Backup group"));
  await waitFor(() => expect(screen.getByRole("option", { name: /VM 116/ })).toBeInTheDocument());
  await user.click(screen.getByRole("option", { name: /VM 116/ }));
  await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());
  await user.click(screen.getByText(new Date(SNAPSHOT.snapshot_time).toLocaleString()));
}

async function walkToCreatedPlan(user: ReturnType<typeof userEvent.setup>) {
  await goToStep1AndPickSnapshot(user);
  await user.click(screen.getByRole("button", { name: "Next" }));
  await fillTargetStep(user);
  await user.click(screen.getByRole("button", { name: "Next" }));
  await user.click(screen.getByRole("button", { name: "Next" }));
  await user.click(screen.getByRole("button", { name: "Next" }));
  await user.click(screen.getByRole("button", { name: "Create restore plan" }));
  await waitFor(() => expect(screen.getByText(/qmrestore/)).toBeInTheDocument());
}

async function fillTargetStep(user: ReturnType<typeof userEvent.setup>) {
  await waitFor(() => expect(screen.getByRole("option", { name: "Primary PVE" })).toBeInTheDocument());
  await user.selectOptions(screen.getByLabelText("PVE cluster"), "pve-1");
  await waitFor(() => expect(screen.getByRole("option", { name: "pve1" })).toBeInTheDocument());
  await user.selectOptions(screen.getByLabelText("Node"), "pve1");
  await waitFor(() => expect(screen.getByRole("option", { name: "local-lvm" })).toBeInTheDocument());
  await user.selectOptions(screen.getByLabelText("Storage"), "local-lvm");
  await user.type(screen.getByLabelText("Target VMID"), "200");
}

describe("RestoreCenterPage", () => {
  it("walks the full happy path and shows the generated command", async () => {
    setup();
    vi.mocked(restorePlansApi.create).mockResolvedValue(CREATED_PLAN);
    const user = userEvent.setup();
    renderWithProviders(<RestoreCenterPage />);

    await goToStep1AndPickSnapshot(user);
    await user.click(screen.getByRole("button", { name: "Next" }));

    await fillTargetStep(user);
    await user.click(screen.getByRole("button", { name: "Next" }));

    // network step: default "none" is fine, just advance
    await user.click(screen.getByRole("button", { name: "Next" }));

    // options step: nothing required, advance
    await user.click(screen.getByRole("button", { name: "Next" }));

    // review step: confirm
    await user.click(screen.getByRole("button", { name: "Create restore plan" }));

    await waitFor(() => expect(screen.getByText(/qmrestore/)).toBeInTheDocument());
    expect(restorePlansApi.create).toHaveBeenCalledWith(
      expect.objectContaining({ backup_snapshot_id: "snap-1", pve_cluster_id: "pve-1", target_vmid: 200 }),
    );
  });

  it("shows inline VMID-taken feedback as the operator types", async () => {
    setup();
    vi.mocked(pveClustersApi.checkVmid).mockResolvedValue({ vmid: 100, free: false });
    const user = userEvent.setup();
    renderWithProviders(<RestoreCenterPage />);

    await goToStep1AndPickSnapshot(user);
    await user.click(screen.getByRole("button", { name: "Next" }));

    await waitFor(() => expect(screen.getByRole("option", { name: "Primary PVE" })).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("PVE cluster"), "pve-1");
    await waitFor(() => expect(screen.getByRole("option", { name: "pve1" })).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Node"), "pve1");
    await waitFor(() => expect(screen.getByRole("option", { name: "local-lvm" })).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Storage"), "local-lvm");
    await user.type(screen.getByLabelText("Target VMID"), "100");

    await waitFor(() => expect(screen.getByText("Already in use")).toBeInTheDocument());
  });

  it("requires acknowledgment before creating a plan with the original network mode", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<RestoreCenterPage />);

    await goToStep1AndPickSnapshot(user);
    await user.click(screen.getByRole("button", { name: "Next" }));
    await fillTargetStep(user);
    await user.click(screen.getByRole("button", { name: "Next" }));

    await user.click(screen.getByLabelText("Keep the snapshot's original network"));
    expect(screen.getByText(/risks reconnecting to production networks/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));

    // Confirm button should be disabled until the risk is acknowledged.
    expect(screen.getByRole("button", { name: "Create restore plan" })).toBeDisabled();
    await user.click(screen.getByLabelText("I understand the network risk and want to proceed."));
    expect(screen.getByRole("button", { name: "Create restore plan" })).toBeEnabled();
  });

  it("shows the TTL/cleanup note as static text, not a toggle", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<RestoreCenterPage />);

    await goToStep1AndPickSnapshot(user);
    await user.click(screen.getByRole("button", { name: "Next" }));
    await fillTargetStep(user);
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" })); // network -> options

    expect(
      screen.getByText(/Time-limited access and automatic cleanup of the restored VM aren't implemented yet/),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText(/TTL/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/cleanup/i)).not.toBeInTheDocument();
  });

  it("shows a clear error when the server rejects the plan", async () => {
    setup();
    vi.mocked(restorePlansApi.create).mockRejectedValue(
      new ApiError(400, "restore_plan_checks_failed", "One or more checks failed.", undefined, {
        checks: [{ check: "vmid", status: "fail", message: "VMID 200 is already in use." }],
      }),
    );
    const user = userEvent.setup();
    renderWithProviders(<RestoreCenterPage />);

    await goToStep1AndPickSnapshot(user);
    await user.click(screen.getByRole("button", { name: "Next" }));
    await fillTargetStep(user);
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Create restore plan" }));

    await waitFor(() => expect(screen.getByText("One or more checks failed.")).toBeInTheDocument());
    expect(screen.getByText("VMID 200 is already in use.")).toBeInTheDocument();
  });

  it("lets long check messages wrap instead of overflowing the panel", async () => {
    setup();
    const longMessage =
      "PBS store not configured on this PVE node — a storage exists for datastore 'backup-store', " +
      "but none is scoped to namespace 'site-b/tenant-b'; please configure it in PVE before restoring here.";
    vi.mocked(restorePlansApi.create).mockRejectedValue(
      new ApiError(400, "restore_plan_checks_failed", "One or more checks failed.", undefined, {
        checks: [{ check: "pbs_storage_visibility", status: "fail", message: longMessage }],
      }),
    );
    const user = userEvent.setup();
    renderWithProviders(<RestoreCenterPage />);

    await goToStep1AndPickSnapshot(user);
    await user.click(screen.getByRole("button", { name: "Next" }));
    await fillTargetStep(user);
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Create restore plan" }));

    await waitFor(() => expect(screen.getByText(longMessage)).toBeInTheDocument());
    // regression: StatusBadge defaults to nowrap for its usual short-label
    // usage — this check message is a full sentence and must wrap instead
    // of overflowing the panel (see RestoreCenterPage.module.css's
    // .checkBadge override, passed via StatusBadge's className prop).
    const badge = screen.getByText(longMessage).closest("span");
    expect(badge?.className).toMatch(/checkBadge/);
  });

  it("shows the Execute button once a plan is created and needs no approval", async () => {
    setup();
    vi.mocked(restorePlansApi.create).mockResolvedValue(CREATED_PLAN);
    const user = userEvent.setup();
    renderWithProviders(<RestoreCenterPage />);

    await walkToCreatedPlan(user);

    expect(screen.getByRole("button", { name: "Execute restore" })).toBeEnabled();
    expect(screen.queryByText("This plan requires approval before it can be executed.")).not.toBeInTheDocument();
  });

  it("shows an approval banner and blocks execution until approved", async () => {
    setup();
    const pendingPlan = { ...CREATED_PLAN, approval_status: "pending" as const };
    vi.mocked(restorePlansApi.create).mockResolvedValue(pendingPlan);
    vi.mocked(restorePlansApi.approve).mockResolvedValue({ ...pendingPlan, approval_status: "approved" as const });
    const user = userEvent.setup();
    renderWithProviders(<RestoreCenterPage />);

    await walkToCreatedPlan(user);

    expect(screen.getByText("This plan requires approval before it can be executed.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Execute restore" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(restorePlansApi.approve).toHaveBeenCalledWith("plan-1"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Execute restore" })).toBeEnabled());
  });

  it("requires typing the exact VMID before executing an overwrite of an existing VMID", async () => {
    setup();
    const overwritePlan = { ...CREATED_PLAN, vmid_intent: "existing" as const, target_vmid: 100 };
    vi.mocked(restorePlansApi.create).mockResolvedValue(overwritePlan);
    const user = userEvent.setup();
    renderWithProviders(<RestoreCenterPage />);

    await goToStep1AndPickSnapshot(user);
    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(screen.getByRole("option", { name: "Primary PVE" })).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("PVE cluster"), "pve-1");
    await waitFor(() => expect(screen.getByRole("option", { name: "pve1" })).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Node"), "pve1");
    await waitFor(() => expect(screen.getByRole("option", { name: "local-lvm" })).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Storage"), "local-lvm");
    await user.click(screen.getByLabelText("Restore into an existing VMID"));
    await user.type(screen.getByLabelText("Target VMID"), "100");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Create restore plan" }));
    await waitFor(() => expect(screen.getByText(/qmrestore/)).toBeInTheDocument());

    const executeButton = screen.getByRole("button", { name: "Execute restore" });
    expect(executeButton).toBeDisabled();

    const confirmInput = screen.getByLabelText(/This will overwrite and destroy VMID 100's current disk/);
    await user.type(confirmInput, "99");
    expect(executeButton).toBeDisabled();

    await user.clear(confirmInput);
    await user.type(confirmInput, "100");
    expect(executeButton).toBeEnabled();
  });

  it("executes the plan and shows progress until completion", async () => {
    setup();
    vi.mocked(restorePlansApi.create).mockResolvedValue(CREATED_PLAN);
    vi.mocked(restorePlansApi.execute).mockResolvedValue({
      ...CREATED_PLAN,
      execution_status: "restore_requested",
      upid: "UPID:pve1:0000ABCD:qmrestore:200",
    });
    vi.mocked(restorePlansApi.pollExecution).mockResolvedValue({
      ...CREATED_PLAN,
      execution_status: "completed",
      upid: "UPID:pve1:0000ABCD:qmrestore:200",
    });
    const user = userEvent.setup();
    renderWithProviders(<RestoreCenterPage />);

    await walkToCreatedPlan(user);
    await user.click(screen.getByRole("button", { name: "Execute restore" }));

    await waitFor(() => expect(restorePlansApi.execute).toHaveBeenCalledWith("plan-1"));
    await waitFor(() => expect(screen.getByText("Completed.")).toBeInTheDocument());
    expect(screen.getByText("The restore completed successfully.")).toBeInTheDocument();
  });

  it("creates and starts an Instant Recovery, then moves it to storage", async () => {
    setup();
    vi.mocked(pveClustersApi.list).mockResolvedValue([PVE_CLUSTER, IR_CLUSTER]);
    vi.mocked(restorePlansApi.create).mockResolvedValue({ ...RUNNING_PLAN, execution_status: null });
    vi.mocked(restorePlansApi.execute).mockResolvedValue(RUNNING_PLAN);
    vi.mocked(restorePlansApi.pollExecution).mockResolvedValue(RUNNING_PLAN);
    vi.mocked(restorePlansApi.moveInstantRecovery).mockResolvedValue({ ...RUNNING_PLAN, execution_status: "moving_disks" });
    const user = userEvent.setup();
    renderWithProviders(<RestoreCenterPage />);

    await goToStep1AndPickSnapshot(user);
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByLabelText(/Instant Recovery — run the VM from the backup now/));
    // Only clusters that allow Instant Recovery; the agent's node is preselected.
    await waitFor(() => expect(screen.getByRole("option", { name: "Production PVE" })).toBeInTheDocument());
    expect(screen.queryByRole("option", { name: "Primary PVE" })).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("PVE cluster"), "pve-ir");
    await waitFor(() => expect(screen.getByLabelText("Node")).toHaveValue("pve1"));
    expect(screen.getByText(/runs on pve1, where the cluster's agent runs/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("option", { name: "local-lvm" })).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Storage"), "local-lvm");
    expect(screen.queryByText("Create a new VM")).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Target VMID"), "200");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText(/always starts as soon as its disks are mapped/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Create restore plan" }));

    await waitFor(() =>
      expect(restorePlansApi.create).toHaveBeenCalledWith(
        expect.objectContaining({ method: "instant", vmid_intent: "new", start_after_restore: false, target_node: "pve1" }),
      ),
    );
    await user.click(await screen.findByRole("button", { name: "Start Instant Recovery" }));

    expect(await screen.findByText("Running from the backup")).toBeInTheDocument();
    expect(screen.getByText(/85 % used/)).toBeInTheDocument();
    expect(screen.getByText(/new \(the original VM runs/)).toBeInTheDocument();
    expect(screen.getByText(/hostpci0/)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Bandwidth limit (MiB/s, optional)"), "50");
    await user.click(screen.getByRole("button", { name: "Move to local-lvm" }));
    await waitFor(() => expect(restorePlansApi.moveInstantRecovery).toHaveBeenCalledWith("plan-1", 51200));
  });

  it("lists VMs still running from backups and discards one after confirmation", async () => {
    setup();
    vi.mocked(restorePlansApi.list).mockResolvedValue([
      { ...RUNNING_PLAN, id: "plan-running" },
      { ...RUNNING_PLAN, id: "plan-done", execution_status: "completed" },
      { ...CREATED_PLAN, id: "plan-full", execution_status: "restore_running" },
    ]);
    vi.mocked(restorePlansApi.discardInstantRecovery).mockResolvedValue({ ...RUNNING_PLAN, execution_status: "discarding" });
    const user = userEvent.setup();
    renderWithProviders(<RestoreCenterPage />);

    const list = await screen.findByTestId("restore-center-instant-recoveries");
    expect(list.querySelectorAll('[data-testid="instant-recovery-panel"]')).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Discard" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(/VM 200 is stopped and deleted/);
    await user.click(within(dialog).getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(restorePlansApi.discardInstantRecovery).toHaveBeenCalledWith("plan-running"));
  });
});
