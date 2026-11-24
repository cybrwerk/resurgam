import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";

import type { BackupGroupOut, CustomerOut, DatastoreOut, PbsInstanceOut, WhoAmIResponse } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { DatastoresPage } from "./DatastoresPage";

vi.mock("../api/resources", () => ({
  pbsInstancesApi: {
    list: vi.fn(),
    availableDatastores: vi.fn(),
    getMaintenanceMode: vi.fn(),
    setMaintenanceMode: vi.fn(),
    getGcSchedule: vi.fn(),
    setGcSchedule: vi.fn(),
  },
  customersApi: { list: vi.fn() },
  datastoresApi: {
    list: vi.fn(),
    backupGroups: vi.fn(),
    assignCustomer: vi.fn(),
    unassignCustomer: vi.fn(),
  },
  authApi: { whoami: vi.fn() },
  protectionPoliciesApi: { list: vi.fn() },
  inventoryApi: {
    snapshots: vi.fn(),
    assignCustomer: vi.fn(),
    unassignCustomer: vi.fn(),
    assignProtectionPolicy: vi.fn(),
    unassignProtectionPolicy: vi.fn(),
  },
  fileRestoreApi: { list: vi.fn(), download: vi.fn() },
  complianceAttestationsApi: { listForDatastore: vi.fn(), createForDatastore: vi.fn() },
  pbsJobsApi: {
    pruneJobCapabilities: vi.fn(),
    pruneJobs: vi.fn(),
    verifyJobCapabilities: vi.fn(),
    verifyJobs: vi.fn(),
    syncJobCapabilities: vi.fn(),
    syncJobs: vi.fn(),
    gcScheduleCapabilities: vi.fn(),
    remotes: vi.fn(),
  },
}));

const {
  pbsInstancesApi,
  customersApi,
  datastoresApi,
  protectionPoliciesApi,
  complianceAttestationsApi,
  pbsJobsApi,
  authApi,
} = await import("../api/resources");

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
  customer_id: "cust-1",
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

function group(overrides: Partial<BackupGroupOut>): BackupGroupOut {
  return {
    id: `group-${overrides.backup_id}`,
    datastore_id: "ds-1",
    namespace: "",
    backup_type: "ct",
    backup_id: "100",
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
    ...overrides,
  };
}

const GROUPS = [
  group({ backup_id: "100" }),
  group({ backup_id: "200" }),
  group({ backup_id: "300" }),
];

function setup() {
  vi.mocked(pbsInstancesApi.list).mockResolvedValue([INSTANCE]);
  vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
  vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE]);
  vi.mocked(datastoresApi.backupGroups).mockResolvedValue(GROUPS);
  vi.mocked(protectionPoliciesApi.list).mockResolvedValue([]);
  vi.mocked(pbsInstancesApi.getMaintenanceMode).mockResolvedValue({ mode: null, message: null });
}

async function expandToGroups(user: ReturnType<typeof userEvent.setup>) {
  await waitFor(() => expect(screen.getByText("Primary PBS")).toBeInTheDocument());
  await user.click(screen.getAllByLabelText("Expand")[0]); // instance
  await waitFor(() => expect(screen.getByText("main-store")).toBeInTheDocument());
  await user.click(screen.getAllByLabelText("Expand")[0]); // datastore
  await waitFor(() => expect(screen.getByText("/")).toBeInTheDocument()); // root namespace
  await user.click(screen.getAllByLabelText("Expand")[0]); // namespace
  await waitFor(() => expect(screen.getByText("100")).toBeInTheDocument());
}

describe("DatastoresPage — PBS instance row", () => {
  it("expands when the row itself is clicked, not just the small chevron icon", async () => {
    // A real UX-audit finding (2026-09-23): the row had a full-width
    // hover highlight suggesting it was clickable, but only the chevron
    // icon actually responded.
    setup();
    const user = userEvent.setup();
    renderWithProviders(<DatastoresPage />);

    // A lone instance starts expanded (see the next test), so the row
    // click is checked as collapse, then expand again.
    await waitFor(() => expect(screen.getByText("main-store")).toBeInTheDocument());

    await user.click(screen.getByText("Primary PBS"));
    await waitFor(() => expect(screen.queryByText("main-store")).not.toBeInTheDocument());

    await user.click(screen.getByText("Primary PBS"));
    await waitFor(() => expect(screen.getByText("main-store")).toBeInTheDocument());
  });

  it("opens a lone PBS instance by itself, and a collapse sticks", async () => {
    setup();
    const user = userEvent.setup();
    const { queryClient } = renderWithProviders(<DatastoresPage />);

    await waitFor(() => expect(screen.getByText("main-store")).toBeInTheDocument());

    await user.click(screen.getByText("Primary PBS"));
    await waitFor(() => expect(screen.queryByText("main-store")).not.toBeInTheDocument());

    // A background refetch must not re-open what the user just closed.
    await queryClient.invalidateQueries({ queryKey: ["pbs-instances"] });
    await waitFor(() => expect(screen.getByText("Primary PBS")).toBeInTheDocument());
    expect(screen.queryByText("main-store")).not.toBeInTheDocument();
  });
});

