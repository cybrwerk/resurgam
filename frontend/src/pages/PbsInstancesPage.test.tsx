import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { PbsInstanceOut, RequiredPrivilegesOut, TestConnectionResponse } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { PbsInstancesPage } from "./PbsInstancesPage";

vi.mock("../api/resources", () => ({
  pbsInstancesApi: {
    list: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    testConnection: vi.fn(),
    requiredPrivileges: vi.fn(),
    probeTls: vi.fn(),
  },
  pbsJobsApi: {
    remoteCapabilities: vi.fn(),
    remotes: vi.fn(),
  },
}));

const { pbsInstancesApi, pbsJobsApi } = await import("../api/resources");

const SAMPLE_INSTANCE: PbsInstanceOut = {
  id: "11111111-1111-1111-1111-111111111111",
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

const REQUIRED: RequiredPrivilegesOut = {
  privileges: [
    { name: "Sys.Audit", path: "/", reason: "Read version and tasks." },
    { name: "Remote.Read", path: "/remote", reason: "Run pull sync jobs." },
    // A privilege the frontend has no translation for (added to the backend later).
    { name: "Datastore.FutureThing", path: "/datastore", reason: "Backend-only explanation." },
  ],
  roles: ["Audit", "DatastoreAdmin", "RemoteAdmin"],
  manual_commands: ["proxmox-backup-manager user generate-token resurgam@pbs resurgam"],
  default_token_name: "resurgam",
};

const MISSING_RESULT: TestConnectionResponse = {
  reachable: true,
  status: "missing_privileges",
  version: "4.0.1",
  error: "Connected, but the token is missing 1 required privilege(s): Remote.Read.",
  missing_privileges: ["Remote.Read"],
  permissions_checked: true,
  resolved_ip: "192.0.2.10",
  checked_at: "2026-10-06T10:00:00Z",
};

async function openCreateForm(user: ReturnType<typeof userEvent.setup>) {
  vi.mocked(pbsInstancesApi.list).mockResolvedValue([]);
  vi.mocked(pbsInstancesApi.requiredPrivileges).mockResolvedValue(REQUIRED);
  renderWithProviders(<PbsInstancesPage />);
  await waitFor(() => expect(screen.getByText("Nothing here yet.")).toBeInTheDocument());
  await user.click(screen.getByRole("button", { name: "Create" }));
  await user.type(screen.getByLabelText("Display name"), "PBS 1");
  await user.type(screen.getByLabelText("Host"), "pbs.example");
}

describe("PbsInstancesPage", () => {
  it("shows a loading state while the request is in flight", () => {
    vi.mocked(pbsInstancesApi.list).mockReturnValue(new Promise(() => {}));
    renderWithProviders(<PbsInstancesPage />);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows an empty state when there are no instances", async () => {
    vi.mocked(pbsInstancesApi.list).mockResolvedValue([]);
    renderWithProviders(<PbsInstancesPage />);
    await waitFor(() => expect(screen.getByText("Nothing here yet.")).toBeInTheDocument());
  });

  it("shows a forbidden message on a 403, not a crash", async () => {
    vi.mocked(pbsInstancesApi.list).mockRejectedValue(new ApiError(403, "forbidden", "no access"));
    renderWithProviders(<PbsInstancesPage />);
    await waitFor(() => expect(screen.getByText("You don't have access to this.")).toBeInTheDocument());
  });

  it("shows a retry option on a generic error", async () => {
    vi.mocked(pbsInstancesApi.list).mockRejectedValue(new ApiError(500, "internal_error", "boom"));
    renderWithProviders(<PbsInstancesPage />);
    await waitFor(() => expect(screen.getByText("Retry")).toBeInTheDocument());
  });

  it("renders instances with a health badge when the request succeeds", async () => {
    vi.mocked(pbsInstancesApi.list).mockResolvedValue([SAMPLE_INSTANCE]);
    renderWithProviders(<PbsInstancesPage />);
    await waitFor(() => expect(screen.getByText("Primary PBS")).toBeInTheDocument());
    expect(screen.getByText("Healthy")).toBeInTheDocument();
  });

  it("lists remotes when the detail modal is opened", async () => {
    vi.mocked(pbsInstancesApi.list).mockResolvedValue([SAMPLE_INSTANCE]);
    vi.mocked(pbsJobsApi.remoteCapabilities).mockResolvedValue({ can_read: true, can_write: true });
    vi.mocked(pbsJobsApi.remotes).mockResolvedValue([
      { name: "offsite", host: "pbs2.example.com", auth_id: "sync@pbs!token", port: null, fingerprint: null, comment: null },
    ]);
    const user = userEvent.setup();
    renderWithProviders(<PbsInstancesPage />);

    await waitFor(() => expect(screen.getByText("Primary PBS")).toBeInTheDocument());
    await user.click(screen.getByText("Primary PBS"));

    await waitFor(() => expect(screen.getByText("offsite")).toBeInTheDocument());
    expect(screen.getByText("pbs2.example.com")).toBeInTheDocument();
    expect(pbsJobsApi.remotes).toHaveBeenCalledWith(SAMPLE_INSTANCE.id);
  });

  it("creates with separate token fields and runs the connection test automatically", async () => {
    const user = userEvent.setup();
    await openCreateForm(user);
    vi.mocked(pbsInstancesApi.create).mockResolvedValue({ ...SAMPLE_INSTANCE, display_name: "PBS 1" });
    vi.mocked(pbsInstancesApi.testConnection).mockResolvedValue(MISSING_RESULT);

    await user.type(screen.getByLabelText("Token ID"), "resurgam@pbs!resurgam");
    await user.type(screen.getByLabelText("Token secret"), "d63e505a-e3ec-449a-9bc7-1da610d4ccde");
    expect(screen.getByLabelText("Token secret")).toHaveAttribute("type", "password");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(pbsInstancesApi.create).toHaveBeenCalled());
    expect(vi.mocked(pbsInstancesApi.create).mock.calls[0][0]).toMatchObject({
      port: 8007,
      token_id: "resurgam@pbs!resurgam",
      token_secret: "d63e505a-e3ec-449a-9bc7-1da610d4ccde",
    });
    expect(vi.mocked(pbsInstancesApi.create).mock.calls[0][0]).not.toHaveProperty("api_token");
    await waitFor(() => expect(pbsInstancesApi.testConnection).toHaveBeenCalledWith(SAMPLE_INSTANCE.id));
    expect(await screen.findByText("Missing privileges:")).toBeInTheDocument();
    expect(screen.getByText("Remote.Read")).toBeInTheDocument();
  });

  it("keeps a cleared port empty instead of snapping to 0 and validates it on submit", async () => {
    const user = userEvent.setup();
    await openCreateForm(user);
    const port = screen.getByLabelText("Port");
    await user.clear(port);
    expect(port).toHaveValue(null);
    await user.type(port, "70000");
    await user.type(screen.getByLabelText("Token ID"), "a@pam!b");
    await user.type(screen.getByLabelText("Token secret"), "s");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText("The port must be a whole number between 1 and 65535.")).toBeInTheDocument();
    expect(pbsInstancesApi.create).not.toHaveBeenCalled();
  });

  it("sends login credentials in credentials mode", async () => {
    const user = userEvent.setup();
    await openCreateForm(user);
    vi.mocked(pbsInstancesApi.create).mockResolvedValue(SAMPLE_INSTANCE);
    vi.mocked(pbsInstancesApi.testConnection).mockResolvedValue({ ...MISSING_RESULT, status: "ok", error: null, missing_privileges: [] });

    await user.click(screen.getByLabelText("Login credentials (RESURGAM creates its own token)"));
    await user.type(screen.getByLabelText("User name"), "root@pam");
    await user.type(screen.getByLabelText("Password"), "pw");
    await user.click(screen.getByRole("button", { name: "Create and set up token" }));

    await waitFor(() => expect(pbsInstancesApi.create).toHaveBeenCalled());
    expect(vi.mocked(pbsInstancesApi.create).mock.calls[0][0]).toMatchObject({
      credentials: { username: "root@pam", password: "pw", token_name: "resurgam" },
    });
    expect(await screen.findByText(/holds every required privilege/)).toBeInTheDocument();
  });

  it("shows the required permissions and manual commands before connecting", async () => {
    const user = userEvent.setup();
    await openCreateForm(user);
    await user.click(screen.getByText("Required permissions"));
    expect(await screen.findByText("Remote.Read")).toBeInTheDocument();
    // Known privileges show the UI's own (translated) reason...
    expect(screen.getByText("Create, edit and run pull sync jobs (read data from the source remote).")).toBeInTheDocument();
    // ...and anything the UI doesn't know yet falls back to the backend's text.
    expect(screen.getByText("Backend-only explanation.")).toBeInTheDocument();
    expect(screen.getByText(/generate-token resurgam@pbs resurgam/)).toBeInTheDocument();
  });

  it("explains the token ID field accessibly", async () => {
    const user = userEvent.setup();
    await openCreateForm(user);
    const tokenId = screen.getByLabelText("Token ID");
    const hintId = tokenId.getAttribute("aria-describedby")!;
    expect(document.getElementById(hintId)).toHaveTextContent("user@realm!tokenname");
    const info = screen.getAllByRole("button", { name: "More information" })[0];
    await user.click(info);
    expect(info).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(/Configuration > Access Control > API Token/)).toBeInTheDocument();
  });

  it("shows the last heartbeat in the list", async () => {
    vi.mocked(pbsInstancesApi.list).mockResolvedValue([
      { ...SAMPLE_INSTANCE, last_health_status: "auth_failed", last_health_check_at: new Date().toISOString() },
    ]);
    renderWithProviders(<PbsInstancesPage />);
    expect(await screen.findByText("Token rejected")).toBeInTheDocument();
    expect(screen.getByText(/Last heartbeat/)).toBeInTheDocument();
  });

  it("flags a connection imported without its token secret", async () => {
    vi.mocked(pbsInstancesApi.list).mockResolvedValue([
      { ...SAMPLE_INSTANCE, token_secret_set: false, last_health_status: "auth_failed", last_health_check_at: null },
    ]);
    renderWithProviders(<PbsInstancesPage />);
    // "Secret missing" (what to do), not the generic "Token rejected".
    expect(await screen.findByText("Secret missing")).toBeInTheDocument();
    expect(screen.queryByText("Token rejected")).not.toBeInTheDocument();
  });

  it("re-tests automatically after the token is changed, but not after a rename", async () => {
    vi.mocked(pbsInstancesApi.list).mockResolvedValue([{ ...SAMPLE_INSTANCE, token_id: "root@pam!resurgam" }]);
    vi.mocked(pbsInstancesApi.requiredPrivileges).mockResolvedValue(REQUIRED);
    vi.mocked(pbsJobsApi.remoteCapabilities).mockResolvedValue({ can_read: true, can_write: true });
    vi.mocked(pbsJobsApi.remotes).mockResolvedValue([]);
    vi.mocked(pbsInstancesApi.update).mockResolvedValue(SAMPLE_INSTANCE);
    vi.mocked(pbsInstancesApi.testConnection).mockClear();
    vi.mocked(pbsInstancesApi.testConnection).mockResolvedValue(MISSING_RESULT);
    const user = userEvent.setup();
    renderWithProviders(<PbsInstancesPage />);
    await user.click(await screen.findByText("Primary PBS"));
    expect(screen.getByText("root@pam!resurgam")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.clear(screen.getByLabelText("Display name"));
    await user.type(screen.getByLabelText("Display name"), "Renamed");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(pbsInstancesApi.update).toHaveBeenCalledTimes(1));
    expect(vi.mocked(pbsInstancesApi.update).mock.calls[0][1]).not.toHaveProperty("token_secret");
    expect(pbsInstancesApi.testConnection).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.type(screen.getByLabelText("Token secret"), "new-secret");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(pbsInstancesApi.update).toHaveBeenCalledTimes(2));
    expect(vi.mocked(pbsInstancesApi.update).mock.calls[1][1]).toMatchObject({ token_secret: "new-secret" });
    await waitFor(() => expect(pbsInstancesApi.testConnection).toHaveBeenCalledWith(SAMPLE_INSTANCE.id));
  });
});
