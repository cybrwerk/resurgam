import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { CredentialOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { CredentialsPage } from "./CredentialsPage";

vi.mock("../api/resources", () => ({
  credentialsApi: { list: vi.fn(), rotate: vi.fn() },
}));

const { credentialsApi } = await import("../api/resources");

const PBS_CREDENTIAL: CredentialOut = {
  secret_reference_id: "ref-1",
  description: "PBS API token for 'Primary PBS'",
  owner_type: "pbs_instance",
  owner_id: "pbs-1",
  owner_label: "Primary PBS",
  created_at: "2026-01-01T00:00:00Z",
  last_rotated_at: "2026-01-01T00:00:00Z",
};

const PVE_CREDENTIAL: CredentialOut = {
  secret_reference_id: "ref-2",
  description: "PVE API token for 'Main Cluster'",
  owner_type: "pve_cluster",
  owner_id: "pve-1",
  owner_label: "Main Cluster",
  created_at: "2026-01-02T00:00:00Z",
  last_rotated_at: "2026-01-02T00:00:00Z",
};

describe("CredentialsPage", () => {
  it("lists every PBS/PVE credential with its owner, never a secret value", async () => {
    vi.mocked(credentialsApi.list).mockResolvedValue([PBS_CREDENTIAL, PVE_CREDENTIAL]);
    renderWithProviders(<CredentialsPage />);

    await waitFor(() => expect(screen.getByText("Primary PBS")).toBeInTheDocument());
    expect(screen.getByText("Main Cluster")).toBeInTheDocument();
    expect(screen.getByText("PBS Instance")).toBeInTheDocument();
    expect(screen.getByText("PVE Cluster")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/secretvalue|api_token/i);
  });

  it("rotates a credential through the modal", async () => {
    vi.mocked(credentialsApi.list).mockResolvedValue([PBS_CREDENTIAL]);
    vi.mocked(credentialsApi.rotate).mockResolvedValue({ ...PBS_CREDENTIAL });
    const user = userEvent.setup();
    renderWithProviders(<CredentialsPage />);

    await waitFor(() => expect(screen.getByText("Primary PBS")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Rotate" }));

    expect(screen.getByText("Rotate credential — Primary PBS")).toBeInTheDocument();
    await user.type(screen.getByLabelText("New value"), "new-secret-token");

    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Rotate" }));

    await waitFor(() =>
      expect(credentialsApi.rotate).toHaveBeenCalledWith("ref-1", "new-secret-token"),
    );
  });
});