describe("DatastoresPage — backup group filter", () => {
  it("shows all groups in an expanded namespace when no filter is typed", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<DatastoresPage />);
    await expandToGroups(user);

    expect(screen.getByText("200")).toBeInTheDocument();
    expect(screen.getByText("300")).toBeInTheDocument();
  });

  it("filters groups by backup ID as you type", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<DatastoresPage />);
    await expandToGroups(user);

    await user.type(screen.getByPlaceholderText("Filter groups by ID or name…"), "200");
    expect(screen.getByText("200")).toBeInTheDocument();
    expect(screen.queryByText("100")).not.toBeInTheDocument();
    expect(screen.queryByText("300")).not.toBeInTheDocument();
  });

  it("shows an empty state when the filter matches nothing", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<DatastoresPage />);
    await expandToGroups(user);

    await user.type(screen.getByPlaceholderText("Filter groups by ID or name…"), "does-not-exist");
    await waitFor(() =>
      expect(screen.getByText("No backup groups match your filter.")).toBeInTheDocument(),
    );
  });
});

describe("DatastoresPage — maintenance mode", () => {
  it("shows a maintenance badge when a datastore has an active mode", async () => {
    setup();
    vi.mocked(pbsInstancesApi.getMaintenanceMode).mockResolvedValue({ mode: "read-only", message: "planned work" });
    const user = userEvent.setup();
    renderWithProviders(<DatastoresPage />);

    await waitFor(() => expect(screen.getByText("Primary PBS")).toBeInTheDocument());
    await user.click(screen.getAllByLabelText("Expand")[0]);

    await waitFor(() => expect(screen.getByText("Maintenance: Read-only")).toBeInTheDocument());
  });

  it("sets maintenance mode through the modal", async () => {
    setup();
    vi.mocked(pbsInstancesApi.setMaintenanceMode).mockResolvedValue({ mode: "offline", message: "" });
    const user = userEvent.setup();
    renderWithProviders(<DatastoresPage />);

    await waitFor(() => expect(screen.getByText("Primary PBS")).toBeInTheDocument());
    await user.click(screen.getAllByLabelText("Expand")[0]);
    await waitFor(() => expect(screen.getByText("main-store")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Maintenance" }));
    const dialog = await screen.findByRole("dialog");
    await user.selectOptions(within(dialog).getByLabelText("Maintenance mode"), "offline");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(pbsInstancesApi.setMaintenanceMode).toHaveBeenCalledWith("pbs-1", "main-store", {
        mode: "offline",
        message: null,
        confirm_instant_recovery: false,
      }),
    );
  });

  it("asks for confirmation when a VM runs from a backup on the datastore (Instant Recovery)", async () => {
    setup();
    vi.mocked(pbsInstancesApi.setMaintenanceMode)
      .mockRejectedValueOnce(
        new ApiError(409, "instant_recovery_active", "VMs run from backups here.", undefined, {
          restore_plans: [{ id: "p1", name: "restore-srv", vmid: 200, node: "pve1" }],
        }),
      )
      .mockResolvedValueOnce({ mode: "offline", message: "" });
    const user = userEvent.setup();
    renderWithProviders(<DatastoresPage />);

    await waitFor(() => expect(screen.getByText("Primary PBS")).toBeInTheDocument());
    await user.click(screen.getAllByLabelText("Expand")[0]);
    await waitFor(() => expect(screen.getByText("main-store")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Maintenance" }));
    const dialog = await screen.findByRole("dialog");
    await user.selectOptions(within(dialog).getByLabelText("Maintenance mode"), "offline");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(await within(dialog).findByText(/restore-srv \(VM 200, pve1\)/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Set it anyway" }));

    await waitFor(() =>
      expect(pbsInstancesApi.setMaintenanceMode).toHaveBeenLastCalledWith("pbs-1", "main-store", {
        mode: "offline",
        message: null,
        confirm_instant_recovery: true,
      }),
    );
  });
});

describe("DatastoresPage — job administration", () => {
  it("lists prune jobs when the Jobs modal is opened", async () => {
    setup();
    vi.mocked(pbsJobsApi.pruneJobCapabilities).mockResolvedValue({ can_read: true, can_write: true });
    vi.mocked(pbsJobsApi.pruneJobs).mockResolvedValue([
      { id: "p1", store: "main-store", schedule: "21:00", ns: null, keep_last: null, keep_hourly: null, keep_daily: 7, keep_weekly: null, keep_monthly: null, keep_yearly: null, disable: false, comment: null },
    ]);
    vi.mocked(pbsJobsApi.verifyJobCapabilities).mockResolvedValue({ can_read: true, can_write: true });
    vi.mocked(pbsJobsApi.verifyJobs).mockResolvedValue([]);
    vi.mocked(pbsJobsApi.syncJobCapabilities).mockResolvedValue({ can_read: true, can_write: true });
    vi.mocked(pbsJobsApi.syncJobs).mockResolvedValue([]);
    vi.mocked(pbsInstancesApi.getGcSchedule).mockResolvedValue({ gc_schedule: "07:45" });
    vi.mocked(pbsJobsApi.gcScheduleCapabilities).mockResolvedValue({ can_read: true, can_write: true });
    const user = userEvent.setup();
    renderWithProviders(<DatastoresPage />);

    await waitFor(() => expect(screen.getByText("Primary PBS")).toBeInTheDocument());
    await user.click(screen.getAllByLabelText("Expand")[0]);
    await waitFor(() => expect(screen.getByText("main-store")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Jobs" }));
    const dialog = await screen.findByRole("dialog");

    await waitFor(() => expect(within(dialog).getByText("21:00")).toBeInTheDocument());
    expect(pbsJobsApi.pruneJobs).toHaveBeenCalledWith("pbs-1", "main-store");
    // GC is now a read-only row with Edit/Remove actions (not an inline input).
    await waitFor(() => expect(within(dialog).getByText("07:45")).toBeInTheDocument());
  });
});

describe("DatastoresPage — immutability attestation", () => {
  it("shows the current attestation and records a new one through the modal", async () => {
    setup();
    vi.mocked(complianceAttestationsApi.listForDatastore).mockResolvedValue([
      {
        id: "attest-1", scope_type: "datastore", scope_id: "ds-1", dimension: "immutable_copy", value: false,
        notes: null, asserted_by_user_id: "user-1", asserted_by_display_name: "Admin One", created_at: "2026-01-01T00:00:00Z",
      },
    ]);
    vi.mocked(complianceAttestationsApi.createForDatastore).mockResolvedValue({
      id: "attest-2", scope_type: "datastore", scope_id: "ds-1", dimension: "immutable_copy", value: true,
      notes: "Confirmed", asserted_by_user_id: "user-1", asserted_by_display_name: "Admin One", created_at: "2026-01-02T00:00:00Z",
    });
    const user = userEvent.setup();
    renderWithProviders(<DatastoresPage />);

    await waitFor(() => expect(screen.getByText("Primary PBS")).toBeInTheDocument());
    await user.click(screen.getAllByLabelText("Expand")[0]);
    await waitFor(() => expect(screen.getByText("main-store")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Immutability" }));
    const dialog = await screen.findByRole("dialog");
    // "Not immutable" also appears as a <select> option and in the history
    // list below, so assert on the count rather than a single unique match.
    await waitFor(() => expect(within(dialog).getAllByText("Not immutable").length).toBeGreaterThanOrEqual(1));
    expect(within(dialog).getByText(/Attested by Admin One on/)).toBeInTheDocument();

    await user.selectOptions(within(dialog).getByLabelText("Record new attestation"), "yes");
    await user.type(within(dialog).getByLabelText("Notes (optional)"), "Confirmed");
    await user.click(within(dialog).getByRole("button", { name: "Record" }));

    await waitFor(() =>
      expect(complianceAttestationsApi.createForDatastore).toHaveBeenCalledWith("ds-1", {
        dimension: "immutable_copy", value: true, notes: "Confirmed",
      }),
    );
  });

  it("shows a not-attested state when no attestation has ever been recorded", async () => {
    setup();
    vi.mocked(complianceAttestationsApi.listForDatastore).mockResolvedValue([]);
    const user = userEvent.setup();
    renderWithProviders(<DatastoresPage />);

    await waitFor(() => expect(screen.getByText("Primary PBS")).toBeInTheDocument());
    await user.click(screen.getAllByLabelText("Expand")[0]);
    await waitFor(() => expect(screen.getByText("main-store")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Immutability" }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getByText("Not attested yet")).toBeInTheDocument());
  });
});

describe("DatastoresPage — customer scope filter", () => {
  afterEach(() => {
    window.localStorage.removeItem("resurgam.selectedCustomerId");
  });

  it("shows the datastore tree unfiltered when no customer is selected platform-wide", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<DatastoresPage />);

    await waitFor(() => expect(screen.getByText("Primary PBS")).toBeInTheDocument());
    await user.click(screen.getAllByLabelText("Expand")[0]);
    expect(screen.getByText("main-store")).toBeInTheDocument();
  });

  it("hides a datastore belonging to a different customer than the one selected platform-wide", async () => {
    setup();
    window.localStorage.setItem("resurgam.selectedCustomerId", "cust-other");
    renderWithProviders(<DatastoresPage />);

    await waitFor(() => expect(screen.getByText("Nothing here yet.")).toBeInTheDocument());
    expect(screen.queryByText("Primary PBS")).not.toBeInTheDocument();
  });

  it("keeps showing a datastore that belongs to the selected customer", async () => {
    setup();
    window.localStorage.setItem("resurgam.selectedCustomerId", "cust-1");
    const user = userEvent.setup();
    renderWithProviders(<DatastoresPage />);

    await waitFor(() => expect(screen.getByText("Primary PBS")).toBeInTheDocument());
    await user.click(screen.getAllByLabelText("Expand")[0]);
    expect(screen.getByText("main-store")).toBeInTheDocument();
  });
});

describe("DatastoresPage — datastores removed from PBS", () => {
  const REMOVED: DatastoreOut = {
    ...DATASTORE,
    id: "ds-gone",
    name: "gone-store",
    removed_from_pbs_at: "2026-10-01T08:00:00Z",
  };

  it("keeps a removed datastore visible with a badge and hides its PBS-side actions", async () => {
    setup();
    vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE, REMOVED]);
    renderWithProviders(<DatastoresPage />);

    const removedRow = await screen.findByTestId("datastore-row-gone-store");
    expect(within(removedRow).getByText("Removed from PBS")).toBeInTheDocument();
    expect(within(removedRow).queryByRole("button", { name: "Maintenance" })).not.toBeInTheDocument();
    expect(within(removedRow).queryByRole("button", { name: "Jobs" })).not.toBeInTheDocument();
    expect(within(removedRow).getByRole("button", { name: "Immutability" })).toBeInTheDocument();

    const liveRow = screen.getByTestId("datastore-row-main-store");
    expect(within(liveRow).queryByText("Removed from PBS")).not.toBeInTheDocument();
    expect(within(liveRow).getByRole("button", { name: "Maintenance" })).toBeInTheDocument();

    // No maintenance-mode lookup against PBS for a datastore it no longer has.
    await waitFor(() => expect(pbsInstancesApi.getMaintenanceMode).toHaveBeenCalledWith("pbs-1", "main-store"));
    expect(pbsInstancesApi.getMaintenanceMode).not.toHaveBeenCalledWith("pbs-1", "gone-store");
  });

  it("says in the import panel that datastores are discovered automatically", async () => {
    setup();
    vi.mocked(pbsInstancesApi.availableDatastores).mockResolvedValue([]);
    const user = userEvent.setup();
    renderWithProviders(<DatastoresPage />);

    await waitFor(() => expect(screen.getByText("Primary PBS")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Import" }));

    expect(screen.getByText(/discovered and imported automatically/)).toBeInTheDocument();
  });
});

const FINGERPRINT = Array(32).fill("ab").join(":");

function loginAs(roles: string[]) {
  const user = {
    user_id: "u-me",
    email: "me@example.com",
    display_name: "Me",
    is_breakglass: false,
    assignments: roles.map((role) => ({ role, scope_type: "global", scope_id: null })),
  } as WhoAmIResponse;
  window.localStorage.setItem("resurgam.token", "fake-token");
  vi.mocked(authApi.whoami).mockResolvedValue(user);
}

describe("DatastoresPage encryption keys", () => {
  afterEach(() => {
    window.localStorage.removeItem("resurgam.token");
    vi.clearAllMocks();
  });

  it("flags backups whose key RESURGAM does not have", async () => {
    loginAs(["platform_admin"]);
    setup();
    vi.mocked(datastoresApi.list).mockResolvedValue([
      { ...DATASTORE, key_fingerprints_in_use: [FINGERPRINT], keys_missing: [FINGERPRINT], encrypted_snapshot_count: 3 },
    ]);
    const user = userEvent.setup();
    renderWithProviders(<DatastoresPage />);
    await waitFor(() => expect(screen.getByText("Primary PBS")).toBeInTheDocument());
    await user.click(screen.getAllByLabelText("Expand")[0]);
    await waitFor(() => expect(screen.getByText("main-store")).toBeInTheDocument());

    expect(screen.getByText("Keys missing: 1")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Encryption key" })).not.toBeInTheDocument();
  });
});
