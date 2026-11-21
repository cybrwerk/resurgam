import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { UpdateBundleOut, UpdateRequestStatusOut, UpdatesOverviewOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { UpdatesPanel, isTransientPollError } from "./UpdatesPanel";

vi.mock("../api/resources", () => ({
  updatesApi: {
    overview: vi.fn(),
    bundles: vi.fn(),
    uploadBundle: vi.fn(),
    deleteBundle: vi.fn(),
    requests: vi.fn(),
    createRequest: vi.fn(),
    requestStatus: vi.fn(),
    history: vi.fn(),
    backups: vi.fn(),
  },
  updateSourceApi: {
    get: vi.fn(),
    save: vi.fn(),
    remove: vi.fn(),
    test: vi.fn(),
    check: vi.fn(),
    startDownload: vi.fn(),
    download: vi.fn(),
  },
}));

const { updatesApi, updateSourceApi } = await import("../api/resources");
const api = vi.mocked(updatesApi);
const sourceApi = vi.mocked(updateSourceApi);

const OVERVIEW: UpdatesOverviewOut = {
  available: true,
  unavailable_reason: null,
  current_version: "0.2.0",
  max_bundle_bytes: 4 * 1024 ** 3,
  active_apply: null,
};

const BUNDLE: UpdateBundleOut = {
  name: "abc.bundle",
  size_bytes: 1024,
  uploaded_at: "2026-10-07T10:00:00Z",
  version: "0.3.0",
  problem: null,
  signature_prechecked: true,
};

function status(overrides: Partial<UpdateRequestStatusOut>): UpdateRequestStatusOut {
  return {
    request_id: "verify1",
    action: "verify",
    bundle: BUNDLE.name,
    requested_by: "admin@example.com",
    requested_at: "2026-10-07T10:00:00Z",
    state: "queued",
    step: null,
    from_version: "0.2.0",
    to_version: "0.3.0",
    message: null,
    log: [],
    manifest: null,
    backup_file: null,
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

const VERIFIED = status({
  state: "succeeded",
  step: "done",
  message: "Bundle is valid.",
  manifest: { version: "0.3.0", changelog: "- New updates page\n- Fixes", min_upgrade_from: "0.2.0" },
});

/** A file whose first block carries the tar "ustar" magic at offset 257. */
function tarFile(name = "resurgam-0.3.0.bundle"): File {
  const bytes = new Uint8Array(1024);
  bytes.set(new TextEncoder().encode("ustar"), 257);
  return new File([bytes], name, { type: "application/octet-stream" });
}

beforeEach(() => {
  vi.resetAllMocks();
  api.overview.mockResolvedValue(OVERVIEW);
  api.bundles.mockResolvedValue([]);
  api.history.mockResolvedValue([]);
  api.backups.mockResolvedValue([]);
  sourceApi.get.mockResolvedValue({
    configured: false,
    provider: "github",
    repository_url: "https://github.com/cybrwerk/resurgam",
    token_configured: false,
    tls_verify: true,
    include_prereleases: false,
    auto_check: true,
    check_interval_hours: 24,
    current_version: "0.2.0",
    last_checked_at: null,
    last_check_error: null,
    latest_version: null,
    available_releases: [],
    active_download: null,
  });
});

async function uploadAndVerify(user: ReturnType<typeof userEvent.setup>) {
  api.uploadBundle.mockImplementation(async (_file, onProgress) => {
    onProgress?.(512, 1024);
    return BUNDLE;
  });
  api.createRequest.mockResolvedValueOnce(status({}));
  api.requestStatus.mockResolvedValueOnce(status({ state: "running", step: "verify" })).mockResolvedValue(VERIFIED);

  renderWithProviders(<UpdatesPanel pollIntervalMs={5} />);
  await user.upload(await screen.findByLabelText("Bundle file"), tarFile());
  await user.click(screen.getByRole("button", { name: "Upload and verify" }));
  await screen.findByTestId("update-changelog");
}

describe("UpdatesPanel", () => {
  it("shows the version and explains when updates are unavailable", async () => {
    api.overview.mockResolvedValue({ ...OVERVIEW, available: false, unavailable_reason: "No updates dir." });
    renderWithProviders(<UpdatesPanel />);
    expect(await screen.findByTestId("updates-current-version")).toHaveTextContent("0.2.0");
    expect(screen.getByText(/only be installed on the RESURGAM appliance/)).toHaveTextContent("No updates dir.");
    expect(api.bundles).not.toHaveBeenCalled();
  });

  it("badges the installed version when the update source found a newer release", async () => {
    const unconfigured = await sourceApi.get();
    sourceApi.get.mockResolvedValue({
      ...unconfigured,
      configured: true,
      latest_version: "0.2.8",
      available_releases: [
        {
          version: "0.2.8",
          tag_name: "v0.2.8",
          name: null,
          released_at: null,
          notes: null,
          web_url: null,
          prerelease: false,
          bundle_name: "resurgam-0.2.8.bundle",
          bundle_size: 10,
        },
      ],
    });
    renderWithProviders(<UpdatesPanel />);
    expect(await screen.findByTestId("updates-newer-available")).toHaveTextContent("0.2.8 available");
  });

  it("uploads a bundle, requests a verify automatically and shows the result", async () => {
    const user = userEvent.setup();
    await uploadAndVerify(user);

    expect(api.uploadBundle).toHaveBeenCalledTimes(1);
    expect(api.createRequest).toHaveBeenCalledWith("verify", "abc.bundle");
    expect(screen.getByTestId("update-signature")).toHaveTextContent("Valid");
    expect(screen.getByTestId("update-changelog").textContent).toBe("- New updates page\n- Fixes");
    expect(screen.getByTestId("update-verify")).toHaveTextContent("0.2.0");
    expect(screen.getByRole("button", { name: "Update now" })).toBeInTheDocument();
  });

  it("rejects a non-tar file before uploading", async () => {
    const user = userEvent.setup();
    renderWithProviders(<UpdatesPanel />);
    const notTar = new File([new Uint8Array(1024)], "renamed-photo.bundle");
    await user.upload(await screen.findByLabelText("Bundle file"), notTar);
    await user.click(screen.getByRole("button", { name: "Upload and verify" }));
    expect(await screen.findByText(/not a RESURGAM update bundle/)).toBeInTheDocument();
    expect(api.uploadBundle).not.toHaveBeenCalled();
  });

  it("shows why verification rejected a bundle and offers no update", async () => {
    const user = userEvent.setup();
    api.uploadBundle.mockResolvedValue(BUNDLE);
    api.createRequest.mockResolvedValueOnce(status({}));
    api.requestStatus.mockResolvedValue(
      status({ state: "failed", step: "verify", message: "The signature does not match the release key." }),
    );
    renderWithProviders(<UpdatesPanel pollIntervalMs={5} />);
    await user.upload(await screen.findByLabelText("Bundle file"), tarFile());
    await user.click(screen.getByRole("button", { name: "Upload and verify" }));

    expect(await screen.findByTestId("update-reject-reason")).toHaveTextContent(
      "The signature does not match the release key.",
    );
    expect(screen.getByTestId("update-signature")).toHaveTextContent("Rejected");
    expect(screen.queryByRole("button", { name: "Update now" })).not.toBeInTheDocument();
  });

  it("shows the upload error from the backend", async () => {
    const user = userEvent.setup();
    api.uploadBundle.mockRejectedValue(new ApiError(413, "bundle_too_large", "The bundle is too large."));
    renderWithProviders(<UpdatesPanel />);
    await user.upload(await screen.findByLabelText("Bundle file"), tarFile());
    await user.click(screen.getByRole("button", { name: "Upload and verify" }));
    expect(await screen.findByText("The bundle is too large.")).toBeInTheDocument();
    expect(api.createRequest).not.toHaveBeenCalled();
  });

  it("confirms in the page, then follows the apply through a backend restart", async () => {
    const user = userEvent.setup();
    await uploadAndVerify(user);

    await user.click(screen.getByRole("button", { name: "Update now" }));
    const confirm = screen.getByRole("group", { name: "Confirm update" });
    expect(confirm).toHaveTextContent("A database backup is taken first");
    expect(confirm).toHaveTextContent("unavailable for a few minutes");
    expect(api.createRequest).toHaveBeenCalledTimes(1);

    const apply = (overrides: Partial<UpdateRequestStatusOut>) =>
      status({ request_id: "apply1", action: "apply", ...overrides });
    let restartPolls = 0;
    let releaseRestart = false;
    api.createRequest.mockResolvedValueOnce(apply({}));
    api.requestStatus.mockReset();
    api.requestStatus
      .mockResolvedValueOnce(apply({ state: "running", step: "backup", log: ["pg_dump done"] }))
      .mockImplementation(async () => {
        if (!releaseRestart) {
          restartPolls += 1;
          throw new TypeError("Failed to fetch");
        }
        return apply({
          state: "succeeded",
          step: "done",
          backup_file: "pre-0.3.0-20261007T100000Z.dump",
          log: ["pg_dump done", "health ok"],
        });
      });

    await user.click(within(confirm).getByRole("button", { name: "Install version 0.3.0" }));
    expect(api.createRequest).toHaveBeenLastCalledWith("apply", "abc.bundle");

    expect(await screen.findByText(/Restarting… the services are being replaced/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await waitFor(() => expect(restartPolls).toBeGreaterThan(1));
    releaseRestart = true;

    expect(await screen.findByTestId("update-result")).toHaveTextContent("Version 0.3.0 is installed.");
    expect(screen.queryByText(/Restarting…/)).not.toBeInTheDocument();
    expect(screen.getByTestId("update-step-health")).toHaveTextContent("Done");
    expect(screen.getByTestId("update-log")).toHaveTextContent("health ok");
    expect(screen.getByText("pre-0.3.0-20261007T100000Z.dump")).toBeInTheDocument();
    await waitFor(() => expect(api.history.mock.calls.length).toBeGreaterThan(1));
  });

  it("shows a rolled-back update with its reason", async () => {
    api.overview.mockResolvedValue({
      ...OVERVIEW,
      active_apply: status({ request_id: "apply9", action: "apply", state: "running", step: "health" }),
    });
    api.requestStatus.mockResolvedValue(
      status({
        request_id: "apply9",
        action: "apply",
        state: "rolled_back",
        step: "rollback",
        message: "The backend did not become ready within 300 s.",
      }),
    );
    renderWithProviders(<UpdatesPanel pollIntervalMs={5} />);

    const result = await screen.findByTestId("update-result");
    expect(result).toHaveTextContent("version 0.2.0 was restored");
    expect(result).toHaveTextContent("The backend did not become ready within 300 s.");
    expect(within(screen.getByTestId("update-progress")).getAllByText("Rolled back").length).toBeGreaterThan(0);
    expect(api.requestStatus).toHaveBeenCalledWith("apply9");
  });

  it("lists history, backups and bundles; deleting asks in the page first", async () => {
    const user = userEvent.setup();
    api.bundles.mockResolvedValue([BUNDLE]);
    api.history.mockResolvedValue([
      {
        request_id: "a",
        state: "succeeded",
        from_version: "0.1.0",
        to_version: "0.2.0",
        message: "Updated.",
        backup_file: null,
        requested_by: "admin@example.com",
        started_at: "2026-10-01T10:00:00Z",
        finished_at: "2026-10-01T10:05:00Z",
      },
    ]);
    api.backups.mockResolvedValue([
      { name: "pre-0.2.0-20261001T100000Z.dump", size_bytes: 2048, created_at: "2026-10-01T10:00:00Z" },
    ]);
    api.deleteBundle.mockResolvedValue(undefined);
    renderWithProviders(<UpdatesPanel />);

    expect(await screen.findByText("0.1.0 → 0.2.0")).toBeInTheDocument();
    expect(await screen.findByText("pre-0.2.0-20261001T100000Z.dump")).toBeInTheDocument();
    expect(screen.getByText("2.0 KB")).toBeInTheDocument();

    await user.click(await screen.findByRole("button", { name: "Delete" }));
    expect(api.deleteBundle).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Delete bundle" }));
    expect(api.deleteBundle).toHaveBeenCalledWith("abc.bundle");
  });
});

describe("isTransientPollError", () => {
  it("treats network failures and gateway errors as a restart", () => {
    expect(isTransientPollError(new TypeError("Failed to fetch"))).toBe(true);
    expect(isTransientPollError(new ApiError(502, "x", "bad gateway"))).toBe(true);
    expect(isTransientPollError(new ApiError(404, "update_request_not_found", "gone"))).toBe(false);
  });
});
