import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import type { BackupGroupOut, BackupSnapshotOut, CustomerOut, DatastoreOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { SnapshotPicker } from "./SnapshotPicker";

vi.mock("../api/resources", () => ({
  customersApi: { list: vi.fn() },
  datastoresApi: { list: vi.fn(), backupGroups: vi.fn() },
  inventoryApi: { snapshots: vi.fn() },
}));

const { customersApi, datastoresApi, inventoryApi } = await import("../api/resources");

const CUSTOMER: CustomerOut = {
  id: "cust-1",
  name: "Acme AG",
  slug: "acme",
  debitor_number: "1000",
  match_code: "ACM",
  disabled: false,
  created_at: "2026-01-01T00:00:00Z",
  report_retention_days: null,
};

const DATASTORE: DatastoreOut = {
  id: "ds-1",
  pbs_instance_id: "pbs-1",
  name: "main-store",
  path: null,
  comment: null,
  created_at: "2026-01-01T00:00:00Z",
  customer_id: "cust-1",
  role: "primary",
};

function group(overrides: Partial<BackupGroupOut>): BackupGroupOut {
  return {
    id: "group-1",
    datastore_id: "ds-1",
    namespace: "",
    backup_type: "vm",
    backup_id: "100",
    resolved_name: null,
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
    ...overrides,
  };
}

const VM_GROUP = group({ id: "group-vm", backup_type: "vm", backup_id: "100" });
const HOST_GROUP = group({ id: "group-host", backup_type: "host", backup_id: "host1" });

const SNAPSHOT: BackupSnapshotOut = {
  id: "snap-1",
  backup_group_id: "group-vm",
  snapshot_time: "2026-01-02T00:00:00Z",
  verify_status: "ok",
  size_bytes: 1024,
  last_synced_at: "2026-01-02T00:00:00Z",
};

function setup() {
  vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
  vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE]);
  vi.mocked(datastoresApi.backupGroups).mockResolvedValue([VM_GROUP, HOST_GROUP]);
  vi.mocked(inventoryApi.snapshots).mockResolvedValue([SNAPSHOT]);
}

function Harness({ groupFilter }: { groupFilter?: (g: BackupGroupOut) => boolean }) {
  const [customerId, setCustomerId] = useState("");
  const [groupId, setGroupId] = useState("");
  const [snapshotId, setSnapshotId] = useState("");
  return (
    <SnapshotPicker
      customerId={customerId}
      onCustomerChange={setCustomerId}
      groupId={groupId}
      onGroupChange={setGroupId}
      snapshotId={snapshotId}
      onSnapshotChange={setSnapshotId}
      groupFilter={groupFilter}
    />
  );
}

async function pickCustomerAndGroup(user: ReturnType<typeof userEvent.setup>) {
  await waitFor(() => expect(screen.getByRole("option", { name: "Acme AG" })).toBeInTheDocument());
  await user.selectOptions(screen.getByLabelText("Tenant"), "cust-1");
  await user.click(screen.getByLabelText("Backup group"));
  await waitFor(() => expect(screen.getByRole("option", { name: /VM 100/ })).toBeInTheDocument());
  await user.click(screen.getByRole("option", { name: /VM 100/ }));
}

describe("SnapshotPicker", () => {
  it("lists backups without any tenant under 'No tenant'", async () => {
    vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
    vi.mocked(datastoresApi.list).mockResolvedValue([DATASTORE]);
    vi.mocked(datastoresApi.backupGroups).mockResolvedValue([
      VM_GROUP,
      group({ id: "group-orphan", backup_id: "901", resolved_name: "router", effective_customer_id: null }),
    ]);
    vi.mocked(inventoryApi.snapshots).mockResolvedValue([SNAPSHOT]);
    const user = userEvent.setup();
    renderWithProviders(<Harness />);

    await waitFor(() => expect(screen.getByRole("option", { name: "Acme AG" })).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Tenant"), "__unassigned__");
    await user.click(screen.getByLabelText("Backup group"));

    await waitFor(() => expect(screen.getByRole("option", { name: /VM router \(901\)/ })).toBeInTheDocument());
    expect(screen.queryByRole("option", { name: /VM 100/ })).not.toBeInTheDocument();
  });

  it("cascades customer -> group -> a restore-point list defaulting to List view", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<Harness />);

    await pickCustomerAndGroup(user);

    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());
    expect(screen.getByText(new Date(SNAPSHOT.snapshot_time).toLocaleString())).toBeInTheDocument();
  });

  it("selects a restore point by clicking its row in List view", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<Harness />);

    await pickCustomerAndGroup(user);
    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());
    await user.click(screen.getByText(new Date(SNAPSHOT.snapshot_time).toLocaleString()));

    // The selected row gets the picker's "selected" styling.
    const row = screen.getByText(new Date(SNAPSHOT.snapshot_time).toLocaleString()).closest("tr");
    expect(row?.className).toMatch(/selectedRow/);
  });

  it("switches to Calendar view and selects a restore point from the month grid", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<Harness />);

    await pickCustomerAndGroup(user);
    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Calendar" }));
    await waitFor(() => expect(screen.queryByRole("table")).not.toBeInTheDocument());

    const snapshotDay = new Date(SNAPSHOT.snapshot_time).getDate();
    const dayButtons = screen.getAllByText(String(snapshotDay), { selector: "span" });
    // The only clickable (has-snapshots) day cell renders as a <button>.
    const dayCell = dayButtons.map((el) => el.closest("button")).find((btn) => btn !== null);
    expect(dayCell).toBeTruthy();
    await user.click(dayCell!);
  });

  it("applies groupFilter to exclude non-matching backup types", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<Harness groupFilter={(g) => g.backup_type !== "host"} />);

    await waitFor(() => expect(screen.getByRole("option", { name: "Acme AG" })).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Tenant"), "cust-1");

    await user.click(screen.getByLabelText("Backup group"));
    await waitFor(() => expect(screen.getByRole("option", { name: /VM 100/ })).toBeInTheDocument());
    expect(screen.queryByRole("option", { name: /HOST/ })).not.toBeInTheDocument();
  });

  it("shows an empty state when the group has no restore points", async () => {
    setup();
    vi.mocked(inventoryApi.snapshots).mockResolvedValue([]);
    const user = userEvent.setup();
    renderWithProviders(<Harness />);

    await pickCustomerAndGroup(user);
    await waitFor(() => expect(screen.getByText("This backup group has no restore points.")).toBeInTheDocument());
  });
});
