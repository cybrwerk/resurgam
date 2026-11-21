import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  BackupGroupOut,
  BackupSnapshotOut,
  CustomerOut,
  DatastoreOut,
  DiskMountOut,
  DiskMountPlacementOut,
  WhoAmIResponse,
} from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { BreakablePath, DiskMountsPage, parseClients, remainingTime } from "./DiskMountsPage";

vi.mock("../api/resources", () => ({
  authApi: { whoami: vi.fn() },
  customersApi: { list: vi.fn() },
  datastoresApi: { list: vi.fn(), backupGroups: vi.fn() },
  inventoryApi: { snapshots: vi.fn() },
  pveClustersApi: {
    labSshTarget: vi.fn(),
    labSshHostKey: vi.fn(),
    labSetupJob: vi.fn(),
    startLabAgentJob: vi.fn(),
    storageConfig: vi.fn(),
  },
  diskMountsApi: {
    list: vi.fn(),
    get: vi.fn(),
    create: vi.fn(),
    approve: vi.fn(),
    reject: vi.fn(),
    extend: vi.fn(),
    stop: vi.fn(),
    forceFinish: vi.fn(),
    snapshotDisks: vi.fn(),
    placement: vi.fn(),
    nodes: vi.fn(),
    bridges: vi.fn(),
    setHelperTemplate: vi.fn(),
    startHelperTemplateBuild: vi.fn(),
  },
}));

const { authApi, customersApi, datastoresApi, inventoryApi, diskMountsApi, pveClustersApi } = await import("../api/resources");

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
  resolved_name: "fileserver",
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
  snapshot_time: "2026-09-14T00:48:57Z",
  verify_status: "ok",
  size_bytes: 1024,
  last_synced_at: "2026-09-14T01:00:00Z",
};
const PLACEMENT: DiskMountPlacementOut = {
  pve_cluster_id: "pve-1",
  display_name: "Lab Cluster",
  ready: true,
  problems: [],
  require_approval: false,
  helper_template_vmid: 9100,
  lab_agent_last_seen_at: "2026-10-08T10:00:00Z",
  uplink_bridge: "vmbr0",
  uplink_vlan_tag: null,
  uplink_static_range: false,
};

function mount(overrides: Partial<DiskMountOut> = {}): DiskMountOut {
  return {
    id: "dm-1",
    status: "ready",
    teardown_reason: null,
    customer_id: "cust-1",
    customer_name: "Acme AG",
    backup_group_id: "group-vm",
    backup_snapshot_id: "snap-1",
    snapshot_time: "2026-09-14T00:48:57Z",
    workload_label: "vm/fileserver (116)",
    disk_archives: ["drive-scsi0.img.fidx"],
    pve_cluster_id: "pve-1",
    pve_cluster_name: "Lab Cluster",
    target_node: "pve1",
    bridge: "vmbr0",
    vlan_tag: 20,
    ip_mode: "static",
    ip_address_cidr: "192.168.20.50/24",
    gateway: null,
    protocols: ["smb", "nfs"],
    allowed_clients: ["192.168.20.0/24"],
    smb_username: "rsgm-1a2b3c4d",
    lifetime_hours: 4,
    created_at: "2026-10-08T09:00:00Z",
    created_by_user_id: "u-other",
    approval_required: false,
    approved_by_user_id: null,
    approved_at: null,
    approval_notes: null,
    ready_at: "2026-10-08T09:05:00Z",
    expires_at: new Date(Date.now() + (3 * 60 + 12) * 60000 + 30000).toISOString(),
    ended_at: null,
    error: null,
    helper_vmid: 4242,
    volumes: [
      {
        disk_index: 1,
        archive: "drive-scsi0.img.fidx",
        partition: "part2",
        fstype: "ntfs",
        size_bytes: 1024,
        name: "disk1-part2-ntfs",
        mounted: true,
        reason: null,
        detail: null,
      },
      {
        disk_index: 1,
        archive: "drive-scsi0.img.fidx",
        partition: "part3",
        fstype: "BitLocker",
        size_bytes: 2048,
        name: null,
        mounted: false,
        reason: "encrypted",
        detail: null,
      },
    ],
    connection: {
      host: "192.168.20.50",
      smb_unc: "\\\\192.168.20.50\\restore",
      smb_url: "smb://192.168.20.50/restore",
      smb_username: "rsgm-1a2b3c4d",
      nfs_exports: ["192.168.20.50:/srv/resurgam/disk1-part2-ntfs"],
    },
    ...overrides,
  };
}

