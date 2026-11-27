import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BackupGapsOut, OffsiteGapOut, WhoAmIResponse } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { BackupGapsPage } from "./BackupGapsPage";

vi.mock("../api/resources", () => ({
  authApi: { whoami: vi.fn() },
  backupGapsApi: {
    get: vi.fn(),
    exceptions: vi.fn(),
    createException: vi.fn(),
    updateThresholds: vi.fn(),
    checkCluster: vi.fn(),
  },
  s3EndpointsApi: { list: vi.fn(), setLocation: vi.fn() },
}));

const { authApi, backupGapsApi, s3EndpointsApi } = await import("../api/resources");

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

function offsite(overrides: Partial<OffsiteGapOut>): OffsiteGapOut {
  return {
    backup_group_id: "g-1",
    workload_label: "web-01",
    backup_type: "vm",
    backup_id: "101",
    namespace: "",
    datastore_id: "ds-1",
    datastore_name: "store-a",
    pbs_instance_name: "Site A",
    customer_id: null,
    customer_name: "Acme",
    status: "ok",
    raw_status: "ok",
    severity: null,
    copy_location: "Site B / store-b",
    copy_backup_group_id: "g-copy",
    sync_jobs: ["Site B: sync job pull-a"],
    latest_backup_at: "2026-11-26T02:00:00Z",
    latest_copy_at: "2026-11-26T02:00:00Z",
    oldest_uncopied_at: null,
    uncopied_count: 0,
    lag_hours: 0,
    exception: null,
    ...overrides,
  };
}

function gaps(): BackupGapsOut {
  return {
    summary: {
      unprotected_guests: 1,
      offsite_gaps: 1,
      offsite_critical: 0,
      offsite_max_lag_hours: 50,
      offsite_without_copy_job: 0,
      excepted: 0,
    },
    thresholds: { offsite_lag_warning_hours: 36, offsite_lag_critical_hours: 72 },
    pve_clusters: [{ pve_cluster_id: "c-1", pve_cluster_name: "PVE", checked_at: "2026-11-26T08:00:00Z", error: null }],
    unprotected_guests: [
      {
        pve_cluster_id: "c-1",
        pve_cluster_name: "PVE",
        vmid: 105,
        guest_type: "qemu",
        name: "web-new",
        reason: "no_job",
        first_seen_at: "2026-11-25T08:00:00Z",
        last_seen_at: "2026-11-26T08:00:00Z",
        exception: null,
      },
    ],
    offsite: [
      offsite({}),
      offsite({
        backup_group_id: "g-2",
        workload_label: "db-01",
        backup_id: "102",
        status: "lagging",
        raw_status: "lagging",
        severity: "warning",
        lag_hours: 50,
        uncopied_count: 2,
      }),
    ],
  };
}

describe("BackupGapsPage", () => {
  afterEach(() => {
    window.localStorage.removeItem("resurgam.token");
    vi.clearAllMocks();
  });

  it("lists only offsite gaps by default and can show all", async () => {
    loginAs(["viewer"]);
    vi.mocked(backupGapsApi.get).mockResolvedValue(gaps());
    renderWithProviders(<BackupGapsPage />);

    await waitFor(() => expect(screen.getByText("db-01")).toBeInTheDocument());
    expect(screen.queryByText("web-01")).not.toBeInTheDocument();
    expect(screen.getByText("Behind")).toBeInTheDocument();
    expect(screen.getByText("2 d")).toBeInTheDocument();
    // No admin tabs and no exception buttons for a viewer.
    expect(screen.queryByRole("tab", { name: /Unprotected guests/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept…" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("Only show gaps"));
    expect(screen.getByText("web-01")).toBeInTheDocument();
    expect(screen.getByText("Current")).toBeInTheDocument();
  });

  it("lets a platform admin accept an unprotected guest with a reason", async () => {
    loginAs(["platform_admin"]);
    vi.mocked(backupGapsApi.get).mockResolvedValue(gaps());
    vi.mocked(backupGapsApi.createException).mockResolvedValue({} as never);
    renderWithProviders(<BackupGapsPage />, { route: "/?tab=guests" });

    await waitFor(() => expect(screen.getByText("web-new")).toBeInTheDocument());
    expect(screen.getByRole("tab", { name: "Unprotected guests (1)" })).toBeInTheDocument();
    expect(screen.getByText("In no backup job")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Accept…" }));
    const dialog = await screen.findByRole("dialog");
    const submit = within(dialog).getByRole("button", { name: "Accept…" });
    fireEvent.change(within(dialog).getByLabelText("Reason"), { target: { value: "short" } });
    expect(submit).toBeDisabled();
    fireEvent.click(within(dialog).getByLabelText("Every guest of PVE"));
    fireEvent.change(within(dialog).getByLabelText("Reason"), { target: { value: "Lab-only cluster, no backups." } });
    fireEvent.click(submit);

    await waitFor(() =>
      expect(backupGapsApi.createException).toHaveBeenCalledWith({
        target_kind: "pve_cluster",
        pve_cluster_id: "c-1",
        reason: "Lab-only cluster, no backups.",
        expires_at: null,
      }),
    );
  });

  it("lets a platform admin mark an S3 endpoint on site with a reason", async () => {
    loginAs(["platform_admin"]);
    vi.mocked(backupGapsApi.get).mockResolvedValue(gaps());
    vi.mocked(s3EndpointsApi.list).mockResolvedValue([
      {
        id: "e-1",
        pbs_instance_id: "p-1",
        pbs_instance_name: "Site A",
        name: "rustfs",
        endpoint: "10.0.0.5",
        port: 9000,
        region: null,
        path_style: true,
        datastores: ["s3-test"],
        on_site: false,
        on_site_reason: null,
        on_site_changed_at: null,
        on_site_changed_by: null,
        removed_from_pbs_at: null,
      },
    ]);
    vi.mocked(s3EndpointsApi.setLocation).mockResolvedValue({} as never);
    renderWithProviders(<BackupGapsPage />, { route: "/?tab=s3" });

    await waitFor(() => expect(screen.getByText("rustfs")).toBeInTheDocument());
    expect(screen.getByText("10.0.0.5:9000")).toBeInTheDocument();
    expect(screen.getByText("Offsite")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Mark on site" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Reason"), { target: { value: "MinIO in the same rack." } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Mark on site" }));
    await waitFor(() =>
      expect(s3EndpointsApi.setLocation).toHaveBeenCalledWith("e-1", { on_site: true, reason: "MinIO in the same rack." }),
    );
  });

  it("shows a backup stored on S3 as fine, not as a gap", async () => {
    loginAs(["viewer"]);
    const data = gaps();
    data.offsite = [offsite({ workload_label: "s3-vm", status: "stored_offsite", raw_status: "stored_offsite" })];
    vi.mocked(backupGapsApi.get).mockResolvedValue(data);
    renderWithProviders(<BackupGapsPage />);
    await waitFor(() => expect(screen.getByLabelText("Only show gaps")).toBeInTheDocument());
    expect(screen.queryByText("s3-vm")).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Only show gaps"));
    expect(screen.getByText("Stored in S3")).toBeInTheDocument();
  });
});
