import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { ConfigBackupDocument, ConfigImportResult } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { ConfigBackupPanel, configBackupFilename } from "./ConfigBackupPanel";

vi.mock("../api/resources", () => ({
  configBackupApi: { export: vi.fn(), import: vi.fn() },
}));

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, downloadBlob: vi.fn() };
});

const { configBackupApi } = await import("../api/resources");
const { downloadBlob } = await import("../api/client");
const api = vi.mocked(configBackupApi);

const DOC: ConfigBackupDocument = {
  format: 1,
  exported_at: "2026-10-07T08:30:00Z",
  resurgam_version: "0.2.0",
  instance_name: "resurgam-01",
  sections: { customers: [] },
};

function result(overrides: Partial<ConfigImportResult> = {}): ConfigImportResult {
  return {
    dry_run: true,
    applied: false,
    source_resurgam_version: "0.2.0",
    source_exported_at: "2026-10-07T08:30:00Z",
    source_instance_name: "resurgam-01",
    sections: [
      {
        section: "customers",
        create: 1,
        update: 0,
        skip: 0,
        conflict: 1,
        items: [
          { key: "acme", action: "create", reason: null },
          { key: "clash", action: "conflict", reason: "debitor_number 'D-200' is already used by customer 'beta'" },
        ],
      },
      { section: "pbs_instances", create: 1, update: 0, skip: 0, conflict: 0, items: [{ key: "pbs1:8007", action: "create", reason: null }] },
      { section: "branding", create: 0, update: 0, skip: 0, conflict: 0, items: [] },
    ],
    to_reenter: [
      {
        kind: "pbs_token_secret",
        target: "PBS One (pbs1:8007)",
        target_id: null,
        page: "/infrastructure/pbs-instances",
        detail: "Edit the PBS entry and enter the API token secret for token root@pam!resurgam.",
      },
    ],
    notes: ["Configuration backups never contain secrets."],
    ...overrides,
  };
}

function jsonFile(content: string, name = "resurgam-config.json"): File {
  return new File([content], name, { type: "application/json" });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("configBackupFilename", () => {
  it("uses the instance name and export date", () => {
    expect(configBackupFilename(DOC)).toBe("resurgam-config-resurgam-01-2026-10-07.json");
    expect(configBackupFilename({ exported_at: "2026-01-02T00:00:00Z", instance_name: "my host/x" })).toBe(
      "resurgam-config-my-host-x-2026-01-02.json",
    );
  });
});

describe("ConfigBackupPanel", () => {
  it("exports the configuration as a dated JSON download", async () => {
    api.export.mockResolvedValue(DOC);
    renderWithProviders(<ConfigBackupPanel />);

    await userEvent.click(screen.getByRole("button", { name: "Export configuration" }));

    await waitFor(() => expect(downloadBlob).toHaveBeenCalledTimes(1));
    const [blob, filename] = vi.mocked(downloadBlob).mock.calls[0];
    expect(filename).toBe("resurgam-config-resurgam-01-2026-10-07.json");
    expect((blob as Blob).type).toBe("application/json");
  });

  it("shows an export error", async () => {
    api.export.mockRejectedValue(new ApiError(403, "forbidden", "Platform admin required."));
    renderWithProviders(<ConfigBackupPanel />);
    await userEvent.click(screen.getByRole("button", { name: "Export configuration" }));
    expect(await screen.findByText("Platform admin required.")).toBeInTheDocument();
  });

  it("runs a dry run on file choice, then imports only after the in-page confirmation", async () => {
    api.import.mockImplementation(async (_doc, dryRun) =>
      dryRun ? result() : result({ dry_run: false, applied: true, to_reenter: result().to_reenter.map((i) => ({ ...i, target_id: "id-1" })) }),
    );
    renderWithProviders(<ConfigBackupPanel />);

    await userEvent.upload(screen.getByTestId("config-import-file"), jsonFile(JSON.stringify(DOC)));

    await waitFor(() => expect(api.import).toHaveBeenCalledWith(DOC, true));
    const summary = await screen.findByTestId("config-import-summary");
    // empty sections are hidden, conflicts are visible with their reason
    expect(within(summary).getByText("Tenants")).toBeInTheDocument();
    expect(within(summary).getByText("PBS instances")).toBeInTheDocument();
    expect(within(summary).queryByText("Branding")).not.toBeInTheDocument();
    expect(within(summary).getByText(/already used by customer 'beta'/)).toBeInTheDocument();
    expect(screen.getByText(/1 conflict\(s\) will be left untouched/)).toBeInTheDocument();
    expect(screen.getByTestId("config-reenter")).toHaveTextContent("PBS token secret");
    // dry run: no link yet (nothing exists)
    expect(screen.queryByRole("link", { name: "Open page" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    expect(api.import).toHaveBeenCalledTimes(1); // confirmation first, no apply yet
    expect(screen.getByText("Apply the import now?")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Import now" }));
    await waitFor(() => expect(api.import).toHaveBeenLastCalledWith(DOC, false));
    expect(await screen.findByText(/The configuration was imported/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open page" })).toHaveAttribute("href", "/infrastructure/pbs-instances");
    expect(screen.getByText("Configuration backups never contain secrets.")).toBeInTheDocument();
  });

  it("cancelling the confirmation applies nothing", async () => {
    api.import.mockResolvedValue(result());
    renderWithProviders(<ConfigBackupPanel />);
    await userEvent.upload(screen.getByTestId("config-import-file"), jsonFile(JSON.stringify(DOC)));
    await screen.findByTestId("config-import-summary");

    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    const confirm = screen.getByRole("group", { name: "Apply the import now?" });
    await userEvent.click(within(confirm).getByRole("button", { name: "Cancel" }));

    expect(api.import).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Import" })).toBeInTheDocument();
  });

  it("rejects a file that is not JSON without calling the API", async () => {
    renderWithProviders(<ConfigBackupPanel />);
    await userEvent.upload(screen.getByTestId("config-import-file"), jsonFile("not json {"));
    expect(await screen.findByText(/This file is not valid JSON/)).toBeInTheDocument();
    expect(api.import).not.toHaveBeenCalled();
  });

  it("shows the backend's format/validation error with field details", async () => {
    api.import.mockRejectedValue(
      new ApiError(422, "config_backup_invalid", "The configuration backup is not valid.", undefined, {
        errors: [{ field: "sections.customers.0.slug", message: "String should match pattern" }],
      }),
    );
    renderWithProviders(<ConfigBackupPanel />);
    await userEvent.upload(screen.getByTestId("config-import-file"), jsonFile(JSON.stringify({ format: 1 })));
    expect(await screen.findByText(/sections\.customers\.0\.slug: String should match pattern/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Import" })).not.toBeInTheDocument();
  });
});