function loginAs(roles: string[] = ["restore_operator"], userId = "u-me") {
  const user: WhoAmIResponse = {
    user_id: userId,
    email: "me@example.com",
    display_name: "Me",
    is_breakglass: false,
    assignments: roles.map((role) => ({ role, scope_type: "global", scope_id: null })),
  } as WhoAmIResponse;
  window.localStorage.setItem("resurgam.token", "fake-token");
  vi.mocked(authApi.whoami).mockResolvedValue(user);
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(diskMountsApi.placement).mockResolvedValue([PLACEMENT]);
  vi.mocked(diskMountsApi.list).mockResolvedValue([]);
  vi.mocked(pveClustersApi.storageConfig).mockResolvedValue([]);
});

describe("DiskMountsPage helpers", () => {
  it("computes the remaining lifetime", () => {
    const now = Date.parse("2026-10-08T10:00:00Z");
    expect(remainingTime("2026-10-08T13:12:30Z", now)).toEqual({ hours: 3, minutes: 12 });
    expect(remainingTime("2026-10-08T09:00:00Z", now)).toBeNull();
    expect(remainingTime(null, now)).toBeNull();
  });

  it("lets paths wrap only after a backslash or slash", () => {
    const { container } = renderWithProviders(<BreakablePath text={"\\\\10.0.0.5\\restore"} />);
    expect(container.querySelectorAll("wbr")).toHaveLength(3);
    expect(container.textContent).toBe("\\\\10.0.0.5\\restore");
    const nfs = renderWithProviders(<BreakablePath text="10.0.0.5:/srv/resurgam/disk1" />);
    expect(nfs.container.querySelectorAll("wbr")).toHaveLength(3);
  });

  it("splits the client list", () => {
    expect(parseClients("192.168.1.5\n10.0.0.0/24, 10.1.1.1 ;")).toEqual(["192.168.1.5", "10.0.0.0/24", "10.1.1.1"]);
  });
});

