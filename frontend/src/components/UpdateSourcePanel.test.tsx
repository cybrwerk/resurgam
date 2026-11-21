import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { UpdateSourceDownloadOut, UpdateSourceOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { UpdateSourcePanel, guessProvider } from "./UpdateSourcePanel";

vi.mock("../api/resources", () => ({
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

const { updateSourceApi } = await import("../api/resources");
const api = vi.mocked(updateSourceApi);

const UNCONFIGURED: UpdateSourceOut = {
  configured: false,
  provider: "github",
  repository_url: "https://github.com/cybrwerk/resurgam",
  token_configured: false,
  tls_verify: true,
  include_prereleases: false,
  auto_check: true,
  check_interval_hours: 24,
  current_version: "0.2.3",
  last_checked_at: null,
  last_check_error: null,
  latest_version: null,
  available_releases: [],
  active_download: null,
};

const CONFIGURED: UpdateSourceOut = {
  ...UNCONFIGURED,
  configured: true,
  token_configured: true,
  last_checked_at: "2026-10-09T10:00:00Z",
  latest_version: "0.3.0",
  available_releases: [
    {
      version: "0.3.0",
      tag_name: "v0.3.0",
      name: "RESURGAM 0.3.0",
      released_at: "2026-10-09T09:00:00Z",
      notes: "New things",
      web_url: "https://github.com/cybrwerk/resurgam/releases/tag/v0.3.0",
      prerelease: false,
      bundle_name: "resurgam-0.3.0.bundle",
      bundle_size: 1024,
    },
  ],
};

function downloadStatus(overrides: Partial<UpdateSourceDownloadOut>): UpdateSourceDownloadOut {
  return {
    download_id: "d1",
    version: "0.3.0",
    source_asset: "resurgam-0.3.0.bundle",
    state: "running",
    bytes_done: 0,
    bytes_total: 1024,
    bundle: null,
    signature_prechecked: false,
    message: null,
    requested_by: "admin@example.com",
    started_at: "2026-10-09T10:00:00Z",
    finished_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.resetAllMocks();
});

describe("guessProvider", () => {
  it("detects the provider from the host", () => {
    expect(guessProvider("https://github.com/a/b")).toBe("github");
    expect(guessProvider("https://gitlab.example.com/a/b")).toBe("gitlab");
    expect(guessProvider("https://git.example.com/a/b")).toBeNull();
    expect(guessProvider("not a url")).toBeNull();
  });
});

describe("UpdateSourcePanel", () => {
  it("pre-fills RESURGAM's GitHub repository and saves the token without echoing it", async () => {
    api.get.mockResolvedValue(UNCONFIGURED);
    api.save.mockResolvedValue({ ...UNCONFIGURED, configured: true, token_configured: true });
    renderWithProviders(<UpdateSourcePanel canDownload onBundleReady={vi.fn()} />);

    const url = await screen.findByLabelText("Repository URL");
    expect(url).toHaveValue("https://github.com/cybrwerk/resurgam");
    await userEvent.type(screen.getByLabelText(/Access token/), "ghp_secret");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(api.save).toHaveBeenCalled());
    expect(api.save.mock.calls[0][0]).toMatchObject({
      provider: "github",
      repository_url: "https://github.com/cybrwerk/resurgam",
      token: "ghp_secret",
      auto_check: true,
    });
    expect(await screen.findByText("Update source saved.")).toBeInTheDocument();
    // Saved: the form gives way to the read-only summary.
    const summary = screen.getByTestId("update-source-summary");
    expect(summary).toHaveTextContent("Stored");
    expect(summary).not.toHaveTextContent("ghp_secret");
    expect(screen.queryByLabelText(/Access token/)).not.toBeInTheDocument();
  });

  it("switches the provider when a GitLab URL is pasted and omits an empty token", async () => {
    api.get.mockResolvedValue({ ...CONFIGURED, available_releases: [] });
    api.test.mockResolvedValue({
      ok: true,
      repository: "ops/resurgam",
      private: true,
      releases_found: 3,
      latest_release: "0.3.0",
      error: null,
    });
    renderWithProviders(<UpdateSourcePanel canDownload onBundleReady={vi.fn()} />);

    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const url = screen.getByLabelText("Repository URL");
    await userEvent.clear(url);
    await userEvent.type(url, "https://gitlab.example.com/ops/resurgam");
    expect(screen.getByLabelText("Provider")).toHaveValue("gitlab");
    await userEvent.click(screen.getByRole("button", { name: "Test connection" }));

    await waitFor(() => expect(api.test).toHaveBeenCalled());
    expect(api.test.mock.calls[0][0]).toEqual({
      provider: "gitlab",
      repository_url: "https://gitlab.example.com/ops/resurgam",
      tls_verify: true,
    });
    expect(await screen.findByTestId("update-source-test-result")).toHaveTextContent("Connected to ops/resurgam");
  });

  it("lists newer releases, downloads one and hands the bundle to the verify step", async () => {
    api.get.mockResolvedValue(CONFIGURED);
    api.startDownload.mockResolvedValue(downloadStatus({}));
    const done = downloadStatus({
      state: "succeeded",
      bytes_done: 1024,
      bundle: "resurgam-0.3.0-d1d1d1d1.bundle",
      finished_at: "2026-10-09T10:01:00Z",
    });
    api.download.mockResolvedValue(done);
    const onBundleReady = vi.fn();
    renderWithProviders(<UpdateSourcePanel canDownload onBundleReady={onBundleReady} pollIntervalMs={10} />);

    const releases = await screen.findByTestId("update-source-releases");
    expect(within(releases).getByText("0.3.0")).toBeInTheDocument();
    await userEvent.click(within(releases).getByRole("button", { name: "Download and verify" }));

    await waitFor(() => expect(onBundleReady).toHaveBeenCalledWith(done));
    expect(api.startDownload).toHaveBeenCalledWith("0.3.0");
    expect(screen.getByTestId("update-source-download")).toHaveTextContent("Bundle 0.3.0 downloaded");
  });

  it("disables downloads outside the appliance and shows the last check error", async () => {
    api.get.mockResolvedValue({ ...CONFIGURED, last_check_error: "GitHub answered 404 for the releases." });
    renderWithProviders(<UpdateSourcePanel canDownload={false} onBundleReady={vi.fn()} />);

    const releases = await screen.findByTestId("update-source-releases");
    expect(within(releases).getByRole("button", { name: "Download and verify" })).toBeDisabled();
    expect(within(releases).getByText("GitHub answered 404 for the releases.")).toBeInTheDocument();
  });

  it("checks now and shows up to date", async () => {
    api.get.mockResolvedValue({ ...CONFIGURED, available_releases: [], last_checked_at: null });
    api.check.mockResolvedValue({ ...CONFIGURED, available_releases: [], latest_version: null });
    renderWithProviders(<UpdateSourcePanel canDownload onBundleReady={vi.fn()} />);

    await userEvent.click(await screen.findByRole("button", { name: "Check now" }));
    expect(await screen.findByText("RESURGAM is up to date.")).toBeInTheDocument();
  });

  it("shows a configured source as a summary with Edit and Remove", async () => {
    api.get.mockResolvedValueOnce({ ...CONFIGURED, check_interval_hours: 6 }).mockResolvedValue(UNCONFIGURED);
    api.remove.mockResolvedValue(undefined);
    renderWithProviders(<UpdateSourcePanel canDownload onBundleReady={vi.fn()} />);

    const summary = await screen.findByTestId("update-source-summary");
    expect(summary).toHaveTextContent("https://github.com/cybrwerk/resurgam");
    expect(summary).toHaveTextContent("GitHub");
    expect(summary).toHaveTextContent("Every 6 hours");
    expect(screen.queryByLabelText("Repository URL")).not.toBeInTheDocument();
    // Releases come before the source settings.
    const releases = screen.getByTestId("update-source-releases");
    expect(releases.compareDocumentPosition(summary) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Remove" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove the update source?" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(api.remove).toHaveBeenCalled());
    expect(await screen.findByLabelText("Repository URL")).toBeInTheDocument();
  });

  it("tells the admin when a host change removed the stored token", async () => {
    api.get.mockResolvedValue({ ...CONFIGURED, available_releases: [] });
    api.save.mockResolvedValue({
      ...CONFIGURED,
      repository_url: "https://gitlab.example.com/ops/resurgam",
      provider: "gitlab",
      token_configured: false,
      token_cleared: true,
      available_releases: [],
    });
    renderWithProviders(<UpdateSourcePanel canDownload onBundleReady={vi.fn()} />);

    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const url = screen.getByLabelText("Repository URL");
    await userEvent.clear(url);
    await userEvent.type(url, "https://gitlab.example.com/ops/resurgam");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/stored access token was removed/)).toBeInTheDocument();
  });

  it("shows no download button for a release without a bundle", async () => {
    api.get.mockResolvedValue({
      ...CONFIGURED,
      available_releases: [{ ...CONFIGURED.available_releases[0], bundle_name: null, bundle_size: null }],
    });
    renderWithProviders(<UpdateSourcePanel canDownload onBundleReady={vi.fn()} />);

    const releases = await screen.findByTestId("update-source-releases");
    expect(within(releases).queryByRole("button", { name: "Download and verify" })).not.toBeInTheDocument();
    expect(within(releases).getByText("No bundle attached")).toBeInTheDocument();
  });
});
