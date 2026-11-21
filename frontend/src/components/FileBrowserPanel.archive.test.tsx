import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { FileEntryOut, FileRestoreJobOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { FileBrowserPanel } from "./FileBrowserPanel";

vi.mock("../api/resources", () => ({
  fileRestoreApi: {
    createJob: vi.fn(),
    createArchiveJob: vi.fn(),
    getJob: vi.fn(),
    saveJobContent: vi.fn().mockResolvedValue(undefined),
  },
}));

const { fileRestoreApi } = await import("../api/resources");

const ENTRIES: FileEntryOut[] = [
  { path: "/etc/nginx", name: "nginx", type: "directory", size: null, mtime: null },
  { path: "/etc/hosts", name: "hosts", type: "file", size: 220, mtime: "2026-01-01T00:00:00Z" },
  { path: "/etc/Grüsse.txt", name: "Grüsse.txt", type: "file", size: 50, mtime: "2026-01-01T00:00:00Z" },
];

function makeJob(overrides: Partial<FileRestoreJobOut> = {}): FileRestoreJobOut {
  return {
    id: "job-1",
    job_type: "list",
    status: "pending",
    workload_label: "web (100)",
    path: "/etc",
    result: null,
    result_filename: null,
    error_code: null,
    error_message: null,
    expires_at: "2026-01-01T00:10:00Z",
    started_at: null,
    completed_at: null,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

const COMPLETED_ARCHIVE = makeJob({
  id: "zip-1",
  job_type: "archive",
  status: "completed",
  paths: ["/etc/hosts", "/etc/nginx"],
  result_filename: "ct-100-2026-01-01T000000Z.zip",
  result_size_bytes: 2048,
  content_available: true,
  archive_report: {
    file_count: 12,
    directory_count: 3,
    symlink_count: 0,
    total_bytes: 4096,
    skipped_count: 1,
    renamed_count: 1,
    skipped: [{ path: "/etc/nginx/secret.key", reason: "not readable: Permission denied" }],
    renamed: [{ path: "/etc/nginx/a:b", archive_name: "nginx/a_b" }],
  },
});

/** The list job resolves at once; archive jobs follow `archiveStates` poll by poll. */
function wire(archiveStates: FileRestoreJobOut[]) {
  vi.mocked(fileRestoreApi.createJob).mockImplementation(async (_g, _s, jobType, path) =>
    makeJob({ id: `list-${path}`, job_type: jobType, path, status: "completed", result: ENTRIES }),
  );
  vi.mocked(fileRestoreApi.createArchiveJob).mockResolvedValue(
    makeJob({ id: "zip-1", job_type: "archive", status: "pending" }),
  );
  let poll = 0;
  vi.mocked(fileRestoreApi.getJob).mockImplementation(async (jobId) => {
    if (jobId.startsWith("list-")) {
      return makeJob({ id: jobId, status: "completed", result: ENTRIES });
    }
    const state = archiveStates[Math.min(poll, archiveStates.length - 1)];
    poll += 1;
    return state;
  });
}

function render() {
  return renderWithProviders(<FileBrowserPanel groupId="group-1" snapshotId="snapshot-1" resetKey="snapshot-1" />);
}

describe("FileBrowserPanel folder and multi-selection ZIP downloads", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fileRestoreApi.saveJobContent).mockResolvedValue(undefined);
  });

  it("downloads several selected entries (files and a folder) as one ZIP and saves it once ready", async () => {
    wire([COMPLETED_ARCHIVE]);
    render();
    await waitFor(() => expect(screen.getByText("hosts")).toBeInTheDocument());

    const zipButton = screen.getByRole("button", { name: "Download (ZIP)" });
    expect(zipButton).toBeDisabled();

    fireEvent.click(screen.getByRole("checkbox", { name: "Select hosts" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select nginx" }));
    expect(screen.getByText("2 selected")).toBeInTheDocument();
    expect(zipButton).not.toBeDisabled();

    fireEvent.click(zipButton);

    await waitFor(() =>
      expect(fileRestoreApi.createArchiveJob).toHaveBeenCalledWith("group-1", "snapshot-1", ["/etc/nginx", "/etc/hosts"]),
    );
    // selecting a checkbox never triggers the row's own single-file download
    expect(fileRestoreApi.createJob).not.toHaveBeenCalledWith("group-1", "snapshot-1", "download", expect.anything());
    await waitFor(() => expect(fileRestoreApi.saveJobContent).toHaveBeenCalledWith("zip-1"));
    expect(fileRestoreApi.saveJobContent).toHaveBeenCalledTimes(1);

    const list = screen.getByRole("list", { name: "ZIP downloads" });
    expect(within(list).getByText("Ready")).toBeInTheDocument();
    expect(within(list).getByText("ct-100-2026-01-01T000000Z.zip")).toBeInTheDocument();
    expect(within(list).getByText(/ZIP 2\.0 KB — 12 files, 4\.0 KB uncompressed/)).toBeInTheDocument();
    expect(within(list).getByText("1 entry is not in the ZIP")).toBeInTheDocument();
    expect(within(list).getByText("/etc/nginx/secret.key")).toBeInTheDocument();
    expect(within(list).getByText("1 name was changed")).toBeInTheDocument();
    // selection is cleared after starting
    expect(screen.queryByText("2 selected")).not.toBeInTheDocument();

    fireEvent.click(within(list).getByRole("button", { name: "Download again" }));
    expect(fileRestoreApi.saveJobContent).toHaveBeenCalledTimes(2);
  });

  it("downloads a single folder from its row action", async () => {
    wire([{ ...COMPLETED_ARCHIVE, result_filename: "nginx.zip" }]);
    render();
    await waitFor(() => expect(screen.getByText("nginx")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Download folder nginx as ZIP" }));

    await waitFor(() =>
      expect(fileRestoreApi.createArchiveJob).toHaveBeenCalledWith("group-1", "snapshot-1", ["/etc/nginx"]),
    );
    await waitFor(() => expect(screen.getByText("nginx.zip")).toBeInTheDocument());
  });

  it("select all toggles every entry of the directory", async () => {
    wire([COMPLETED_ARCHIVE]);
    render();
    await waitFor(() => expect(screen.getByText("hosts")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("checkbox", { name: "Select all" }));
    expect(screen.getByText("3 selected")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select all" }));
    expect(screen.queryByText("3 selected")).not.toBeInTheDocument();
  });

  it("shows progress while the ZIP is built", async () => {
    wire([
      makeJob({
        id: "zip-1",
        job_type: "archive",
        status: "running",
        progress: { phase: "packing", entries_done: 1, entries_total: 2, bytes_done: 1048576 },
      }),
    ]);
    render();
    await waitFor(() => expect(screen.getByText("nginx")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Download folder nginx as ZIP" }));

    await waitFor(() => expect(screen.getByText("Creating ZIP")).toBeInTheDocument());
    expect(screen.getByText("Writing the ZIP… 1 of 2 entries, 1.0 MB so far")).toBeInTheDocument();
    expect(fileRestoreApi.saveJobContent).not.toHaveBeenCalled();
  });

  it("shows the worker's error for a failed ZIP job", async () => {
    wire([
      makeJob({
        id: "zip-1",
        job_type: "archive",
        status: "failed",
        error_code: "content_too_large",
        error_message: "The content (60.0 GB) exceeds the configured limit of 50.0 GB per download.",
      }),
    ]);
    render();
    await waitFor(() => expect(screen.getByText("nginx")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Download folder nginx as ZIP" }));

    await waitFor(() => expect(screen.getByText("Failed")).toBeInTheDocument());
    expect(
      screen.getByText("The content (60.0 GB) exceeds the configured limit of 50.0 GB per download."),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss nginx" }));
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
  });

  it("explains why a ZIP job could not even be created (e.g. not enough disk space)", async () => {
    wire([]);
    vi.mocked(fileRestoreApi.createArchiveJob).mockRejectedValue(
      new ApiError(507, "insufficient_disk_space", "Not enough free disk space for file-restore downloads."),
    );
    render();
    await waitFor(() => expect(screen.getByText("nginx")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Download folder nginx as ZIP" }));

    await waitFor(() =>
      expect(
        screen.getByText(/The ZIP download could not be started\. Not enough free disk space for file-restore downloads\./),
      ).toBeInTheDocument(),
    );
  });

  it("clears the selection when navigating into another directory", async () => {
    wire([COMPLETED_ARCHIVE]);
    render();
    await waitFor(() => expect(screen.getByText("hosts")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("checkbox", { name: "Select hosts" }));
    expect(screen.getByText("1 selected")).toBeInTheDocument();

    fireEvent.click(screen.getByText("nginx"));
    await waitFor(() =>
      expect(fileRestoreApi.createJob).toHaveBeenCalledWith("group-1", "snapshot-1", "list", "/etc/nginx"),
    );
    await waitFor(() => expect(screen.queryByText("1 selected")).not.toBeInTheDocument());
  });
});
