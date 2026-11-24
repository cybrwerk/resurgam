import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ZabbixSetupOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { ZabbixMonitoringPage } from "./ZabbixMonitoringPage";

vi.mock("../api/resources", () => ({
  zabbixApi: { setup: vi.fn(), downloadTemplate: vi.fn(), downloadCaCertificate: vi.fn() },
  apiTokensApi: { grantable: vi.fn(), create: vi.fn() },
}));

const { zabbixApi, apiTokensApi } = await import("../api/resources");

const SETUP: ZabbixSetupOut = {
  appliance_url: "https://resurgam.example.internal",
  template_name: "RESURGAM by HTTP",
  template_version: "0.3-1",
  min_zabbix_version: "7.0",
  ca_certificate_available: true,
  datastores: 3,
  datastores_with_usage: 2,
  job_types: 4,
};

const TOKEN = "rsg_abcdefghijklmnopqrstuvwxyz0123456789";

beforeEach(() => {
  vi.mocked(zabbixApi.setup).mockResolvedValue(SETUP);
  vi.mocked(zabbixApi.downloadTemplate).mockResolvedValue();
  vi.mocked(apiTokensApi.grantable).mockResolvedValue({ can_grant_any: true, options: [], max_lifetime_days: 548 });
  vi.mocked(apiTokensApi.create).mockResolvedValue({
    id: "t1",
    name: "Zabbix",
    token_prefix: "rsg_abcd",
    token: TOKEN,
  } as never);
});

describe("ZabbixMonitoringPage", () => {
  it("walks from the overview through token, template and host to done", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ZabbixMonitoringPage />);

    const discovered = await screen.findByTestId("zabbix-discovered");
    expect(discovered).toHaveTextContent("3 datastores");
    expect(discovered).toHaveTextContent("1 without a measurement yet");
    expect(discovered).toHaveTextContent("4 job types");
    expect(screen.getByTestId("zabbix-step-overview")).toHaveTextContent("https://resurgam.example.internal");

    await user.click(screen.getByRole("button", { name: "Next" }));
    const tokenStep = screen.getByTestId("zabbix-step-token");
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    await user.type(within(tokenStep).getByLabelText(/Zabbix server addresses/), "192.0.2.10, 198.51.100.0/24");
    await user.click(within(tokenStep).getByRole("button", { name: "Create token" }));

    await waitFor(() =>
      expect(apiTokensApi.create).toHaveBeenCalledWith({
        name: "Zabbix",
        expires_in_days: 365,
        read_only: true,
        grants: [{ role: "platform_admin", scope_type: "global", scope_id: null }],
        allowed_cidrs: ["192.0.2.10", "198.51.100.0/24"],
      }),
    );
    expect(await screen.findByTestId("zabbix-token-created")).toHaveTextContent(TOKEN);
    // The token is shown once: no going on before confirming it was copied.
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    await user.click(screen.getByLabelText("I have copied the token"));
    await user.click(screen.getByRole("button", { name: "Next" }));

    await user.click(screen.getByRole("button", { name: "Download template" }));
    expect(zabbixApi.downloadTemplate).toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Next" }));

    const hostStep = screen.getByTestId("zabbix-step-host");
    expect(hostStep).toHaveTextContent("{$RESURGAM.URL}");
    expect(hostStep).toHaveTextContent("https://resurgam.example.internal");
    // The token is copyable here but no longer shown in full.
    expect(hostStep).toHaveTextContent("rsg_abcdefgh…");
    expect(hostStep).not.toHaveTextContent(TOKEN);
    expect(within(hostStep).getByRole("button", { name: "Download CA certificate" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByTestId("zabbix-step-done")).toHaveTextContent("{$RESURGAM.DATASTORE.PUSED.WARN}");
    expect(screen.getByRole("button", { name: "Finish" })).toBeEnabled();
  });

  it("lets an existing token be used instead of creating one", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ZabbixMonitoringPage />);
    await screen.findByTestId("zabbix-discovered");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByLabelText("I already have a suitable token"));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByTestId("zabbix-step-host")).toHaveTextContent("enter your existing token");
    expect(apiTokensApi.create).not.toHaveBeenCalled();
  });
});
