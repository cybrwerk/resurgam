import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { LabReadinessItemOut, LabReadinessOut, LabSetupJobOut, PveClusterOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { LabComputeSetup, LabReadinessPill, canSetUpLab } from "./LabComputeSetup";

vi.mock("../api/resources", () => ({
  pveClustersApi: {
    nodes: vi.fn(),
    labReadiness: vi.fn(),
    sdnZones: vi.fn(),
    bridges: vi.fn(),
    storageConfig: vi.fn(),
    templates: vi.fn(),
    createLabOverlayStorage: vi.fn(),
    addLabStorageContent: vi.fn(),
    labAgentInstall: vi.fn(),
    rotateLabAgentToken: vi.fn(),
    update: vi.fn(),
    labDownloads: vi.fn(),
    downloadLabArtifact: vi.fn(),
    labSshTarget: vi.fn(),
    labSshHostKey: vi.fn(),
    startLabAgentInstall: vi.fn(),
    startLabTemplateBuild: vi.fn(),
    labSetupJob: vi.fn(),
    createLabPairing: vi.fn(),
    labPairing: vi.fn(),
    startLabAgentJob: vi.fn(),
    labTemplateBuildScript: vi.fn(),
    labNextVmid: vi.fn(),
  },
}));

const { pveClustersApi } = await import("../api/resources");

const CLUSTER: PveClusterOut = {
  id: "33333333-3333-3333-3333-333333333333",
  display_name: "Lab PVE",
  host: "pve.lab.example",
  port: 8006,
  tls_trust_mode: "ca",
  tls_fingerprint_sha256: null,
  version: "9.0",
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
  lab_snippet_storage_id: "local",
  lab_appliance_uplink_bridge: null,
  lab_agent_last_seen_at: null,
};

function item(key: LabReadinessItemOut["key"], status: LabReadinessItemOut["status"], code: string, extra: Partial<LabReadinessItemOut> = {}): LabReadinessItemOut {
  return { key, status, code, message: `${key} ${code}`, fix_hint: null, auto_fix: null, details: {}, ...extra };
}

function readiness(overrides: Partial<Record<LabReadinessItemOut["key"], LabReadinessItemOut>> = {}): LabReadinessOut {
  const items: LabReadinessItemOut[] = [
    item("privileges", "ok", "all_granted"),
    item("sdn_zone", "ok", "default_zone_auto_created", { details: { zone: "resurgam" } }),
    item("uplink_bridge", "missing", "not_configured"),
    item("snippet_storage", "missing", "snippets_content_missing", {
      auto_fix: "add_snippets_content",
      details: { storage: "local" },
    }),
    item("overlay_storage", "missing", "storage_not_found", {
      auto_fix: "create_overlay_storage",
      details: { storage: "resurgam-lab-overlays", expected_path: "/var/lib/resurgam-lab-agent/overlays" },
    }),
    item("appliance_template", "missing", "not_configured"),
    item("lab_agent", "missing", "token_missing"),
    item("backend_url", "ok", "configured", { details: { url: "https://resurgam.example/api/v1" } }),
  ].map((i) => overrides[i.key] ?? i);
  return {
    cluster_id: CLUSTER.id,
    ready: items.every((i) => i.status === "ok" || i.status === "warning"),
    lab_nodes: ["pve1"],
    lab_nodes_source: "all_online_nodes",
    overlay_storage_id: "resurgam-lab-overlays",
    checked_at: "2026-10-06T10:00:00Z",
    items,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(pveClustersApi.nodes).mockResolvedValue([{ node: "pve1", status: "online", cpu: null, maxmem: null, uptime: null }, { node: "pve2", status: "online", cpu: null, maxmem: null, uptime: null }]);
  vi.mocked(pveClustersApi.labReadiness).mockResolvedValue(readiness());
  vi.mocked(pveClustersApi.sdnZones).mockResolvedValue([
    { zone: "iso", type: "simple", nodes: null, pending: false, usable_for_lab: true },
    { zone: "vl", type: "vlan", nodes: null, pending: false, usable_for_lab: false },
  ]);
  vi.mocked(pveClustersApi.bridges).mockResolvedValue([
    { iface: "vmbr0", type: "bridge", active: true, vlan_aware: false, comments: "LAN" },
  ]);
  vi.mocked(pveClustersApi.storageConfig).mockResolvedValue([
    { storage: "local", type: "dir", content: ["iso", "backup"], nodes: null, path: "/var/lib/vz", shared: false, disabled: false, snippets_capable: true },
  ]);
  vi.mocked(pveClustersApi.templates).mockResolvedValue([
    { vmid: 9003, name: "lab-appliance", node: "pve1", guest_type: "qemu" },
    { vmid: 9100, name: "ct-tpl", node: "pve1", guest_type: "lxc" },
  ]);
  vi.mocked(pveClustersApi.update).mockResolvedValue(CLUSTER);
  vi.mocked(pveClustersApi.labDownloads).mockResolvedValue({ release_version: "0.2.1", debian_release: "trixie", artifacts: [] });
  vi.mocked(pveClustersApi.labNextVmid).mockResolvedValue({ vmid: 9010 });
  vi.mocked(pveClustersApi.labSshTarget).mockResolvedValue({
    node: "pve1",
    host: "192.0.2.21",
    host_source: "cluster_status",
    port: 22,
    username: "root",
  });
  vi.mocked(pveClustersApi.labAgentInstall).mockResolvedValue({
    package_name: "resurgam-lab-agent",
    package_version: "0.2.1-1",
    package_source: "build it",
    build_steps: ["dpkg-buildpackage -us -uc -b"],
    install_steps: ["apt install /root/resurgam-lab-agent_0.2.1-1_amd64.deb"],
    env_file_path: "/etc/default/resurgam-lab-agent",
    env_file: "RESURGAM_BACKEND_URL=https://resurgam.example/api/v1\nRESURGAM_LAB_AGENT_TOKEN=<LAB_AGENT_TOKEN>\n",
    token_placeholder: "<LAB_AGENT_TOKEN>",
    backend_url: "https://resurgam.example/api/v1",
    overlay_storage_id: "resurgam-lab-overlays",
    overlay_dir: "/var/lib/resurgam-lab-agent/overlays",
  });
});

const FINGERPRINT = "SHA256:" + "Q".repeat(43);
const SSH_PASSWORD = "Correct-Horse-42";

function job(overrides: Partial<LabSetupJobOut> = {}): LabSetupJobOut {
  return {
    id: "44444444-4444-4444-4444-444444444444",
    cluster_id: CLUSTER.id,
    kind: "agent_install",
    node: "pve1",
    status: "succeeded",
    started_at: "2026-10-07T10:00:00Z",
    finished_at: "2026-10-07T10:01:00Z",
    steps: [
      { key: "connect", status: "ok", message: "Connected.", output: null, started_at: "2026-10-07T10:00:00Z", finished_at: null },
      { key: "install", status: "ok", message: "Installed.", output: "Setting up resurgam-lab-agent", started_at: "2026-10-07T10:00:10Z", finished_at: null },
    ],
    result: {},
    error_code: null,
    error_message: null,
    ...overrides,
  };
}

function setupGuidedMocks() {
  vi.mocked(pveClustersApi.labSshTarget).mockResolvedValue({
    node: "pve1",
    host: "192.0.2.21",
    host_source: "cluster_status",
    port: 22,
    username: "root",
  });
  vi.mocked(pveClustersApi.labSshHostKey).mockResolvedValue({
    host: "192.0.2.21",
    port: 22,
    key_type: "ssh-ed25519",
    fingerprint_sha256: FINGERPRINT,
    verify_command: "for f in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf \"$f\"; done",
  });
}

/** Fetches and confirms the host key, then enters the password. */
async function loginOnce(user: ReturnType<typeof userEvent.setup>, prefix: string) {
  await waitFor(() => expect(screen.getByLabelText("SSH address")).toHaveValue("192.0.2.21"));
  await user.click(screen.getByRole("button", { name: "Fetch host key" }));
  expect(await screen.findByTestId(`${prefix}-host-key`)).toHaveTextContent(FINGERPRINT);
  // No credential field before the fingerprint is confirmed.
  expect(screen.queryByLabelText("SSH password")).not.toBeInTheDocument();
  await user.click(screen.getByRole("checkbox", { name: /I compared the fingerprint/ }));
  await user.type(screen.getByLabelText("SSH password"), SSH_PASSWORD);
}

async function goToStep(user: ReturnType<typeof userEvent.setup>, times: number) {
  for (let i = 0; i < times; i += 1) {
    await user.click(screen.getByRole("button", { name: "Next" }));
  }
}

describe("LabComputeSetup", () => {
  it("only allows setup once the cluster is connected", () => {
    expect(canSetUpLab(CLUSTER)).toBe(true);
    expect(canSetUpLab({ ...CLUSTER, last_health_status: "missing_privileges" })).toBe(true);
    expect(canSetUpLab({ ...CLUSTER, last_health_status: "unreachable" })).toBe(false);
    expect(canSetUpLab({ ...CLUSTER, disabled: true })).toBe(false);
  });

  it("shows the prerequisite checks with translated text and defaults the lab nodes", async () => {
    renderWithProviders(<LabComputeSetup cluster={CLUSTER} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("readiness-backend_url")).toBeInTheDocument());
    expect(within(screen.getByTestId("readiness-backend_url")).getByText("Agent and appliance report to https://resurgam.example/api/v1.")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "pve1" })).toBeChecked());
    expect(screen.getByRole("checkbox", { name: "pve2" })).not.toBeChecked();
    expect(pveClustersApi.labReadiness).toHaveBeenCalledWith(CLUSTER.id, { nodes: [], overlayStorageId: "resurgam-lab-overlays" });
  });

  it("offers zone and bridge pickers and saves the choice on Next", async () => {
    const user = userEvent.setup();
    renderWithProviders(<LabComputeSetup cluster={CLUSTER} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "pve1" })).toBeChecked());
    await goToStep(user, 1);

    const zoneSelect = await screen.findByLabelText("SDN zone");
    await waitFor(() => expect(within(zoneSelect).getByRole("option", { name: /^iso/ })).toBeInTheDocument());
    expect(within(zoneSelect).getByRole("option", { name: /^vl/ })).toBeDisabled();
    await user.selectOptions(zoneSelect, "iso");
    const bridgeSelect = screen.getByLabelText("Uplink bridge for the probe appliance");
    await waitFor(() => expect(within(bridgeSelect).getByRole("option", { name: /vmbr0/ })).toBeInTheDocument());
    await user.selectOptions(bridgeSelect, "vmbr0");
    expect(pveClustersApi.bridges).toHaveBeenCalledWith(CLUSTER.id, "pve1");

    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() =>
      expect(pveClustersApi.update).toHaveBeenCalledWith(CLUSTER.id, {
        lab_sdn_zone_name: "iso",
        lab_appliance_uplink_bridge: "vmbr0",
        lab_snippet_storage_id: "local",
        lab_appliance_template_vmid: null,
        lab_appliance_uplink_vlan_tag: null,
        lab_appliance_uplink_ip_mode: "dhcp",
        lab_appliance_uplink_ip_range_start: null,
        lab_appliance_uplink_ip_range_end: null,
        lab_appliance_uplink_ip_prefix_length: null,
        lab_appliance_uplink_gateway: null,
        lab_appliance_uplink_dns_servers: [],
      }),
    );
    expect(await screen.findByLabelText("Snippet storage")).toBeInTheDocument();
  });

  it("runs an auto-fix only after the in-page confirmation", async () => {
    vi.mocked(pveClustersApi.addLabStorageContent).mockResolvedValue({
      action: "add_storage_content",
      storage_id: "local",
      content: ["iso", "backup", "snippets"],
      item: item("snippet_storage", "ok", "storage_ok"),
    });
    vi.mocked(pveClustersApi.createLabOverlayStorage).mockResolvedValue({
      action: "create_overlay_storage",
      storage_id: "resurgam-lab-overlays",
      content: ["images"],
      item: item("overlay_storage", "ok", "storage_ok"),
    });
    const user = userEvent.setup();
    renderWithProviders(<LabComputeSetup cluster={CLUSTER} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "pve1" })).toBeChecked());
    await goToStep(user, 2);

    await user.click(await screen.findByRole("button", { name: 'Add "snippets" to local' }));
    expect(pveClustersApi.addLabStorageContent).not.toHaveBeenCalled();
    const confirm = screen.getByRole("group", { name: 'Add "snippets" to local' });
    expect(within(confirm).getByText(/All existing content types stay/)).toBeInTheDocument();
    await user.click(within(confirm).getByRole("button", { name: "Apply now" }));
    await waitFor(() =>
      expect(pveClustersApi.addLabStorageContent).toHaveBeenCalledWith(
        CLUSTER.id,
        { storage_id: "local", content_type: "snippets" },
        ["pve1"],
      ),
    );

    await user.click(screen.getByRole("button", { name: "Create overlay storage resurgam-lab-overlays" }));
    const overlayConfirm = screen.getByRole("group", { name: "Create overlay storage resurgam-lab-overlays" });
    // Cancel first: nothing is sent.
    await user.click(within(overlayConfirm).getByRole("button", { name: "Cancel" }));
    expect(pveClustersApi.createLabOverlayStorage).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Create overlay storage resurgam-lab-overlays" }));
    await user.click(
      within(screen.getByRole("group", { name: "Create overlay storage resurgam-lab-overlays" })).getByRole("button", {
        name: "Apply now",
      }),
    );
    await waitFor(() =>
      expect(pveClustersApi.createLabOverlayStorage).toHaveBeenCalledWith(CLUSTER.id, {
        storage_id: "resurgam-lab-overlays",
        nodes: ["pve1"],
      }),
    );
  });

  it("lists only VM templates for the probe appliance", async () => {
    const user = userEvent.setup();
    renderWithProviders(<LabComputeSetup cluster={CLUSTER} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "pve1" })).toBeChecked());
    await goToStep(user, 4);
    const select = await screen.findByLabelText("Probe appliance template");
    await waitFor(() => expect(within(select).getByRole("option", { name: /9003/ })).toBeInTheDocument());
    expect(within(select).queryByRole("option", { name: /9100/ })).not.toBeInTheDocument();
  });

  it("generates the agent token, fills it into the env file and polls until the agent checks in", async () => {
    vi.mocked(pveClustersApi.rotateLabAgentToken).mockResolvedValue({ token: "fresh-agent-token" });
    const user = userEvent.setup();
    renderWithProviders(<LabComputeSetup cluster={CLUSTER} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "pve1" })).toBeChecked());
    await goToStep(user, 3);
    await user.click(screen.getByRole("radio", { name: "Manual" }));

    await waitFor(() => expect(screen.getByTestId("lab-agent-env-file")).toHaveTextContent("<LAB_AGENT_TOKEN>"));
    expect(screen.getByText("apt install /root/resurgam-lab-agent_0.2.1-1_amd64.deb")).toBeInTheDocument();

    vi.mocked(pveClustersApi.labReadiness).mockResolvedValue(
      readiness({ lab_agent: item("lab_agent", "ok", "recently_seen") }),
    );
    await user.click(screen.getByRole("button", { name: "Generate token" }));
    await waitFor(() =>
      expect(screen.getByTestId("lab-agent-env-file")).toHaveTextContent("RESURGAM_LAB_AGENT_TOKEN=fresh-agent-token"),
    );
    expect(screen.getByText("The token is shown only now. It is stored hashed and cannot be displayed again.")).toBeInTheDocument();
    await waitFor(() =>
      expect(within(screen.getByTestId("readiness-lab_agent")).getByText("The agent is checking in.")).toBeInTheDocument(),
    );
  });

  it("enables the cluster as lab compute at the end, even with open items", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(<LabComputeSetup cluster={CLUSTER} onClose={onClose} />);
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "pve1" })).toBeChecked());
    await goToStep(user, 5);

    expect(await screen.findByText(/You can enable the cluster as lab compute anyway/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Enable as lab compute" }));
    await waitFor(() => expect(pveClustersApi.update).toHaveBeenCalledWith(CLUSTER.id, { is_lab_compute: true }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("validates the static uplink range and saves VLAN + range on Next", async () => {
    const user = userEvent.setup();
    renderWithProviders(<LabComputeSetup cluster={{ ...CLUSTER, lab_appliance_uplink_bridge: "vmbr0" }} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "pve1" })).toBeChecked());
    await goToStep(user, 1);

    await user.type(await screen.findByLabelText("Uplink VLAN tag (optional)"), "42");
    await user.click(screen.getByRole("radio", { name: "Static range" }));
    await user.type(screen.getByLabelText("Range from"), "192.168.40.50");
    await user.type(screen.getByLabelText("Range to"), "192.168.41.60");
    await user.type(screen.getByLabelText("Gateway"), "192.168.40.55");
    expect(screen.getByText("Start and end are not in the same subnet.")).toBeInTheDocument();
    expect(screen.getByText("At least one DNS server is needed so the appliance can resolve the backend URL.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("Please correct the highlighted uplink fields first.")).toBeInTheDocument();
    expect(pveClustersApi.update).not.toHaveBeenCalled();

    await user.clear(screen.getByLabelText("Range to"));
    await user.type(screen.getByLabelText("Range to"), "192.168.40.60");
    expect(screen.getByText("The gateway lies inside the range; shrink the range so the gateway is outside it.")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Gateway"));
    await user.type(screen.getByLabelText("Gateway"), "192.168.40.1");
    await user.type(screen.getByLabelText("DNS servers"), "192.168.40.2, 9.9.9.9");

    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() =>
      expect(pveClustersApi.update).toHaveBeenCalledWith(
        CLUSTER.id,
        expect.objectContaining({
          lab_appliance_uplink_vlan_tag: 42,
          lab_appliance_uplink_ip_mode: "static_range",
          lab_appliance_uplink_ip_range_start: "192.168.40.50",
          lab_appliance_uplink_ip_range_end: "192.168.40.60",
          lab_appliance_uplink_ip_prefix_length: 24,
          lab_appliance_uplink_gateway: "192.168.40.1",
          lab_appliance_uplink_dns_servers: ["192.168.40.2", "9.9.9.9"],
        }),
      ),
    );
  });

  it("installs the agent over a one-time SSH session after the host key is confirmed", async () => {
    setupGuidedMocks();
    vi.mocked(pveClustersApi.startLabAgentInstall).mockResolvedValue(job({ status: "running", steps: [] }));
    vi.mocked(pveClustersApi.labSetupJob).mockResolvedValue(job());
    const user = userEvent.setup();
    renderWithProviders(<LabComputeSetup cluster={CLUSTER} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "pve1" })).toBeChecked());
    await goToStep(user, 3);

    expect(screen.getByRole("radio", { name: "Pair node with a one-time code (recommended)" })).toBeChecked();
    await user.click(screen.getByRole("radio", { name: "Guided (SSH)" }));
    const install = screen.getByRole("button", { name: "Install agent" });
    expect(install).toBeDisabled();
    await loginOnce(user, "lab-agent-ssh");
    // The host-key step never carries credentials.
    expect(pveClustersApi.labSshHostKey).toHaveBeenCalledWith(CLUSTER.id, { node: "pve1", host: "192.0.2.21", port: 22 });
    expect(JSON.stringify(vi.mocked(pveClustersApi.labSshHostKey).mock.calls)).not.toContain(SSH_PASSWORD);

    await user.type(screen.getByLabelText("This node's PBS API token (optional)"), "lab@pbs!agent:secret");
    expect(install).toBeEnabled();
    await user.click(install);
    await waitFor(() =>
      expect(pveClustersApi.startLabAgentInstall).toHaveBeenCalledWith(CLUSTER.id, {
        ssh: {
          node: "pve1",
          host: "192.0.2.21",
          port: 22,
          username: "root",
          auth_method: "password",
          password: SSH_PASSWORD,
          private_key: null,
          passphrase: null,
          confirmed_fingerprint: FINGERPRINT,
        },
        overlay_storage_id: "resurgam-lab-overlays",
        overlay_dir: "/var/lib/resurgam-lab-agent/overlays",
        pbs_token: "lab@pbs!agent:secret",
      }),
    );
    const log = await screen.findByTestId("lab-job-log");
    expect(within(log).getByText("Succeeded")).toBeInTheDocument();
    expect(within(log).getByText("Connect and check host key")).toBeInTheDocument();
    expect(within(log).getByText("Install package")).toBeInTheDocument();
    // The PBS token field is cleared after use.
    expect(screen.getByLabelText("This node's PBS API token (optional)")).toHaveValue("");
  });

  it("shows a failed guided job's translated error", async () => {
    setupGuidedMocks();
    vi.mocked(pveClustersApi.startLabAgentInstall).mockResolvedValue(job({ status: "running", steps: [] }));
    vi.mocked(pveClustersApi.labSetupJob).mockResolvedValue(
      job({
        status: "failed",
        error_code: "privilege_missing",
        error_message: "User 'admin' is not root",
        steps: [{ key: "connect", status: "failed", message: "x", output: null, started_at: "2026-10-07T10:00:00Z", finished_at: null }],
      }),
    );
    const user = userEvent.setup();
    renderWithProviders(<LabComputeSetup cluster={CLUSTER} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "pve1" })).toBeChecked());
    await goToStep(user, 3);
    await user.click(screen.getByRole("radio", { name: "Guided (SSH)" }));
    await loginOnce(user, "lab-agent-ssh");
    await user.click(screen.getByRole("button", { name: "Install agent" }));
    expect(
      await screen.findByText(
        "The user is not root and may not run sudo without a password. Log in as root or allow passwordless sudo.",
      ),
    ).toBeInTheDocument();
  });

  it("shows a host-key error instead of the fingerprint", async () => {
    setupGuidedMocks();
    const { ApiError } = await import("../api/client");
    vi.mocked(pveClustersApi.labSshHostKey).mockRejectedValue(new ApiError(502, "connect_failed", "Could not reach 192.0.2.21:22."));
    const user = userEvent.setup();
    renderWithProviders(<LabComputeSetup cluster={CLUSTER} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "pve1" })).toBeChecked());
    await goToStep(user, 4);
    await waitFor(() => expect(screen.getByLabelText("SSH address")).toHaveValue("192.0.2.21"));
    await user.click(screen.getByRole("button", { name: "Fetch host key" }));
    expect(await screen.findByText("Could not reach 192.0.2.21:22.")).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /I compared the fingerprint/ })).not.toBeInTheDocument();
  });

  it("builds the template over SSH and selects it", async () => {
    setupGuidedMocks();
    vi.mocked(pveClustersApi.storageConfig).mockImplementation(async (_id, opts) =>
      opts?.snippetsCapable
        ? []
        : [
            { storage: "local-lvm", type: "lvmthin", content: ["images", "rootdir"], nodes: null, path: null, shared: false, disabled: false, snippets_capable: false },
            { storage: "local", type: "dir", content: ["iso"], nodes: null, path: "/var/lib/vz", shared: false, disabled: false, snippets_capable: true },
          ],
    );
    vi.mocked(pveClustersApi.labNextVmid).mockResolvedValue({ vmid: 9010 });
    vi.mocked(pveClustersApi.startLabTemplateBuild).mockResolvedValue(job({ kind: "template_build", status: "running", steps: [] }));
    vi.mocked(pveClustersApi.labSetupJob).mockResolvedValue(
      job({ kind: "template_build", result: { template_vmid: 9010, prober_version: "0.2.1-1" } }),
    );
    const user = userEvent.setup();
    renderWithProviders(<LabComputeSetup cluster={CLUSTER} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "pve1" })).toBeChecked());
    await goToStep(user, 4);

    const storage = await screen.findByLabelText("Target storage (content images)");
    await waitFor(() => expect(within(storage).getByRole("option", { name: /local-lvm/ })).toBeInTheDocument());
    expect(within(storage).queryByRole("option", { name: /^local \(/ })).not.toBeInTheDocument();
    await user.selectOptions(storage, "local-lvm");
    await waitFor(() => expect(screen.getByLabelText("Template VMID")).toHaveAttribute("placeholder", "9010"));
    await loginOnce(user, "lab-build-ssh");
    await user.click(screen.getByRole("button", { name: "Build template" }));

    await waitFor(() =>
      expect(pveClustersApi.startLabTemplateBuild).toHaveBeenCalledWith(
        CLUSTER.id,
        expect.objectContaining({ storage: "local-lvm", vmid: null, image_source: "download", node_image: null }),
      ),
    );
    await waitFor(() => expect(screen.getByLabelText("Probe appliance template")).toHaveValue("9010"));
  });

  it("shows the manual template build script", async () => {
    vi.mocked(pveClustersApi.storageConfig).mockResolvedValue([
      { storage: "local-lvm", type: "lvmthin", content: ["images"], nodes: null, path: null, shared: false, disabled: false, snippets_capable: false },
    ]);
    vi.mocked(pveClustersApi.labNextVmid).mockResolvedValue({ vmid: 9010 });
    vi.mocked(pveClustersApi.downloadLabArtifact).mockResolvedValue(undefined);
    vi.mocked(pveClustersApi.labTemplateBuildScript).mockResolvedValue({
      node: "pve1",
      vmid: 9010,
      storage: "local-lvm",
      prober_filename: "resurgam-lab-prober_0.2.1-1_amd64.deb",
      prober_download_path: "/pve-clusters/lab-setup/downloads/resurgam-lab-prober",
      script: "#!/bin/sh\nqm create 9010 ...\nqm template 9010\n",
    });
    const user = userEvent.setup();
    renderWithProviders(<LabComputeSetup cluster={CLUSTER} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "pve1" })).toBeChecked());
    await goToStep(user, 4);
    await user.click(screen.getByRole("radio", { name: "Manual (script)" }));
    const storage = screen.getByLabelText("Target storage (content images)");
    await waitFor(() => expect(within(storage).getByRole("option", { name: /local-lvm/ })).toBeInTheDocument());
    await user.selectOptions(storage, "local-lvm");
    await user.click(screen.getByRole("radio", { name: "Image on the node" }));
    await user.type(screen.getByLabelText("Image (volume ID or path)"), "local:import/debian.qcow2");
    await user.click(screen.getByRole("button", { name: "Generate script" }));
    await waitFor(() =>
      expect(pveClustersApi.labTemplateBuildScript).toHaveBeenCalledWith(CLUSTER.id, {
        node: "pve1",
        storage: "local-lvm",
        vmid: null,
        imageSource: "node_image",
        nodeImage: "local:import/debian.qcow2",
      }),
    );
    expect(await screen.findByTestId("lab-template-script")).toHaveTextContent("qm template 9010");
    await user.click(screen.getByRole("button", { name: "Download resurgam-lab-prober_0.2.1-1_amd64.deb" }));
    expect(pveClustersApi.downloadLabArtifact).toHaveBeenCalledWith(
      "/pve-clusters/lab-setup/downloads/resurgam-lab-prober",
      "resurgam-lab-prober_0.2.1-1_amd64.deb",
    );
  });

  it("offers the shipped package download in manual agent mode", async () => {
    vi.mocked(pveClustersApi.labAgentInstall).mockResolvedValue({
      package_name: "resurgam-lab-agent",
      package_version: "0.2.1-1",
      package_source: "shipped",
      package_download_path: "/pve-clusters/lab-setup/downloads/resurgam-lab-agent",
      package_sha256: "ab".repeat(32),
      package_filename: "resurgam-lab-agent_0.2.1-1_amd64.deb",
      build_steps: [],
      install_steps: ["apt install /root/resurgam-lab-agent_0.2.1-1_amd64.deb"],
      env_file_path: "/etc/default/resurgam-lab-agent",
      env_file: "RESURGAM_LAB_AGENT_TOKEN=<LAB_AGENT_TOKEN>\n",
      token_placeholder: "<LAB_AGENT_TOKEN>",
      backend_url: "https://resurgam.example/api/v1",
      overlay_storage_id: "resurgam-lab-overlays",
      overlay_dir: "/var/lib/resurgam-lab-agent/overlays",
    });
    vi.mocked(pveClustersApi.downloadLabArtifact).mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderWithProviders(<LabComputeSetup cluster={CLUSTER} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "pve1" })).toBeChecked());
    await goToStep(user, 3);
    await user.click(screen.getByRole("radio", { name: "Manual" }));
    await user.click(await screen.findByRole("button", { name: "Download resurgam-lab-agent_0.2.1-1_amd64.deb" }));
    expect(pveClustersApi.downloadLabArtifact).toHaveBeenCalledWith(
      "/pve-clusters/lab-setup/downloads/resurgam-lab-agent",
      "resurgam-lab-agent_0.2.1-1_amd64.deb",
    );
    expect(screen.getByText(new RegExp("ab".repeat(32)))).toBeInTheDocument();
    expect(screen.queryByText("dpkg-buildpackage -us -uc -b")).not.toBeInTheDocument();
  });

  it("falls back to the backend message for an unknown code", async () => {
    vi.mocked(pveClustersApi.labReadiness).mockResolvedValue(
      readiness({ privileges: item("privileges", "unknown", "brand_new_code", { message: "Something new.", fix_hint: "Do X." }) }),
    );
    renderWithProviders(<LabComputeSetup cluster={CLUSTER} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("Something new. Do X.")).toBeInTheDocument());
  });
});

