import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { PveClusterOut, VzdumpJobOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { PveClustersPage } from "./PveClustersPage";

vi.mock("../api/resources", () => ({
  pveClustersApi: {
    list: vi.fn(),
    backupJobs: vi.fn(),
    backupJobCapabilities: vi.fn(),
    enableBackupJob: vi.fn(),
    disableBackupJob: vi.fn(),
    cancelBackupJobReenable: vi.fn(),
    setBackupJobReenable: vi.fn(),
    createBackupJob: vi.fn(),
    updateBackupJob: vi.fn(),
    deleteBackupJob: vi.fn(),
    assignBackupJobCustomer: vi.fn(),
    unassignBackupJobCustomer: vi.fn(),
    nodes: vi.fn(),
    storages: vi.fn(),
    rotateLabAgentToken: vi.fn(),
    labReadiness: vi.fn(),
    sdnZones: vi.fn(),
    bridges: vi.fn(),
    storageConfig: vi.fn(),
    templates: vi.fn(),
    labAgentInstall: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    testConnection: vi.fn(),
    requiredPrivileges: vi.fn(),
    probeTls: vi.fn(),
  },
  pbsInstancesApi: {
    list: vi.fn(),
  },
  datastoresApi: {
    list: vi.fn(),
  },
  pbsJobsApi: {
    pruneJobs: vi.fn(),
  },
  customersApi: {
    list: vi.fn(),
  },
}));

const { pveClustersApi, pbsInstancesApi, datastoresApi, customersApi } = await import("../api/resources");

const SAMPLE_CLUSTER: PveClusterOut = {
  id: "22222222-2222-2222-2222-222222222222",
  display_name: "Primary PVE",
  host: "pve1.internal.example",
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

const ENABLED_JOB: VzdumpJobOut = {
  id: "backup-abc123",
  enabled: true,
  schedule: "02:30",
  storage: "pbs-main",
  node: null,
  comment: "Daily All",
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
};

describe("PveClustersPage backup jobs", () => {
  it("lists the cluster's vzdump jobs when the detail modal is opened", async () => {
    vi.mocked(pveClustersApi.list).mockResolvedValue([SAMPLE_CLUSTER]);
    vi.mocked(pbsInstancesApi.list).mockResolvedValue([]);
    vi.mocked(datastoresApi.list).mockResolvedValue([]);
    vi.mocked(pveClustersApi.backupJobCapabilities).mockResolvedValue({ can_read: true, can_write: true });
    vi.mocked(customersApi.list).mockResolvedValue([]);
    vi.mocked(pveClustersApi.backupJobs).mockResolvedValue([ENABLED_JOB]);
    const user = userEvent.setup();
    renderWithProviders(<PveClustersPage />);

    await waitFor(() => expect(screen.getByText("Primary PVE")).toBeInTheDocument());
    await user.click(screen.getByText("Primary PVE"));

    await waitFor(() => expect(screen.getByText("02:30")).toBeInTheDocument());
    expect(screen.getByText("pbs-main")).toBeInTheDocument();
    expect(pveClustersApi.backupJobs).toHaveBeenCalledWith(SAMPLE_CLUSTER.id);
  });

  it("disables an enabled job through the confirm dialog", async () => {
    vi.mocked(pveClustersApi.list).mockResolvedValue([SAMPLE_CLUSTER]);
    vi.mocked(pbsInstancesApi.list).mockResolvedValue([]);
    vi.mocked(datastoresApi.list).mockResolvedValue([]);
    vi.mocked(pveClustersApi.backupJobCapabilities).mockResolvedValue({ can_read: true, can_write: true });
    vi.mocked(customersApi.list).mockResolvedValue([]);
    vi.mocked(pveClustersApi.backupJobs).mockResolvedValue([ENABLED_JOB]);
    vi.mocked(pveClustersApi.disableBackupJob).mockResolvedValue({ ...ENABLED_JOB, enabled: false });
    const user = userEvent.setup();
    renderWithProviders(<PveClustersPage />);

    await waitFor(() => expect(screen.getByText("Primary PVE")).toBeInTheDocument());
    await user.click(screen.getByText("Primary PVE"));
    await waitFor(() => expect(screen.getByText("02:30")).toBeInTheDocument());

    const detailModal = screen.getByRole("dialog", { name: "Primary PVE" });
    await user.click(within(detailModal).getByRole("button", { name: "Disable" }));
    const confirmDialog = await screen.findByRole("dialog", { name: "Disable backup job" });
    await user.click(within(confirmDialog).getByRole("button", { name: "Disable" }));

    await waitFor(() => expect(pveClustersApi.disableBackupJob).toHaveBeenCalledWith(SAMPLE_CLUSTER.id, "backup-abc123", null));
  });

  it("shows a one-time lab-agent token after rotating it on a lab-enabled cluster", async () => {
    const labCluster: PveClusterOut = { ...SAMPLE_CLUSTER, is_lab_compute: true, lab_appliance_template_vmid: 9000 };
    vi.mocked(pveClustersApi.list).mockResolvedValue([labCluster]);
    vi.mocked(pbsInstancesApi.list).mockResolvedValue([]);
    vi.mocked(datastoresApi.list).mockResolvedValue([]);
    vi.mocked(pveClustersApi.backupJobCapabilities).mockResolvedValue({ can_read: true, can_write: true });
    vi.mocked(customersApi.list).mockResolvedValue([]);
    vi.mocked(pveClustersApi.backupJobs).mockResolvedValue([]);
    vi.mocked(pveClustersApi.rotateLabAgentToken).mockResolvedValue({ token: "plaintext-agent-token" });
    const user = userEvent.setup();
    renderWithProviders(<PveClustersPage />);

    await waitFor(() => expect(screen.getByText("Primary PVE")).toBeInTheDocument());
    await user.click(screen.getByText("Primary PVE"));

    const detailModal = await screen.findByRole("dialog", { name: "Primary PVE" });
    await user.click(within(detailModal).getByRole("button", { name: "Generate / rotate token" }));

    await waitFor(() => expect(within(detailModal).getByText("plaintext-agent-token")).toBeInTheDocument());
    expect(pveClustersApi.rotateLabAgentToken).toHaveBeenCalledWith(labCluster.id);
  });

  it("assigns and unassigns a customer-scope override on a job", async () => {
    vi.mocked(pveClustersApi.list).mockResolvedValue([SAMPLE_CLUSTER]);
    vi.mocked(pbsInstancesApi.list).mockResolvedValue([]);
    vi.mocked(datastoresApi.list).mockResolvedValue([]);
    vi.mocked(pveClustersApi.backupJobCapabilities).mockResolvedValue({ can_read: true, can_write: true });
    vi.mocked(customersApi.list).mockResolvedValue([
      { id: "cust-1", name: "Acme AG", slug: "acme", debitor_number: "1000", match_code: "ACM", disabled: false, created_at: "2026-01-01T00:00:00Z", report_retention_days: null },
    ]);
    vi.mocked(pveClustersApi.backupJobs).mockResolvedValue([ENABLED_JOB]);
    vi.mocked(pveClustersApi.assignBackupJobCustomer).mockResolvedValue({ ...ENABLED_JOB, customer_id: "cust-1" });
    const user = userEvent.setup();
    renderWithProviders(<PveClustersPage />);

    await waitFor(() => expect(screen.getByText("Primary PVE")).toBeInTheDocument());
    await user.click(screen.getByText("Primary PVE"));
    await waitFor(() => expect(screen.getByText("02:30")).toBeInTheDocument());

    const detailModal = screen.getByRole("dialog", { name: "Primary PVE" });
    await user.selectOptions(within(detailModal).getByDisplayValue("Inherit from datastore"), "cust-1");

    await waitFor(() =>
      expect(pveClustersApi.assignBackupJobCustomer).toHaveBeenCalledWith(SAMPLE_CLUSTER.id, "backup-abc123", "cust-1"),
    );
  });

  it("creates with the PVE token fields, then shows the automatic test result", async () => {
    vi.mocked(pveClustersApi.list).mockResolvedValue([]);
    vi.mocked(pveClustersApi.requiredPrivileges).mockResolvedValue({
      privileges: [{ name: "VM.GuestAgent.Audit", path: "/vms", reason: "Recovery Lab level-1 checks." }],
      roles: ["RESURGAM"],
      manual_commands: ['pveum role add RESURGAM --privs "VM.GuestAgent.Audit"'],
      default_token_name: "resurgam",
    });
    vi.mocked(pveClustersApi.create).mockResolvedValue(SAMPLE_CLUSTER);
    vi.mocked(pveClustersApi.testConnection).mockResolvedValue({
      reachable: false,
      status: "auth_failed",
      version: null,
      error: "PVE rejected the API token (HTTP 401): token ID or secret rejected.",
      missing_privileges: [],
      permissions_checked: false,
      resolved_ip: "192.0.2.10",
      checked_at: "2026-10-06T10:00:00Z",
    });
    const user = userEvent.setup();
    renderWithProviders(<PveClustersPage />);
    await user.click(await screen.findByRole("button", { name: "Create" }));
    await user.type(screen.getByLabelText("Display name"), "PVE 1");
    await user.type(screen.getAllByLabelText("Host")[0], "pve.example");
    const port = screen.getByLabelText("Port");
    await user.clear(port);
    expect(port).toHaveValue(null);
    await user.type(port, "8006");
    await user.type(screen.getByLabelText("Token ID"), "root@pam!resurgam");
    await user.type(screen.getByLabelText("Token secret"), "uuid-secret");
    await user.click(screen.getByText("Required permissions"));
    expect(await screen.findByText("VM.GuestAgent.Audit")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(pveClustersApi.create).toHaveBeenCalled());
    expect(vi.mocked(pveClustersApi.create).mock.calls[0][0]).toMatchObject({
      port: 8006,
      token_id: "root@pam!resurgam",
      token_secret: "uuid-secret",
    });
    await waitFor(() => expect(pveClustersApi.testConnection).toHaveBeenCalledWith(SAMPLE_CLUSTER.id));
    expect(await screen.findByText(/token ID or secret rejected/)).toBeInTheDocument();
    expect(screen.getByText("Token rejected")).toBeInTheDocument();
  });

  it("shows the stored token ID and the last heartbeat in the detail modal", async () => {
    vi.mocked(pveClustersApi.list).mockResolvedValue([
      { ...SAMPLE_CLUSTER, token_id: "root@pam!resurgam", last_health_check_at: new Date().toISOString() },
    ]);
    vi.mocked(pbsInstancesApi.list).mockResolvedValue([]);
    vi.mocked(datastoresApi.list).mockResolvedValue([]);
    vi.mocked(pveClustersApi.backupJobs).mockResolvedValue([]);
    vi.mocked(pveClustersApi.backupJobCapabilities).mockResolvedValue({ can_read: true, can_write: true });
    vi.mocked(customersApi.list).mockResolvedValue([]);
    const user = userEvent.setup();
    renderWithProviders(<PveClustersPage />);
    await user.click(await screen.findByText("Primary PVE"));
    expect(await screen.findByText("root@pam!resurgam")).toBeInTheDocument();
    expect(screen.getAllByText(/Last heartbeat/).length).toBeGreaterThan(0);
  });
});

describe("PveClustersPage lab compute setup", () => {
  const readyChecklist = {
    cluster_id: SAMPLE_CLUSTER.id,
    ready: false,
    lab_nodes: ["pve1"],
    lab_nodes_source: "all_online_nodes" as const,
    overlay_storage_id: "resurgam-lab-overlays",
    checked_at: "2026-10-06T10:00:00Z",
    items: [
      { key: "privileges" as const, status: "ok" as const, code: "all_granted", message: "", fix_hint: null, auto_fix: null, details: {} },
      { key: "lab_agent" as const, status: "missing" as const, code: "never_seen", message: "", fix_hint: null, auto_fix: null, details: {} },
    ],
  };

  it("shows the lab readiness pill for lab clusters in the list", async () => {
    vi.mocked(pveClustersApi.list).mockResolvedValue([
      { ...SAMPLE_CLUSTER, is_lab_compute: true },
      { ...SAMPLE_CLUSTER, id: "33333333-3333-3333-3333-333333333333", display_name: "Other PVE" },
    ]);
    vi.mocked(pveClustersApi.labReadiness).mockResolvedValue(readyChecklist);
    renderWithProviders(<PveClustersPage />);

    expect(await screen.findByText("Lab incomplete")).toBeInTheDocument();
    expect(pveClustersApi.labReadiness).toHaveBeenCalledTimes(1);
    expect(pveClustersApi.labReadiness).toHaveBeenCalledWith(SAMPLE_CLUSTER.id);
  });

  it("enables the setup button only for connected clusters and opens the guided flow", async () => {
    vi.mocked(pveClustersApi.list).mockResolvedValue([
      SAMPLE_CLUSTER,
      { ...SAMPLE_CLUSTER, id: "44444444-4444-4444-4444-444444444444", display_name: "Down PVE", last_health_status: "unreachable" },
    ]);
    vi.mocked(pveClustersApi.labReadiness).mockResolvedValue(readyChecklist);
    vi.mocked(pveClustersApi.nodes).mockResolvedValue([{ node: "pve1", status: "online", cpu: null, maxmem: null, uptime: null }]);
    const user = userEvent.setup();
    renderWithProviders(<PveClustersPage />);

    await waitFor(() => expect(screen.getByText("Down PVE")).toBeInTheDocument());
    const buttons = screen.getAllByRole("button", { name: "Set up lab compute" });
    expect(buttons[0]).toBeEnabled();
    expect(buttons[1]).toBeDisabled();

    await user.click(buttons[0]);
    expect(await screen.findByRole("dialog", { name: "Set up lab compute: Primary PVE" })).toBeInTheDocument();
    await waitFor(() => expect(pveClustersApi.labReadiness).toHaveBeenCalled());
  });
});
