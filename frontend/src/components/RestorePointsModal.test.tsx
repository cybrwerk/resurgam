import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { BackupSnapshotOut, SnapshotIndexStatusOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { RestorePointsModal } from "./RestorePointsModal";

vi.mock("../api/resources", () => ({
  inventoryApi: { snapshots: vi.fn() },
  fileIndexApi: { snapshotStatus: vi.fn(), browse: vi.fn(), search: vi.fn() },
  fileIndexSettingsApi: { get: vi.fn() },
  fileRestoreApi: { createJob: vi.fn(), getJob: vi.fn(), saveJobContent: vi.fn().mockResolvedValue(undefined) },
}));

const { inventoryApi, fileIndexApi, fileIndexSettingsApi, fileRestoreApi } = await import("../api/resources");

const SNAPSHOT: BackupSnapshotOut = {
  id: "snap-1",
  backup_group_id: "group-1",
  snapshot_time: "2026-01-02T00:00:00Z",
  verify_status: "ok",
  size_bytes: 2048,
  last_synced_at: "2026-01-02T00:00:00Z",
};

function setup(status: SnapshotIndexStatusOut = { indexed_snapshot_ids: [] }) {
  vi.mocked(inventoryApi.snapshots).mockResolvedValue([SNAPSHOT]);
  vi.mocked(fileIndexApi.snapshotStatus).mockResolvedValue(status);
  vi.mocked(fileIndexSettingsApi.get).mockRejectedValue(new ApiError(404, "not_found", "Not found"));
}

function renderModal() {
  return renderWithProviders(
    <RestorePointsModal
      open
      onClose={() => {}}
      groupId="group-1"
      customerId="cust-1"
      backupType="vm"
      groupFileIndexEnabled={false}
      workloadLabel="VM 100 — tenant-a"
    />,
  );
}

describe("RestorePointsModal", () => {
  it("shows the workload in the title and lists its restore points", async () => {
    setup();
    renderModal();

    expect(screen.getByText("Restore points — VM 100 — tenant-a")).toBeInTheDocument();
    await waitFor(() => expect(inventoryApi.snapshots).toHaveBeenCalledWith("group-1"));
    await waitFor(() => expect(screen.getByText("2.0 KB")).toBeInTheDocument());
  });

  it("shows an empty state when there are no restore points", async () => {
    setup();
    vi.mocked(inventoryApi.snapshots).mockResolvedValue([]);
    renderModal();
    await waitFor(() => expect(screen.getByText("No restore points found for this workload.")).toBeInTheDocument());
  });

  it("opens the unified File Browser window for a green (indexed) restore point", async () => {
    // Raised directly by the user: this modal used to open its own,
    // older always-live file browser instead of the same File Browser
    // window (tree + search + catalog browsing) the Files workspace uses.
    setup({ indexed_snapshot_ids: ["snap-1"] });
    vi.mocked(fileIndexApi.browse).mockResolvedValue([]);
    renderModal();

    await waitFor(() => expect(screen.getByText("2.0 KB")).toBeInTheDocument());
    screen.getByText("2.0 KB").closest("button")?.click();

    const dialog = await screen.findByRole("dialog", { name: "VM 100 — tenant-a" });
    await waitFor(() =>
      expect(fileIndexApi.browse).toHaveBeenCalledWith({ backupGroupId: "group-1", backupSnapshotId: "snap-1", path: "/" }),
    );

    within(dialog).getByLabelText("Close").click();
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "VM 100 — tenant-a" })).not.toBeInTheDocument());
    // The restore-points list underneath stays open.
    expect(screen.getByText("Restore points — VM 100 — tenant-a")).toBeInTheDocument();
  });

  it("gates a yellow (not-yet-indexed) restore point behind the Run confirmation", async () => {
    setup({ indexed_snapshot_ids: [] });
    renderModal();

    await waitFor(() => expect(screen.getByText("2.0 KB")).toBeInTheDocument());
    screen.getByText("2.0 KB").closest("button")?.click();

    await screen.findByRole("dialog", { name: "VM 100 — tenant-a" });
    await waitFor(() => expect(screen.getByText("This snapshot hasn't been indexed yet.")).toBeInTheDocument());
    expect(fileRestoreApi.createJob).not.toHaveBeenCalled();
  });
});