describe("DiskMountsPage", () => {
  it("lists an active mount with status, remaining time, connection info and the volume report", async () => {
    loginAs();
    vi.mocked(diskMountsApi.list).mockResolvedValue([
      mount(),
      mount({ id: "dm-2", status: "expired", ended_at: "2026-10-07T12:00:00Z", connection: { ...mount().connection, host: null } }),
    ]);
    const user = userEvent.setup();
    renderWithProviders(<DiskMountsPage />);

    const active = await screen.findByTestId("disk-mounts-active");
    expect(within(active).getByText("Ready")).toBeInTheDocument();
    expect(within(active).getByText("3 h 12 min")).toBeInTheDocument();
    expect(within(active).getByText("\\\\192.168.20.50\\restore")).toBeInTheDocument();
    expect(within(active).getByText("192.168.20.50:/srv/resurgam/disk1-part2-ntfs")).toBeInTheDocument();
    expect(within(active).getByRole("button", { name: "Copy SMB path" })).toBeInTheDocument();
    expect(within(active).getByRole("button", { name: "Copy NFS export" })).toBeInTheDocument();
    expect(screen.getAllByText("Ended").length).toBeGreaterThan(0);
    expect(screen.getByText("Expired")).toBeInTheDocument();

    await user.click(within(active).getByText("vm/fileserver (116)"));
    const volumes = await screen.findByTestId("disk-mount-volumes");
    expect(within(volumes).getByText("Read-only: disk1-part2-ntfs")).toBeInTheDocument();
    expect(within(volumes).getByText("Encrypted (BitLocker/LUKS) -- not mounted")).toBeInTheDocument();
  });

  it("stops only after the in-page confirmation", async () => {
    loginAs();
    vi.mocked(diskMountsApi.list).mockResolvedValue([mount()]);
    vi.mocked(diskMountsApi.stop).mockResolvedValue(mount({ status: "stopping" }));
    const user = userEvent.setup();
    renderWithProviders(<DiskMountsPage />);

    await user.click(await screen.findByRole("button", { name: "Stop" }));
    expect(diskMountsApi.stop).not.toHaveBeenCalled();
    expect(screen.getByText(/Stop the share now\?/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText(/Stop the share now\?/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Stop" }));
    await user.click(screen.getByRole("button", { name: "Stop and remove" }));
    await waitFor(() => expect(diskMountsApi.stop).toHaveBeenCalledWith("dm-1"));
  });

  it("extends a ready mount", async () => {
    loginAs();
    vi.mocked(diskMountsApi.list).mockResolvedValue([mount()]);
    vi.mocked(diskMountsApi.extend).mockResolvedValue(mount());
    const user = userEvent.setup();
    renderWithProviders(<DiskMountsPage />);
    await user.selectOptions(await screen.findByLabelText("Extend by"), "24");
    await user.click(screen.getByRole("button", { name: "Extend" }));
    await waitFor(() => expect(diskMountsApi.extend).toHaveBeenCalledWith("dm-1", 24));
  });

  it("offers approval only to someone other than the requester", async () => {
    loginAs(["restore_operator"], "u-me");
    vi.mocked(diskMountsApi.list).mockResolvedValue([
      mount({ id: "dm-mine", status: "pending_approval", created_by_user_id: "u-me", connection: { ...mount().connection, host: null } }),
      mount({ id: "dm-theirs", status: "pending_approval", created_by_user_id: "u-other", workload_label: "vm/other (7)", connection: { ...mount().connection, host: null } }),
    ]);
    vi.mocked(diskMountsApi.approve).mockResolvedValue(mount({ status: "queued" }));
    const user = userEvent.setup();
    renderWithProviders(<DiskMountsPage />);
    expect(await screen.findByText("Another operator has to approve it.")).toBeInTheDocument();
    const approveButtons = await screen.findAllByRole("button", { name: "Approve" });
    expect(approveButtons).toHaveLength(1);
    await user.click(approveButtons[0]);
    await waitFor(() => expect(diskMountsApi.approve).toHaveBeenCalledWith("dm-theirs"));
  });

  it("explains when no cluster is ready and disables New", async () => {
    loginAs();
    vi.mocked(diskMountsApi.placement).mockResolvedValue([
      { ...PLACEMENT, ready: false, problems: ["helper_template_missing"] },
    ]);
    renderWithProviders(<DiskMountsPage />);
    expect(await screen.findByText(/No lab cluster is set up for disk mounts yet/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New disk mount" })).toBeDisabled();
  });

  it("walks through the new disk mount wizard and shows the password once", async () => {
    loginAs();
    vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
    vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE]);
    vi.mocked(datastoresApi.backupGroups).mockResolvedValue([VM_GROUP]);
    vi.mocked(inventoryApi.snapshots).mockResolvedValue([SNAPSHOT]);
    vi.mocked(diskMountsApi.snapshotDisks).mockResolvedValue([
      { archive: "drive-scsi0.img.fidx", slot: "scsi0", size_bytes: 32 * 2 ** 30, crypt_mode: "none" },
      { archive: "drive-scsi1.img.fidx", slot: "scsi1", size_bytes: 2 ** 30, crypt_mode: "none" },
    ]);
    vi.mocked(diskMountsApi.nodes).mockResolvedValue(["pve1"]);
    vi.mocked(diskMountsApi.bridges).mockResolvedValue([
      { iface: "vmbr0", type: "bridge", active: true, vlan_aware: true, comments: null },
    ]);
    vi.mocked(diskMountsApi.create).mockResolvedValue({
      mount: mount({ status: "queued", connection: { ...mount().connection, host: null } }),
      share_password: "Gx7-very-secret-pw-QZ01",
    });
    const user = userEvent.setup();
    renderWithProviders(<DiskMountsPage />);

    await user.click(await screen.findByRole("button", { name: "New disk mount" }));
    const wizard = screen.getByTestId("disk-mount-wizard");
    // Step 1: tenant -> VM backup -> snapshot.
    await waitFor(() => expect(screen.getByRole("option", { name: "Acme AG" })).toBeInTheDocument());
    await user.selectOptions(within(wizard).getByLabelText("Tenant"), "cust-1");
    await user.click(within(wizard).getByLabelText("Backup group"));
    await user.click(await screen.findByRole("option", { name: /VM fileserver/ }));
    await user.click(await screen.findByText(new Date(SNAPSHOT.snapshot_time).toLocaleString()));
    await user.click(within(wizard).getByRole("button", { name: "Next" }));
    // Step 2: disks.
    await user.click(await within(wizard).findByRole("checkbox", { name: /scsi1/ }));
    await user.click(within(wizard).getByRole("button", { name: "Next" }));
    // Step 3: target + network.
    await user.selectOptions(within(wizard).getByLabelText("Lab cluster"), "pve-1");
    await waitFor(() => expect(within(wizard).getByRole("option", { name: "pve1" })).toBeInTheDocument());
    await user.selectOptions(within(wizard).getByLabelText("Node"), "pve1");
    await waitFor(() => expect(within(wizard).getByRole("option", { name: "vmbr0" })).toBeInTheDocument());
    await user.selectOptions(within(wizard).getByLabelText("Bridge"), "vmbr0");
    await user.type(within(wizard).getByLabelText("VLAN tag"), "20");
    await user.click(within(wizard).getByRole("button", { name: "Next" }));
    // Step 4: NFS needs a client restriction before Next is possible.
    await user.click(within(wizard).getByRole("checkbox", { name: /NFS/ }));
    expect(within(wizard).getByRole("button", { name: "Next" })).toBeDisabled();
    await user.type(within(wizard).getByLabelText("Allowed clients"), "192.168.20.0/24");
    await user.click(within(wizard).getByRole("button", { name: "Next" }));
    // Step 5: summary + the concrete final action.
    expect(within(screen.getByTestId("disk-mount-review")).getByText("drive-scsi1.img.fidx")).toBeInTheDocument();
    await user.click(within(wizard).getByRole("button", { name: "Start disk mount on Lab Cluster" }));

    await waitFor(() =>
      expect(diskMountsApi.create).toHaveBeenCalledWith({
        backup_snapshot_id: "snap-1",
        disk_archives: ["drive-scsi1.img.fidx"],
        pve_cluster_id: "pve-1",
        target_node: "pve1",
        bridge: "vmbr0",
        vlan_tag: 20,
        ip_mode: "dhcp",
        ip_address_cidr: null,
        gateway: null,
        protocols: ["smb", "nfs"],
        allowed_clients: ["192.168.20.0/24"],
        lifetime_hours: 4,
      }),
    );
    const credentials = await screen.findByTestId("disk-mount-credentials");
    expect(within(credentials).getByTestId("disk-mount-password")).toHaveTextContent("Gx7-very-secret-pw-QZ01");
    expect(within(credentials).getByText(/shown only now/)).toBeInTheDocument();
    await user.click(within(credentials).getByRole("button", { name: "I have saved the password" }));
    expect(screen.queryByText("Gx7-very-secret-pw-QZ01")).not.toBeInTheDocument();
  });

  it("shows the setup panel to platform admins only", async () => {
    loginAs(["platform_admin"]);
    vi.mocked(diskMountsApi.nodes).mockResolvedValue(["pve1"]);
    const user = userEvent.setup();
    renderWithProviders(<DiskMountsPage />);
    await user.click(await screen.findByRole("button", { name: "Setup" }));
    const setup = screen.getByTestId("disk-mount-setup");
    expect(within(setup).getByLabelText("Share helper template (VMID)")).toHaveValue("9100");
    expect(within(setup).getByRole("button", { name: "Build share helper template" })).toBeDisabled();
  });

  it("builds the helper template with a paired lab agent, without SSH", async () => {
    loginAs(["platform_admin"]);
    vi.mocked(diskMountsApi.placement).mockResolvedValue([{ ...PLACEMENT, lab_agent_node: "pve1" }]);
    vi.mocked(diskMountsApi.nodes).mockResolvedValue(["pve1"]);
    vi.mocked(pveClustersApi.startLabAgentJob).mockResolvedValue({
      id: "job-1",
      cluster_id: "pve-1",
      kind: "share_helper_template_build",
      node: "pve1",
      status: "running",
      started_at: "2026-10-11T10:00:00Z",
      finished_at: null,
      steps: [],
      result: {},
      error_code: null,
      error_message: null,
      runner: "agent",
    });
    vi.mocked(pveClustersApi.labSetupJob).mockResolvedValue({
      id: "job-1",
      cluster_id: "pve-1",
      kind: "share_helper_template_build",
      node: "pve1",
      status: "running",
      started_at: "2026-10-11T10:00:00Z",
      finished_at: null,
      steps: [],
      result: {},
      error_code: null,
      error_message: null,
      runner: "agent",
    });
    const user = userEvent.setup();
    renderWithProviders(<DiskMountsPage />);
    await user.click(await screen.findByRole("button", { name: "Setup" }));
    const setup = screen.getByTestId("disk-mount-setup");
    await user.type(within(setup).getByLabelText("Storage for the template"), "local-lvm");
    await user.click(within(setup).getByRole("button", { name: "Build with lab agent" }));
    await waitFor(() =>
      expect(pveClustersApi.startLabAgentJob).toHaveBeenCalledWith("pve-1", {
        kind: "share_helper_template_build",
        storage: "local-lvm",
        image_source: "download",
      }),
    );
  });

  it("toggles the setup panel with aria-expanded", async () => {
    loginAs(["platform_admin"]);
    vi.mocked(diskMountsApi.nodes).mockResolvedValue(["pve1"]);
    const user = userEvent.setup();
    renderWithProviders(<DiskMountsPage />);
    const toggle = await screen.findByRole("button", { name: "Setup" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveAttribute("aria-controls", screen.getByTestId("disk-mount-setup").id);
  });

  it("offers the cluster's VM-disk storages as a select", async () => {
    loginAs(["platform_admin"]);
    vi.mocked(diskMountsApi.nodes).mockResolvedValue(["pve1"]);
    vi.mocked(pveClustersApi.storageConfig).mockResolvedValue([
      { storage: "local-lvm", type: "lvmthin", content: ["images", "rootdir"], nodes: null, path: null, shared: false, disabled: false, snippets_capable: false },
      { storage: "local", type: "dir", content: ["iso", "snippets"], nodes: null, path: "/var/lib/vz", shared: false, disabled: false, snippets_capable: true },
    ]);
    const user = userEvent.setup();
    renderWithProviders(<DiskMountsPage />);
    await user.click(await screen.findByRole("button", { name: "Setup" }));
    const setup = screen.getByTestId("disk-mount-setup");
    const select = await within(setup).findByRole("combobox", { name: "Storage for the template" });
    expect(within(select).getByRole("option", { name: "local-lvm (lvmthin)" })).toBeInTheDocument();
    expect(within(select).queryByRole("option", { name: "local (dir)" })).not.toBeInTheDocument();
  });

  it("does not flash the empty state while the list is loading", async () => {
    loginAs();
    vi.mocked(diskMountsApi.list).mockReturnValue(new Promise(() => {}));
    renderWithProviders(<DiskMountsPage />);
    const active = await screen.findByTestId("disk-mounts-active");
    expect(within(active).getByText("Loading…")).toBeInTheDocument();
    expect(within(active).queryByText("No active disk mounts")).not.toBeInTheDocument();
  });

  it("lets a platform admin force-finish a mount stuck in stopping, after confirmation", async () => {
    loginAs(["platform_admin"]);
    vi.mocked(diskMountsApi.list).mockResolvedValue([mount({ status: "stopping" })]);
    vi.mocked(diskMountsApi.forceFinish).mockResolvedValue(mount({ status: "failed" }));
    const user = userEvent.setup();
    renderWithProviders(<DiskMountsPage />);
    await user.click(await screen.findByRole("button", { name: "Force finish" }));
    expect(diskMountsApi.forceFinish).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "End without the agent" }));
    await waitFor(() => expect(diskMountsApi.forceFinish).toHaveBeenCalledWith("dm-1"));
  });

  it("does not offer force-finish to operators", async () => {
    loginAs();
    vi.mocked(diskMountsApi.list).mockResolvedValue([mount({ status: "stopping" })]);
    renderWithProviders(<DiskMountsPage />);
    await screen.findByText("Removing");
    expect(screen.queryByRole("button", { name: "Force finish" })).not.toBeInTheDocument();
  });

  it("renders active mounts as cards on a phone, with copyable connection lines", async () => {
    const original = window.matchMedia;
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: query.includes("max-width"),
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })) as unknown as typeof window.matchMedia;
    try {
      loginAs();
      vi.mocked(diskMountsApi.list).mockResolvedValue([mount()]);
      const user = userEvent.setup();
      renderWithProviders(<DiskMountsPage />);
      const card = await screen.findByTestId("disk-mount-card");
      expect(screen.queryByRole("table")).not.toBeInTheDocument();
      expect(within(card).getByText("Ready")).toBeInTheDocument();
      expect(within(card).getByText("3 h 12 min")).toBeInTheDocument();
      expect(within(card).getByText("\\\\192.168.20.50\\restore")).toBeInTheDocument();
      expect(within(card).getByRole("button", { name: "Copy SMB path" })).toBeInTheDocument();
      expect(within(card).getByRole("button", { name: "Stop" })).toBeInTheDocument();
      await user.click(within(card).getByRole("button", { name: "Details" }));
      expect(await screen.findByTestId("disk-mount-volumes")).toBeInTheDocument();
    } finally {
      window.matchMedia = original;
    }
  });
});
