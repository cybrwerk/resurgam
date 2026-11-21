import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { FileEntryOut, FileRestoreJobOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { FileBrowserPanel } from "./FileBrowserPanel";

vi.mock("../api/resources", () => ({
  fileRestoreApi: {
    createJob: vi.fn(),
    getJob: vi.fn(),
    saveJobContent: vi.fn().mockResolvedValue(undefined),
  },
}));

const { fileRestoreApi } = await import("../api/resources");

const ENTRIES: FileEntryOut[] = [
  { path: "/etc", name: "etc", type: "directory", size: null, mtime: null },
  { path: "/etc/hosts", name: "hosts", type: "file", size: 220, mtime: "2026-01-01T00:00:00Z" },
  { path: "/etc/motd", name: "motd", type: "file", size: 50, mtime: "2026-01-01T00:00:00Z" },
];

function makeJob(overrides: Partial<FileRestoreJobOut> = {}): FileRestoreJobOut {
  return {
    id: "job-1",
    job_type: "list",
    status: "pending",
    workload_label: "Office-PC (116)",
    path: "/",
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

/** Wires createJob/getJob so every created job resolves immediately to a
 * terminal status on its first poll — enough for tests that don't need
 * to observe an in-flight polling state themselves. */
function autoResolveJobs(resultFor: (jobType: "list" | "download", path: string) => Partial<FileRestoreJobOut>) {
  let counter = 0;
  const jobs = new Map<string, FileRestoreJobOut>();
  vi.mocked(fileRestoreApi.createJob).mockImplementation(async (_groupId, _snapshotId, jobType, path) => {
    counter += 1;
    const created = makeJob({ id: `job-${counter}`, job_type: jobType, path, ...resultFor(jobType, path) });
    jobs.set(created.id, created);
    return created;
  });
  vi.mocked(fileRestoreApi.getJob).mockImplementation(async (jobId) => jobs.get(jobId) as FileRestoreJobOut);
  return jobs;
}

function renderModal(overrides: Partial<Parameters<typeof FileBrowserPanel>[0]> = {}) {
  return renderWithProviders(
    <FileBrowserPanel groupId="group-1" snapshotId="snapshot-1" resetKey="snapshot-1" {...overrides} />,
  );
}

describe("FileBrowserPanel", () => {
  it("shows a loading state while the initial job creation is in flight", async () => {
    vi.mocked(fileRestoreApi.createJob).mockReturnValue(new Promise(() => {}));
    renderModal();
    await waitFor(() => expect(screen.getByText("Loading…")).toBeInTheDocument());
  });

  it("shows an empty state for an empty directory", async () => {
    autoResolveJobs(() => ({ status: "completed", result: [] }));
    renderModal();
    await waitFor(() => expect(screen.getByText("This directory is empty.")).toBeInTheDocument());
  });

  it("shows the file-restore-unavailable message distinctly from a generic error", async () => {
    autoResolveJobs(() => ({
      status: "failed",
      error_code: "file_restore_unavailable",
      error_message: "proxmox-file-restore is not installed on the host running the file-restore worker.",
    }));
    renderModal();
    await waitFor(() => expect(screen.getByText("File browsing isn't available here")).toBeInTheDocument());
    // The worker's actionable detail (what is missing, where, how to fix it) is shown, not just a generic text.
    expect(
      screen.getByText("proxmox-file-restore is not installed on the host running the file-restore worker."),
    ).toBeInTheDocument();
  });

  it("shows a retry option on a generic error", async () => {
    autoResolveJobs(() => ({ status: "failed", error_code: "file_restore_failed" }));
    renderModal();
    await waitFor(() => expect(screen.getByText("Retry")).toBeInTheDocument());
  });

  it("renders directories and files, and downloads a file on click", async () => {
    autoResolveJobs((jobType) =>
      jobType === "list" ? { status: "completed", result: ENTRIES } : { status: "completed", result_filename: "hosts" },
    );
    renderModal();

    await waitFor(() => expect(screen.getByText("etc")).toBeInTheDocument());
    expect(screen.getByText("hosts")).toBeInTheDocument();

    screen.getByText("hosts").closest("button")?.click();
    await waitFor(() => expect(fileRestoreApi.saveJobContent).toHaveBeenCalledWith(expect.any(String)));
  });

  it("navigates into a directory on click, creating a new list job for the new path", async () => {
    autoResolveJobs(() => ({ status: "completed", result: ENTRIES }));
    renderModal();

    await waitFor(() => expect(screen.getByText("etc")).toBeInTheDocument());
    screen.getByText("etc").closest("button")?.click();

    await waitFor(() => expect(fileRestoreApi.createJob).toHaveBeenCalledWith("group-1", "snapshot-1", "list", "/etc"));
  });

  it("shows a restoring indicator for the row in flight, and a hint that jobs run in the background", async () => {
    autoResolveJobs(() => ({ status: "completed", result: ENTRIES }));
    renderModal();
    await waitFor(() => expect(screen.getByText("hosts")).toBeInTheDocument());
    expect(
      screen.getByText(
        "Downloads and directory listings run as background jobs and can take a few minutes, especially for CT/host backups or deep VM paths.",
      ),
    ).toBeInTheDocument();

    let resolveGetJob: (job: FileRestoreJobOut) => void = () => {};
    vi.mocked(fileRestoreApi.createJob).mockResolvedValueOnce(
      makeJob({ id: "dl-1", job_type: "download", status: "pending", path: "/etc/hosts" }),
    );
    vi.mocked(fileRestoreApi.getJob).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveGetJob = resolve;
      }),
    );

    screen.getByText("hosts").closest("button")?.click();
    await waitFor(() => expect(screen.getByText("Restoring…")).toBeInTheDocument());
    // the row being restored is disabled, but an unrelated row stays clickable
    expect(screen.getByText("hosts").closest("button")).toBeDisabled();
    expect(screen.getByText("motd").closest("button")).not.toBeDisabled();

    resolveGetJob(makeJob({ id: "dl-1", job_type: "download", status: "completed", result_filename: "hosts" }));
    await waitFor(() => expect(screen.queryByText("Restoring…")).not.toBeInTheDocument());
    expect(fileRestoreApi.saveJobContent).toHaveBeenCalledWith("dl-1");
  });

  it("tracks two concurrent downloads independently, each keeping its own row disabled until it settles", async () => {
    autoResolveJobs(() => ({ status: "completed", result: ENTRIES }));
    renderModal();
    await waitFor(() => expect(screen.getByText("hosts")).toBeInTheDocument());

    const resolvers: Record<string, (job: FileRestoreJobOut) => void> = {};
    let dlCounter = 0;
    vi.mocked(fileRestoreApi.createJob).mockImplementation(async (_groupId, _snapshotId, jobType) => {
      dlCounter += 1;
      return makeJob({ id: `dl-${dlCounter}`, job_type: jobType, status: "pending" });
    });
    vi.mocked(fileRestoreApi.getJob).mockImplementation(
      (jobId) =>
        new Promise((resolve) => {
          resolvers[jobId] = resolve;
        }),
    );

    screen.getByText("hosts").closest("button")?.click();
    await waitFor(() => expect(screen.getByText("hosts").closest("button")).toBeDisabled());
    screen.getByText("motd").closest("button")?.click();
    await waitFor(() => expect(screen.getByText("motd").closest("button")).toBeDisabled());

    resolvers["dl-1"](makeJob({ id: "dl-1", job_type: "download", status: "completed", result_filename: "hosts" }));
    await waitFor(() => expect(screen.getByText("hosts").closest("button")).not.toBeDisabled());
    // motd's own download is still in flight and must not have been cleared by hosts settling
    expect(screen.getByText("motd").closest("button")).toBeDisabled();

    resolvers["dl-2"](makeJob({ id: "dl-2", job_type: "download", status: "completed", result_filename: "motd" }));
    await waitFor(() => expect(screen.getByText("motd").closest("button")).not.toBeDisabled());
  });

  it("shows the failed filename in the error message, distinct per download", async () => {
    autoResolveJobs((jobType) =>
      jobType === "list" ? { status: "completed", result: ENTRIES } : { status: "failed", error_code: "file_restore_failed" },
    );
    renderModal();

    await waitFor(() => expect(screen.getByText("hosts")).toBeInTheDocument());
    screen.getByText("hosts").closest("button")?.click();

    await waitFor(() => expect(screen.getByText('Downloading "hosts" failed. Try again.')).toBeInTheDocument());
  });
});
