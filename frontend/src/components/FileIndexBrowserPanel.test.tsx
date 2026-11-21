import { useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import type { FileIndexEntryOut, FileIndexJobOut, FileRestoreJobOut } from "../api/types";
import { FileIndexBrowserPanel } from "./FileIndexBrowserPanel";

vi.mock("../api/resources", () => ({
  fileIndexApi: { browse: vi.fn(), search: vi.fn(), createJob: vi.fn() },
  fileRestoreApi: {
    createJob: vi.fn(),
    createArchiveJob: vi.fn(),
    getJob: vi.fn(),
    saveJobContent: vi.fn().mockResolvedValue(undefined),
  },
}));

const { fileIndexApi, fileRestoreApi } = await import("../api/resources");

function entry(overrides: Partial<FileIndexEntryOut> = {}): FileIndexEntryOut {
  return {
    id: "entry-1", backup_snapshot_id: "snap-1", backup_group_id: "group-1",
    archive: "root.pxar", path: "/etc", filename: "etc", type: "directory",
    size_bytes: null, mtime: null, ...overrides,
  };
}

type HarnessProps = Omit<Parameters<typeof FileIndexBrowserPanel>[0], "path" | "onPathChange">;

/** `path` is now fully controlled (the Unified File Workspace owns it) --
 * this harness plays the same role the real page does, so existing
 * navigation-driven test expectations (click a directory, see the
 * breadcrumb/query update) keep working unchanged. */
function Harness(props: HarnessProps) {
  const [path, setPath] = useState("/");
  return <FileIndexBrowserPanel {...props} path={path} onPathChange={setPath} />;
}

function renderPanel(props: Partial<HarnessProps> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <Harness customerId="cust-1" groupId="group-1" snapshotId="snap-1" indexingEnabled {...props} />
    </QueryClientProvider>,
  );
}

