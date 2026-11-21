import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import type { FileRestoreJobOut } from "../api/types";
import { useSnapshotFileDownloads } from "./useSnapshotFileDownloads";

vi.mock("../api/resources", () => ({
  fileRestoreApi: { createJob: vi.fn(), getJob: vi.fn(), saveJobContent: vi.fn() },
}));

const { fileRestoreApi } = await import("../api/resources");

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

const COMPLETED_JOB: FileRestoreJobOut = {
  id: "job-1",
  job_type: "download",
  status: "completed",
  workload_label: "Office-PC (116)",
  path: "/etc/hosts",
  result: null,
  result_filename: "hosts",
  error_code: null,
  error_message: null,
  expires_at: "2026-01-01T00:10:00Z",
  started_at: "2026-01-01T00:00:00Z",
  completed_at: "2026-01-01T00:00:05Z",
  created_at: "2026-01-01T00:00:00Z",
};

describe("useSnapshotFileDownloads", () => {
  it("surfaces an error when the completed job's save fetch fails, instead of failing silently", async () => {
    vi.mocked(fileRestoreApi.createJob).mockResolvedValue({ ...COMPLETED_JOB, status: "pending" });
    vi.mocked(fileRestoreApi.getJob).mockResolvedValue(COMPLETED_JOB);
    vi.mocked(fileRestoreApi.saveJobContent).mockRejectedValue(new Error("410 content_expired"));

    const { result } = renderHook(() => useSnapshotFileDownloads("group-1", "snap-1"), { wrapper });

    await act(async () => {
      result.current.startDownload({ path: "/etc/hosts", name: "hosts" });
    });

    await waitFor(() => expect(fileRestoreApi.saveJobContent).toHaveBeenCalledWith("job-1"));
    await waitFor(() => expect(result.current.downloadError).toEqual({ name: "hosts" }));
  });
});
