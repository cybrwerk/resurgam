import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { BackupGroupOut, BackupSnapshotOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { PruneSimulationButton } from "./PruneSimulationButton";

vi.mock("../api/resources", () => ({
  datastoresApi: { backupGroups: vi.fn() },
  inventoryApi: { snapshots: vi.fn() },
}));

const { datastoresApi, inventoryApi } = await import("../api/resources");

const GROUP: BackupGroupOut = {
  id: "group-1",
  datastore_id: "ds-1",
  namespace: "",
  backup_type: "vm",
  backup_id: "116",
  resolved_name: "web-01",
  owner: null,
  last_backup_at: null,
  last_verify_status: "ok",
  protection_policy_id: null,
  protection_status: "unknown",
  customer_id: null,
  effective_customer_id: "cust-1",
  file_index_enabled: false,
  file_index_exclude_patterns: [],
  file_index_method: "external_mount",
  guest_exec_live_pve_cluster_id: null,
  superseded_at: null,
  removed_from_pbs_at: null,
  last_synced_at: "2026-01-01T00:00:00Z",
};

function snapshot(overrides: Partial<BackupSnapshotOut>): BackupSnapshotOut {
  return {
    id: "snap",
    backup_group_id: "group-1",
    snapshot_time: "2026-01-01T00:00:00",
    verify_status: "ok",
    size_bytes: 1024,
    last_synced_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function renderButton(overrides: Partial<Parameters<typeof PruneSimulationButton>[0]> = {}) {
  return renderWithProviders(
    <PruneSimulationButton
      datastoreId="ds-1"
      namespace={null}
      keepLast=""
      keepHourly=""
      keepDaily="2"
      keepWeekly=""
      keepMonthly=""
      keepYearly=""
      {...overrides}
    />,
  );
}

describe("PruneSimulationButton", () => {
  it("disables the button when no datastore has been resolved yet", () => {
    renderButton({ datastoreId: null });
    expect(screen.getByRole("button", { name: "Simulate" })).toBeDisabled();
  });

  it("opens a modal, lets the user pick a workload, and shows the simulated timeline", async () => {
    vi.mocked(datastoresApi.backupGroups).mockResolvedValue([GROUP]);
    vi.mocked(inventoryApi.snapshots).mockResolvedValue([
      snapshot({ id: "s1", snapshot_time: "2026-01-01T00:00:00" }),
      snapshot({ id: "s2", snapshot_time: "2026-01-02T00:00:00" }),
      snapshot({ id: "s3", snapshot_time: "2026-01-03T00:00:00" }),
    ]);
    const user = userEvent.setup();
    renderButton();

    await user.click(screen.getByRole("button", { name: "Simulate" }));
    const dialog = await screen.findByRole("dialog", { name: "Prune simulation" });

    await waitFor(() => expect(within(dialog).getByRole("option", { name: /web-01/ })).toBeInTheDocument());
    await user.selectOptions(within(dialog).getByLabelText("Preview against"), "group-1");

    await waitFor(() => expect(inventoryApi.snapshots).toHaveBeenCalledWith("group-1"));
    // keepDaily=2 -> the two most recent days survive, the oldest is pruned.
    await waitFor(() => expect(within(dialog).getByText("2 kept")).toBeInTheDocument());
    expect(within(dialog).getByText("1 would be pruned")).toBeInTheDocument();
  });

  it("warns when no keep option would retain anything", async () => {
    vi.mocked(datastoresApi.backupGroups).mockResolvedValue([GROUP]);
    vi.mocked(inventoryApi.snapshots).mockResolvedValue([snapshot({ id: "s1" })]);
    const user = userEvent.setup();
    renderButton({ keepDaily: "" });

    await user.click(screen.getByRole("button", { name: "Simulate" }));
    const dialog = await screen.findByRole("dialog", { name: "Prune simulation" });
    await waitFor(() => expect(within(dialog).getByRole("option", { name: /web-01/ })).toBeInTheDocument());
    await user.selectOptions(within(dialog).getByLabelText("Preview against"), "group-1");

    await waitFor(() =>
      expect(within(dialog).getByText("No keep option would retain anything -- every snapshot would be pruned.")).toBeInTheDocument(),
    );
  });
});
