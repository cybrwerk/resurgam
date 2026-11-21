import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { BackupGroupOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { FileIndexExcludePatternsModal } from "./FileIndexExcludePatternsModal";

vi.mock("../api/resources", () => ({
  inventoryApi: {
    setFileIndexExcludePatterns: vi.fn(),
    defaultFileIndexExcludePatterns: vi.fn(),
    setFileIndexMethod: vi.fn(),
  },
  pveClustersApi: { list: vi.fn() },
}));

const { inventoryApi, pveClustersApi } = await import("../api/resources");
vi.mocked(pveClustersApi.list).mockResolvedValue([]);

const DEFAULT_PATTERNS = ["(^|/)Windows/WinSxS(/|$)", "(^|/)var/lib/docker(/|$)"];

function group(overrides: Partial<BackupGroupOut>): BackupGroupOut {
  return {
    id: "group-1",
    datastore_id: "ds-1",
    namespace: "",
    backup_type: "vm",
    backup_id: "100",
    resolved_name: null,
    owner: null,
    last_backup_at: "2026-01-05T00:00:00Z",
    last_verify_status: "ok",
    protection_policy_id: null,
    protection_status: "ok",
    customer_id: null,
    effective_customer_id: "cust-a",
    file_index_enabled: false,
    file_index_exclude_patterns: [],
    file_index_method: "external_mount",
    guest_exec_live_pve_cluster_id: null,
    superseded_at: null,
    removed_from_pbs_at: null,
    last_synced_at: "2026-01-05T00:00:00Z",
    ...overrides,
  };
}

function setup() {
  vi.mocked(inventoryApi.defaultFileIndexExcludePatterns).mockResolvedValue({ patterns: DEFAULT_PATTERNS });
}

describe("FileIndexExcludePatternsModal", () => {
  it("a new, never-configured group already shows the server-applied default patterns", () => {
    // BackupGroup.file_index_exclude_patterns defaults server-side now --
    // the textarea reflects whatever the group already has, no click
    // needed, matching "set by default, changing is the intentional step".
    setup();
    renderWithProviders(
      <FileIndexExcludePatternsModal group={group({ file_index_exclude_patterns: DEFAULT_PATTERNS })} onClose={vi.fn()} onSaved={vi.fn()} />,
    );

    const textarea = screen.getByLabelText("Exclusion patterns") as HTMLTextAreaElement;
    expect(textarea.value.split("\n")).toEqual(DEFAULT_PATTERNS);
  });

  it("resets an empty list back to the recommended defaults, fetched from the backend", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(
      <FileIndexExcludePatternsModal group={group({})} onClose={vi.fn()} onSaved={vi.fn()} />,
    );

    await waitFor(() => expect(inventoryApi.defaultFileIndexExcludePatterns).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: "Insert recommended defaults" }));

    const textarea = screen.getByLabelText("Exclusion patterns") as HTMLTextAreaElement;
    expect(textarea.value.split("\n")).toEqual(DEFAULT_PATTERNS);
  });

  it("appends only the defaults not already present, without duplicating an existing one", async () => {
    setup();
    const user = userEvent.setup();
    const existing = DEFAULT_PATTERNS[0];
    renderWithProviders(
      <FileIndexExcludePatternsModal
        group={group({ file_index_exclude_patterns: [existing, "^/my-custom-exclude"] })}
        onClose={vi.fn()}
        onSaved={vi.fn()}
      />,
    );

    await waitFor(() => expect(inventoryApi.defaultFileIndexExcludePatterns).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: "Insert recommended defaults" }));

    const textarea = screen.getByLabelText("Exclusion patterns") as HTMLTextAreaElement;
    const lines = textarea.value.split("\n");
    expect(lines.filter((l) => l === existing)).toHaveLength(1);
    expect(lines).toContain("^/my-custom-exclude");
    expect(lines).toHaveLength(DEFAULT_PATTERNS.length + 1);
  });

  it("saves the reset-to-defaults list through the existing save flow", async () => {
    setup();
    vi.mocked(inventoryApi.setFileIndexExcludePatterns).mockResolvedValue(
      group({ file_index_exclude_patterns: DEFAULT_PATTERNS }),
    );
    const onSaved = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(<FileIndexExcludePatternsModal group={group({})} onClose={vi.fn()} onSaved={onSaved} />);

    await waitFor(() => expect(inventoryApi.defaultFileIndexExcludePatterns).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: "Insert recommended defaults" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(inventoryApi.setFileIndexExcludePatterns).toHaveBeenCalledWith("group-1", DEFAULT_PATTERNS),
    );
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
  });

  // --- Guest-exec File Indexing method picker (ADR 0021) ---------------------

  it("shows no method picker at all for a CT/host group", () => {
    setup();
    renderWithProviders(
      <FileIndexExcludePatternsModal group={group({ backup_type: "ct" })} onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    expect(screen.queryByLabelText("Indexing method")).not.toBeInTheDocument();
  });

  it("defaults the method picker to the group's own current method", () => {
    setup();
    renderWithProviders(
      <FileIndexExcludePatternsModal group={group({ file_index_method: "guest_exec_ephemeral" })} onClose={vi.fn()} onSaved={vi.fn()} />,
    );
    expect(screen.getByLabelText("Indexing method")).toHaveValue("guest_exec_ephemeral");
  });

  it("shows the cluster picker only once guest_exec_live is selected, and requires a choice", async () => {
    setup();
    vi.mocked(pveClustersApi.list).mockResolvedValue([
      { id: "pve-1", display_name: "Prod PVE", host: "pve.example", port: 8006, tls_trust_mode: "ca", tls_fingerprint_sha256: null, version: null, last_health_check_at: null, last_health_status: "unknown", disabled: false, require_restore_approval: false, is_lab_compute: false } as never,
    ]);
    const user = userEvent.setup();
    renderWithProviders(<FileIndexExcludePatternsModal group={group({})} onClose={vi.fn()} onSaved={vi.fn()} />);

    expect(screen.queryByLabelText("Guest-exec lab cluster")).not.toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Indexing method"), "guest_exec_live");

    await waitFor(() => expect(screen.getByRole("option", { name: "Prod PVE" })).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Save method" })).toBeDisabled();

    await user.selectOptions(screen.getByLabelText("Guest-exec lab cluster"), "pve-1");
    expect(screen.getByRole("button", { name: "Save method" })).not.toBeDisabled();
  });

  it("saves the selected method with the cluster id for guest_exec_live", async () => {
    setup();
    vi.mocked(pveClustersApi.list).mockResolvedValue([
      { id: "pve-1", display_name: "Prod PVE", host: "pve.example", port: 8006, tls_trust_mode: "ca", tls_fingerprint_sha256: null, version: null, last_health_check_at: null, last_health_status: "unknown", disabled: false, require_restore_approval: false, is_lab_compute: false } as never,
    ]);
    vi.mocked(inventoryApi.setFileIndexMethod).mockResolvedValue(group({ file_index_method: "guest_exec_live" }));
    const onSaved = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(<FileIndexExcludePatternsModal group={group({})} onClose={vi.fn()} onSaved={onSaved} />);

    await user.selectOptions(screen.getByLabelText("Indexing method"), "guest_exec_live");
    await waitFor(() => expect(screen.getByRole("option", { name: "Prod PVE" })).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Guest-exec lab cluster"), "pve-1");
    await user.click(screen.getByRole("button", { name: "Save method" }));

    await waitFor(() => expect(inventoryApi.setFileIndexMethod).toHaveBeenCalledWith("group-1", "guest_exec_live", "pve-1"));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
  });

  it("saves external_mount with a null cluster id (no cluster picker involved)", async () => {
    setup();
    vi.mocked(inventoryApi.setFileIndexMethod).mockResolvedValue(group({}));
    const user = userEvent.setup();
    renderWithProviders(
      <FileIndexExcludePatternsModal group={group({ file_index_method: "guest_exec_ephemeral" })} onClose={vi.fn()} onSaved={vi.fn()} />,
    );

    await user.selectOptions(screen.getByLabelText("Indexing method"), "external_mount");
    await user.click(screen.getByRole("button", { name: "Save method" }));

    await waitFor(() => expect(inventoryApi.setFileIndexMethod).toHaveBeenCalledWith("group-1", "external_mount", null));
  });

  it("does not close the modal after saving the method (exclude patterns may still need editing)", async () => {
    setup();
    vi.mocked(inventoryApi.setFileIndexMethod).mockResolvedValue(group({}));
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(<FileIndexExcludePatternsModal group={group({})} onClose={onClose} onSaved={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Save method" }));

    await waitFor(() => expect(inventoryApi.setFileIndexMethod).toHaveBeenCalled());
    expect(onClose).not.toHaveBeenCalled();
  });
});