describe("FileIndexBrowserPanel", () => {
  it("browses the root directory by default", async () => {
    vi.mocked(fileIndexApi.browse).mockResolvedValue([
      entry({ id: "d1", path: "/etc", filename: "etc", type: "directory" }),
      entry({ id: "f1", path: "/etc/hosts", filename: "hosts", type: "file", size_bytes: 123 }),
    ]);
    renderPanel();

    await waitFor(() => expect(screen.getByText("etc")).toBeInTheDocument());
    expect(fileIndexApi.browse).toHaveBeenCalledWith({ backupGroupId: "group-1", backupSnapshotId: "snap-1", path: "/" });
  });

  it("navigates into a directory on click and updates the breadcrumb", async () => {
    vi.mocked(fileIndexApi.browse).mockImplementation(({ path }) =>
      Promise.resolve(
        path === "/"
          ? [entry({ id: "d1", path: "/etc", filename: "etc", type: "directory" })]
          : [entry({ id: "f1", path: "/etc/hosts", filename: "hosts", type: "file", size_bytes: 123 })],
      ),
    );
    const user = userEvent.setup();
    renderPanel();

    await waitFor(() => expect(screen.getByText("etc")).toBeInTheDocument());
    await user.click(screen.getByText("etc"));

    await waitFor(() => expect(fileIndexApi.browse).toHaveBeenCalledWith({ backupGroupId: "group-1", backupSnapshotId: "snap-1", path: "/etc" }));
    await waitFor(() => expect(screen.getByText("hosts")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "etc" })).toBeDisabled();
  });

  it("keeps the current path when the snapshot (point in time) changes", async () => {
    vi.mocked(fileIndexApi.browse).mockResolvedValue([entry({ id: "d1", path: "/etc", filename: "etc", type: "directory" })]);
    const user = userEvent.setup();
    const { rerender } = renderPanel();

    await waitFor(() => expect(screen.getByText("etc")).toBeInTheDocument());
    await user.click(screen.getByText("etc"));
    await waitFor(() => expect(fileIndexApi.browse).toHaveBeenCalledWith({ backupGroupId: "group-1", backupSnapshotId: "snap-1", path: "/etc" }));

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    rerender(
      <QueryClientProvider client={queryClient}>
        <Harness customerId="cust-1" groupId="group-1" snapshotId="snap-2" indexingEnabled />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(fileIndexApi.browse).toHaveBeenCalledWith({ backupGroupId: "group-1", backupSnapshotId: "snap-2", path: "/etc" }));
  });

  it("performs a recursive search scoped to the current path and shows the matched full paths", async () => {
    vi.mocked(fileIndexApi.browse).mockResolvedValue([entry({ id: "d1", path: "/etc", filename: "etc", type: "directory" })]);
    vi.mocked(fileIndexApi.search).mockResolvedValue({
      items: [entry({ id: "f1", path: "/etc/nested/deep.conf", filename: "deep.conf", type: "file", size_bytes: 42 })],
      has_more: false,
    });
    const user = userEvent.setup();
    renderPanel();

    await waitFor(() => expect(screen.getByText("etc")).toBeInTheDocument());
    await user.type(screen.getByLabelText("Search from here"), "deep{Enter}");

    await waitFor(() =>
      expect(fileIndexApi.search).toHaveBeenCalledWith({
        customerId: "cust-1", backupGroupId: "group-1", backupSnapshotId: "snap-1",
        path: undefined, filename: "deep", limit: 200,
      }),
    );
    await waitFor(() => expect(screen.getByText("/etc/nested/deep.conf")).toBeInTheDocument());
  });

  it("downloads a file via the file-restore job queue when a file row is clicked", async () => {
    vi.mocked(fileIndexApi.browse).mockResolvedValue([
      entry({ id: "f1", path: "/etc/hosts", filename: "hosts", type: "file", size_bytes: 123 }),
    ]);
    const downloadJob: FileRestoreJobOut = {
      id: "restore-job-1", job_type: "download", status: "completed", workload_label: "Office-PC (116)", path: "/etc/hosts",
      result: null, result_filename: "hosts", error_code: null, error_message: null,
      expires_at: "2026-01-01T00:10:00Z", started_at: null, completed_at: null, created_at: "2026-01-01T00:00:00Z",
    };
    vi.mocked(fileRestoreApi.createJob).mockResolvedValue(downloadJob);
    vi.mocked(fileRestoreApi.getJob).mockResolvedValue(downloadJob);
    const user = userEvent.setup();
    renderPanel();

    await waitFor(() => expect(screen.getByText("hosts")).toBeInTheDocument());
    await user.click(screen.getByText("hosts"));

    await waitFor(() => expect(fileRestoreApi.createJob).toHaveBeenCalledWith("group-1", "snap-1", "download", "/etc/hosts"));
    await waitFor(() => expect(fileRestoreApi.saveJobContent).toHaveBeenCalledWith("restore-job-1"));
  });

  it("shows a not-indexed state with an index CTA when the root listing is empty", async () => {
    vi.mocked(fileIndexApi.browse).mockResolvedValue([]);
    vi.mocked(fileIndexApi.createJob).mockResolvedValue({} as FileIndexJobOut);
    const user = userEvent.setup();
    renderPanel();

    await waitFor(() => expect(screen.getByText("This snapshot hasn't been indexed yet.")).toBeInTheDocument());
    await user.click(screen.getByText("Index this snapshot"));
    await waitFor(() => expect(fileIndexApi.createJob).toHaveBeenCalledWith("group-1", "snap-1"));
  });

  it("does not show the index CTA when indexing is not enabled for the customer", async () => {
    vi.mocked(fileIndexApi.browse).mockResolvedValue([]);
    renderPanel({ indexingEnabled: false });

    await waitFor(() => expect(screen.getByText("This snapshot hasn't been indexed yet.")).toBeInTheDocument());
    expect(screen.queryByText("Index this snapshot")).not.toBeInTheDocument();
  });

  it("downloads a selection of indexed entries (here: search hits from different folders) as one ZIP", async () => {
    const user = userEvent.setup();
    vi.mocked(fileIndexApi.search).mockResolvedValue({
      items: [
        entry({ id: "a", path: "/etc/nginx/nginx.conf", filename: "nginx.conf", type: "file", size_bytes: 10 }),
        entry({ id: "b", path: "/srv/www/nginx.conf", filename: "nginx.conf", type: "file", size_bytes: 12 }),
      ],
      has_more: false,
    } as never);
    vi.mocked(fileRestoreApi.createArchiveJob).mockResolvedValue({
      id: "zip-1",
      job_type: "archive",
      status: "pending",
    } as FileRestoreJobOut);
    vi.mocked(fileRestoreApi.getJob).mockResolvedValue({
      id: "zip-1",
      job_type: "archive",
      status: "completed",
      result_filename: "ct-100-x.zip",
      result_size_bytes: 100,
      content_available: true,
      archive_report: null,
    } as unknown as FileRestoreJobOut);
    vi.mocked(fileIndexApi.browse).mockResolvedValue([]);
    renderPanel();

    await user.type(screen.getByLabelText("Search from here"), "nginx.conf{Enter}");
    await waitFor(() => expect(screen.getAllByText("nginx.conf")).toHaveLength(2));
    await user.click(screen.getByRole("checkbox", { name: "Select all" }));
    await user.click(screen.getByRole("button", { name: "Download (ZIP)" }));

    await waitFor(() =>
      expect(fileRestoreApi.createArchiveJob).toHaveBeenCalledWith("group-1", "snap-1", [
        "/etc/nginx/nginx.conf",
        "/srv/www/nginx.conf",
      ]),
    );
    await waitFor(() => expect(fileRestoreApi.saveJobContent).toHaveBeenCalledWith("zip-1"));
    expect(screen.getByText("ct-100-x.zip")).toBeInTheDocument();
  });
});
