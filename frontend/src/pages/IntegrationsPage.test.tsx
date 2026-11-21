import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { OidcSettingsOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { IntegrationsPage } from "./IntegrationsPage";

vi.mock("../api/resources", () => ({
  integrationsApi: { getOidc: vi.fn(), updateOidc: vi.fn(), testOidc: vi.fn() },
}));

const { integrationsApi } = await import("../api/resources");

const ENV_SETTINGS: OidcSettingsOut = {
  issuer: "https://idp.example.com",
  client_id: "resurgam",
  redirect_uri: "https://app.example.com/api/v1/auth/oidc/callback",
  enabled: false,
  client_secret_configured: true,
  source: "environment",
};

describe("IntegrationsPage", () => {
  it("shows the effective OIDC config and its source", async () => {
    vi.mocked(integrationsApi.getOidc).mockResolvedValue(ENV_SETTINGS);
    renderWithProviders(<IntegrationsPage />);

    await waitFor(() => expect(screen.getByText("https://idp.example.com")).toBeInTheDocument());
    expect(screen.getByText("From environment")).toBeInTheDocument();
    expect(screen.getByText("resurgam")).toBeInTheDocument();
  });

  it("saves an edited config, omitting a blank secret", async () => {
    vi.mocked(integrationsApi.getOidc).mockResolvedValue(ENV_SETTINGS);
    vi.mocked(integrationsApi.updateOidc).mockResolvedValue({ ...ENV_SETTINGS, source: "database" });
    const user = userEvent.setup();
    renderWithProviders(<IntegrationsPage />);

    await waitFor(() => expect(screen.getByText("https://idp.example.com")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Edit" }));

    const issuerInput = screen.getByLabelText("Issuer URL");
    await user.clear(issuerInput);
    await user.type(issuerInput, "https://idp2.example.com");

    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(integrationsApi.updateOidc).toHaveBeenCalledWith({
        issuer: "https://idp2.example.com",
        client_id: "resurgam",
        client_secret: undefined,
        redirect_uri: "https://app.example.com/api/v1/auth/oidc/callback",
        enabled: false,
      }),
    );
  });

  it("keeps in-progress edits when the settings query refetches in the background", async () => {
    // Regression test: a background refetch (e.g. react-query's default
    // refetchOnReconnect firing on a network reconnect) while the edit
    // form is open must not silently overwrite unsaved typing.
    vi.mocked(integrationsApi.getOidc).mockResolvedValue(ENV_SETTINGS);
    const user = userEvent.setup();
    const { queryClient } = renderWithProviders(<IntegrationsPage />);

    await waitFor(() => expect(screen.getByText("https://idp.example.com")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Edit" }));

    const issuerInput = screen.getByLabelText("Issuer URL");
    await user.clear(issuerInput);
    await user.type(issuerInput, "https://still-typing.example.com");

    // Simulates react-query's default refetchOnReconnect firing while
    // the form is open (a network blip, not a user action).
    await queryClient.refetchQueries({ queryKey: ["integrations-oidc"] });
    await waitFor(() => expect(integrationsApi.getOidc).toHaveBeenCalledTimes(2));

    expect(screen.getByLabelText("Issuer URL")).toHaveValue("https://still-typing.example.com");
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
  });

  it("tests discovery against the entered issuer before saving", async () => {
    vi.mocked(integrationsApi.getOidc).mockResolvedValue(ENV_SETTINGS);
    vi.mocked(integrationsApi.testOidc).mockResolvedValue({ reachable: true, error: null });
    const user = userEvent.setup();
    renderWithProviders(<IntegrationsPage />);

    await waitFor(() => expect(screen.getByText("https://idp.example.com")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("button", { name: "Test" }));

    await waitFor(() => expect(integrationsApi.testOidc).toHaveBeenCalledWith("https://idp.example.com"));
    expect(await screen.findByText("Reachable")).toBeInTheDocument();
  });
});
