import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { FileRestoreJobOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { FileBrowserModal } from "./FileBrowserModal";

vi.mock("../api/resources", () => ({
  fileRestoreApi: {
    createJob: vi.fn(),
    getJob: vi.fn(),
    saveJobContent: vi.fn().mockResolvedValue(undefined),
  },
}));

const { fileRestoreApi } = await import("../api/resources");

function job(overrides: Partial<FileRestoreJobOut> = {}): FileRestoreJobOut {
  return {
    id: "job-1",
    job_type: "list",
    status: "completed",
    workload_label: "Office-PC (116)",
    path: "/",
    result: [],
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

describe("FileBrowserModal", () => {
  it("renders the title and browses when open", async () => {
    vi.mocked(fileRestoreApi.createJob).mockResolvedValue(job());
    vi.mocked(fileRestoreApi.getJob).mockResolvedValue(job());
    renderWithProviders(
      <FileBrowserModal open onClose={() => {}} groupId="group-1" snapshotId="snapshot-1" title="Files in test snapshot" />,
    );
    expect(screen.getByText("Files in test snapshot")).toBeInTheDocument();
    await waitFor(() => expect(fileRestoreApi.createJob).toHaveBeenCalledWith("group-1", "snapshot-1", "list", "/"));
  });

  it("doesn't fetch anything when closed", () => {
    renderWithProviders(
      <FileBrowserModal open={false} onClose={() => {}} groupId="group-1" snapshotId="snapshot-1" title="Files" />,
    );
    expect(fileRestoreApi.createJob).not.toHaveBeenCalled();
  });
});
