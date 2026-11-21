import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import type { FileEntryOut, FileIndexEntryOut, FileRestoreJobOut } from "../api/types";
import { DiskTree } from "./DiskTree";

vi.mock("../api/resources", () => ({
  fileIndexApi: { browse: vi.fn() },
  fileRestoreApi: { createJob: vi.fn(), getJob: vi.fn() },
}));

const { fileIndexApi, fileRestoreApi } = await import("../api/resources");

function indexEntry(overrides: Partial<FileIndexEntryOut>): FileIndexEntryOut {
  return {
    id: "e1", backup_snapshot_id: "snap-1", backup_group_id: "group-1", archive: "root.pxar",
    path: "/etc", filename: "etc", type: "directory", size_bytes: null, mtime: null, ...overrides,
  };
}

function liveEntry(overrides: Partial<FileEntryOut>): FileEntryOut {
  return { path: "/etc", name: "etc", type: "directory", size: null, mtime: null, ...overrides };
}

function renderTree(props: Partial<Parameters<typeof DiskTree>[0]> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <DiskTree
        groupId="group-1"
        snapshotId="snap-1"
        backupType="vm"
        isIndexed
        selectedPath="/"
        onSelectPath={vi.fn()}
        {...props}
      />
    </QueryClientProvider>,
  );
}

describe("DiskTree", () => {
  it("green mode: labels a VM's root disk archives with their friendly disk name", async () => {
    vi.mocked(fileIndexApi.browse).mockResolvedValue([
      indexEntry({ id: "d1", path: "/drive-scsi0.img.fidx", filename: "drive-scsi0.img.fidx", type: "directory" }),
    ]);
    renderTree();

    await waitFor(() => expect(screen.getByText("scsi0")).toBeInTheDocument());
    expect(fileIndexApi.browse).toHaveBeenCalledWith({ backupGroupId: "group-1", backupSnapshotId: "snap-1", path: "/" });
  });

  it("green mode: lazily expands a node, fetching only that node's own path", async () => {
    vi.mocked(fileIndexApi.browse).mockImplementation(({ path }) =>
      Promise.resolve(
        path === "/"
          ? [indexEntry({ id: "d1", path: "/drive-scsi0.img.fidx", filename: "drive-scsi0.img.fidx", type: "directory" })]
          : [indexEntry({ id: "d2", path: "/drive-scsi0.img.fidx/2", filename: "2", type: "directory" })],
      ),
    );
    const user = userEvent.setup();
    renderTree();

    await waitFor(() => expect(screen.getByText("scsi0")).toBeInTheDocument());
    expect(fileIndexApi.browse).not.toHaveBeenCalledWith({
      backupGroupId: "group-1", backupSnapshotId: "snap-1", path: "/drive-scsi0.img.fidx",
    });

    await user.click(screen.getByText("scsi0"));

    await waitFor(() =>
      expect(fileIndexApi.browse).toHaveBeenCalledWith({
        backupGroupId: "group-1", backupSnapshotId: "snap-1", path: "/drive-scsi0.img.fidx",
      }),
    );
    // Partition "2" renders one level deeper -- no schema tag distinguishes
    // it from a plain folder, but it must still show up as a child node.
    await waitFor(() => expect(screen.getByText("2")).toBeInTheDocument());
  });

  it("green mode: filters out file entries, only directories become tree nodes", async () => {
    vi.mocked(fileIndexApi.browse).mockResolvedValue([
      indexEntry({ id: "d1", path: "/etc", filename: "etc", type: "directory" }),
      indexEntry({ id: "f1", path: "/readme.txt", filename: "readme.txt", type: "file" }),
    ]);
    renderTree({ backupType: "ct" });

    await waitFor(() => expect(screen.getByText("etc")).toBeInTheDocument());
    expect(screen.queryByText("readme.txt")).not.toBeInTheDocument();
  });

  it("CT/host: shows the root heading and real filesystem root directly, no disk framing", async () => {
    vi.mocked(fileIndexApi.browse).mockResolvedValue([indexEntry({ id: "d1", path: "/etc", filename: "etc", type: "directory" })]);
    renderTree({ backupType: "ct" });

    expect(screen.getByText("Root filesystem")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("etc")).toBeInTheDocument());
  });

  it("yellow mode: creates a live list job and reads its result directly when already completed", async () => {
    // createJob itself returns a terminal "completed" job here -- the
    // separate in-progress-poll timing is already covered by
    // FileBrowserPanel's own tests; this test's job is only to confirm
    // yellow mode goes through the live job queue, not the index browse.
    const completedJob: FileRestoreJobOut = {
      id: "job-1", job_type: "list", status: "completed", workload_label: "web-01", path: "/",
      result: [liveEntry({ path: "/drive-scsi0.img.fidx", name: "drive-scsi0.img.fidx" })],
      result_filename: null, error_code: null, error_message: null,
      expires_at: "2026-01-01T00:10:00Z", started_at: null, completed_at: null, created_at: "2026-01-01T00:00:00Z",
    };
    vi.mocked(fileRestoreApi.createJob).mockResolvedValue(completedJob);
    renderTree({ isIndexed: false });

    await waitFor(() => expect(screen.getByText("scsi0")).toBeInTheDocument());
    expect(fileIndexApi.browse).not.toHaveBeenCalled();
    expect(fileRestoreApi.createJob).toHaveBeenCalledWith("group-1", "snap-1", "list", "/");
    expect(fileRestoreApi.getJob).not.toHaveBeenCalled();
  });

  it("calls onSelectPath when a node is clicked, even before its children have loaded", async () => {
    vi.mocked(fileIndexApi.browse).mockResolvedValue([
      indexEntry({ id: "d1", path: "/etc", filename: "etc", type: "directory" }),
    ]);
    const onSelectPath = vi.fn();
    const user = userEvent.setup();
    renderTree({ backupType: "ct", onSelectPath });

    await waitFor(() => expect(screen.getByText("etc")).toBeInTheDocument());
    await user.click(screen.getByText("etc"));

    expect(onSelectPath).toHaveBeenCalledWith("/etc");
  });
});