describe("LabReadinessPill", () => {
  it("shows ready / incomplete for lab clusters and nothing for others", async () => {
    vi.mocked(pveClustersApi.labReadiness).mockResolvedValueOnce(
      readiness({
        uplink_bridge: item("uplink_bridge", "ok", "bridge_ok"),
        snippet_storage: item("snippet_storage", "ok", "storage_ok"),
        overlay_storage: item("overlay_storage", "ok", "storage_ok"),
        appliance_template: item("appliance_template", "ok", "template_ok"),
        lab_agent: item("lab_agent", "ok", "recently_seen"),
      }),
    );
    const { unmount } = renderWithProviders(<LabReadinessPill cluster={{ ...CLUSTER, is_lab_compute: true }} />);
    expect(await screen.findByText("Lab ready")).toBeInTheDocument();
    unmount();

    renderWithProviders(<LabReadinessPill cluster={{ ...CLUSTER, id: "other", is_lab_compute: true }} />);
    expect(await screen.findByText("Lab incomplete")).toBeInTheDocument();

    vi.mocked(pveClustersApi.labReadiness).mockClear();
    renderWithProviders(<LabReadinessPill cluster={{ ...CLUSTER, id: "nolab" }} />);
    expect(pveClustersApi.labReadiness).not.toHaveBeenCalled();
  });

  it("pairs the node with a one-time command and then builds the template with the agent", async () => {
    const pairing = {
      id: "pair-1",
      node: "pve1",
      code: "ABCD-EFGH-JKMN-PQRS",
      command: "curl -fsSk -o /root/resurgam-pair.sh https://r/api/v1/lab-setup/bootstrap/p/pair-1/script && echo 'aa  /root/resurgam-pair.sh' | sha256sum -c - && sh /root/resurgam-pair.sh ABCD-EFGH-JKMN-PQRS",
      script_sha256: "aa",
      shell_url: "https://pve.lab.example:8006/?console=shell&xtermjs=1&vmid=0&vmname=&node=pve1&cmd=",
      expires_at: "2026-10-11T12:30:00Z",
      state: "waiting" as const,
      consumed_at: null,
      pbs_token_provided: null,
      agent_last_seen_at: null,
    };
    vi.mocked(pveClustersApi.createLabPairing).mockResolvedValue(pairing);
    vi.mocked(pveClustersApi.labPairing).mockResolvedValue({
      ...pairing,
      code: null,
      command: null,
      state: "paired",
      consumed_at: "2026-10-11T12:05:00Z",
      pbs_token_provided: false,
    });
    vi.mocked(pveClustersApi.storageConfig).mockResolvedValue([
      { storage: "local-lvm", type: "lvmthin", content: ["images"], nodes: null, path: null, shared: false, disabled: false, snippets_capable: false },
    ]);
    vi.mocked(pveClustersApi.labNextVmid).mockResolvedValue({ vmid: 9010 });
    vi.mocked(pveClustersApi.startLabAgentJob).mockResolvedValue(
      job({ kind: "template_build", status: "running", steps: [], runner: "agent" }),
    );
    vi.mocked(pveClustersApi.labSetupJob).mockResolvedValue(
      job({ kind: "template_build", result: { template_vmid: 9010, prober_version: "0.2.5-1" }, runner: "agent" }),
    );
    const user = userEvent.setup();
    renderWithProviders(<LabComputeSetup cluster={CLUSTER} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "pve1" })).toBeChecked());
    await goToStep(user, 3);

    await user.click(screen.getByRole("button", { name: "Create pairing command" }));
    expect(await screen.findByTestId("lab-pair-command")).toHaveTextContent("sha256sum -c -");
    expect(pveClustersApi.createLabPairing).toHaveBeenCalledWith(CLUSTER.id, expect.objectContaining({ node: "pve1" }));
    expect(screen.getByRole("link", { name: "Open node shell (Proxmox)" })).toHaveAttribute("href", pairing.shell_url);
    await waitFor(() => expect(screen.getByTestId("lab-pair-state")).toHaveTextContent("Paired"), { timeout: 5000 });
    // Success and the missing PBS token are separate notices.
    const paired = screen.getByText("Node pve1 is paired; its agent now reports to RESURGAM.");
    expect(paired.closest(".inlineAlert")).toHaveClass("inlineAlertSuccess");
    expect(screen.getByText(/No PBS token was entered/).closest(".inlineAlert")).toHaveClass("inlineAlertInfo");
    expect(screen.getByText("The previous code stops working.")).toBeInTheDocument();

    await goToStep(user, 1);
    expect(screen.getByRole("radio", { name: "Build with the paired lab agent on pve1 (no login)" })).toBeChecked();
    const storage = await screen.findByLabelText("Target storage (content images)");
    await waitFor(() => expect(within(storage).getByRole("option", { name: /local-lvm/ })).toBeInTheDocument());
    await user.selectOptions(storage, "local-lvm");
    await user.click(screen.getByRole("button", { name: "Build template" }));
    await waitFor(() =>
      expect(pveClustersApi.startLabAgentJob).toHaveBeenCalledWith(
        CLUSTER.id,
        expect.objectContaining({ kind: "template_build", storage: "local-lvm", image_source: "download" }),
      ),
    );
    expect(pveClustersApi.startLabTemplateBuild).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByLabelText("Probe appliance template")).toHaveValue("9010"));
  });
});
