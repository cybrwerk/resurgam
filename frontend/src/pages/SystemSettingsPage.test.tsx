import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { BrandingSettingsOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { SystemSettingsPage } from "./SystemSettingsPage";

vi.mock("../api/resources", () => ({
  brandingApi: {
    get: vi.fn(),
    logoUrl: vi.fn(() => "http://localhost/api/v1/branding/logo"),
    update: vi.fn(),
    reset: vi.fn(),
    removeLogo: vi.fn(),
  },
  instanceSettingsApi: { get: vi.fn(), update: vi.fn() },
  mailSettingsApi: { get: vi.fn(), update: vi.fn(), sendTest: vi.fn() },
}));

const { brandingApi, instanceSettingsApi, mailSettingsApi } = await import("../api/resources");

const UNCONFIGURED: BrandingSettingsOut = { logo_configured: false, primary_color: null, accent_color: null };

const MAIL_UNCONFIGURED = {
  smtp_host: "", smtp_port: 587, smtp_username: null, smtp_password_configured: false,
  use_tls: true, from_address: "", from_name: null, enabled: false,
};

function setupInstanceSettings() {
  vi.mocked(instanceSettingsApi.get).mockResolvedValue({ min_report_retention_days: 365, collector_poll_interval_minutes: 15 });
  vi.mocked(mailSettingsApi.get).mockResolvedValue(MAIL_UNCONFIGURED);
}

describe("SystemSettingsPage — branding", () => {
  it("shows the RESURGAM defaults when unconfigured", async () => {
    setupInstanceSettings();
    vi.mocked(brandingApi.get).mockResolvedValue(UNCONFIGURED);
    renderWithProviders(<SystemSettingsPage />);

    await waitFor(() => expect(screen.getByText("Using the default logo")).toBeInTheDocument());
    expect(screen.getAllByText("Using the default")).toHaveLength(2);
  });

  it("shows the configured logo and colors", async () => {
    setupInstanceSettings();
    vi.mocked(brandingApi.get).mockResolvedValue({
      logo_configured: true,
      primary_color: "#123456",
      accent_color: "#abcdef",
    });
    renderWithProviders(<SystemSettingsPage />);

    await waitFor(() => expect(screen.getByText("#123456")).toBeInTheDocument());
    expect(screen.getByText("#abcdef")).toBeInTheDocument();
    expect(document.querySelector("img")).toHaveAttribute("src", "http://localhost/api/v1/branding/logo");
  });

  it("saves edited colors without a new logo file", async () => {
    setupInstanceSettings();
    vi.mocked(brandingApi.get).mockResolvedValue(UNCONFIGURED);
    vi.mocked(brandingApi.update).mockResolvedValue({ logo_configured: false, primary_color: "#ff0000", accent_color: "#00ff00" });
    const user = userEvent.setup();
    renderWithProviders(<SystemSettingsPage />);

    await waitFor(() => expect(screen.getByText("Using the default logo")).toBeInTheDocument());
    const branding = within(screen.getByTestId("branding-settings"));
    await user.click(branding.getByRole("button", { name: "Edit" }));

    const primaryInput = branding.getByLabelText("Primary color");
    fireEvent.change(primaryInput, { target: { value: "#ff0000" } });

    await user.click(branding.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(brandingApi.update).toHaveBeenCalledWith({
        logo: undefined,
        primaryColor: "#ff0000",
        accentColor: "#b08d57",
        appName: "",
      }),
    );
  });

  it("sets a custom app title", async () => {
    setupInstanceSettings();
    vi.mocked(brandingApi.get).mockResolvedValue(UNCONFIGURED);
    vi.mocked(brandingApi.update).mockResolvedValue({ ...UNCONFIGURED, app_name: "Acme Backup" });
    const user = userEvent.setup();
    renderWithProviders(<SystemSettingsPage />);

    const branding = within(await screen.findByTestId("branding-settings"));
    expect(branding.getByText("RESURGAM (default)")).toBeInTheDocument();
    await user.click(branding.getByRole("button", { name: "Edit" }));
    await user.type(branding.getByLabelText("App title"), "Acme Backup");
    await user.click(branding.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(brandingApi.update).toHaveBeenCalledWith(expect.objectContaining({ appName: "Acme Backup" })),
    );
  });

  it("restores the default branding after confirmation", async () => {
    setupInstanceSettings();
    vi.mocked(brandingApi.get).mockResolvedValue({
      logo_configured: true,
      primary_color: "#123456",
      accent_color: "#abcdef",
      app_name: "Acme",
    });
    vi.mocked(brandingApi.reset).mockResolvedValue(UNCONFIGURED);
    const user = userEvent.setup();
    renderWithProviders(<SystemSettingsPage />);

    const branding = within(await screen.findByTestId("branding-settings"));
    await user.click(await branding.findByRole("button", { name: "Restore defaults" }));
    const dialog = await screen.findByRole("dialog", { name: "Restore the default branding?" });
    await user.click(within(dialog).getByRole("button", { name: "Restore defaults" }));
    await waitFor(() => expect(brandingApi.reset).toHaveBeenCalled());
  });

  it("loads the defaults into the form and saves them as a reset", async () => {
    setupInstanceSettings();
    vi.mocked(brandingApi.get).mockResolvedValue({
      logo_configured: true,
      primary_color: "#123456",
      accent_color: "#abcdef",
      app_name: "Acme",
    });
    vi.mocked(brandingApi.reset).mockResolvedValue(UNCONFIGURED);
    const user = userEvent.setup();
    renderWithProviders(<SystemSettingsPage />);

    const branding = within(await screen.findByTestId("branding-settings"));
    await user.click(await branding.findByRole("button", { name: "Edit" }));
    await user.click(branding.getByRole("button", { name: "Reset form to defaults" }));
    expect(branding.getByLabelText("App title")).toHaveValue("");
    expect(branding.getByLabelText("Primary color")).toHaveValue("#005f6b");
    expect(branding.getByTestId("branding-primary-color-hex")).toHaveTextContent("#005F6B");
    expect(branding.getByLabelText("Remove the custom logo")).toBeChecked();
    await user.click(branding.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(brandingApi.reset).toHaveBeenCalled());
    expect(brandingApi.update).not.toHaveBeenCalled();
  });

  it("resetting the form without a custom logo never removes a logo", async () => {
    setupInstanceSettings();
    vi.mocked(brandingApi.get).mockResolvedValue({
      logo_configured: false,
      primary_color: "#123456",
      accent_color: "#abcdef",
      app_name: "Acme",
    });
    vi.mocked(brandingApi.update).mockResolvedValue({
      logo_configured: false,
      primary_color: "#005f6b",
      accent_color: "#b08d57",
      app_name: "Other",
    });
    const user = userEvent.setup();
    renderWithProviders(<SystemSettingsPage />);

    const branding = within(await screen.findByTestId("branding-settings"));
    await user.click(await branding.findByRole("button", { name: "Edit" }));
    await user.click(branding.getByRole("button", { name: "Reset form to defaults" }));
    await user.type(branding.getByLabelText("App title"), "Other");
    await user.click(branding.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(brandingApi.update).toHaveBeenCalled());
    expect(brandingApi.removeLogo).not.toHaveBeenCalled();
    expect(brandingApi.reset).not.toHaveBeenCalled();
  });

  it("removes only the logo when asked", async () => {
    setupInstanceSettings();
    vi.mocked(brandingApi.get).mockResolvedValue({ logo_configured: true, primary_color: "#123456", accent_color: null });
    vi.mocked(brandingApi.removeLogo).mockResolvedValue({ logo_configured: false, primary_color: "#123456", accent_color: null });
    vi.mocked(brandingApi.update).mockResolvedValue({ logo_configured: false, primary_color: "#123456", accent_color: "#b08d57" });
    const user = userEvent.setup();
    renderWithProviders(<SystemSettingsPage />);

    const branding = within(await screen.findByTestId("branding-settings"));
    await user.click(await branding.findByRole("button", { name: "Edit" }));
    await user.click(branding.getByLabelText("Remove the custom logo"));
    await user.click(branding.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(brandingApi.removeLogo).toHaveBeenCalled());
    expect(brandingApi.update).toHaveBeenCalledWith(expect.objectContaining({ primaryColor: "#123456" }));
    expect(brandingApi.reset).not.toHaveBeenCalled();
  });

  it("keeps in-progress edits when the settings query refetches in the background", async () => {
    setupInstanceSettings();
    vi.mocked(brandingApi.get).mockResolvedValue(UNCONFIGURED);
    const user = userEvent.setup();
    const { queryClient } = renderWithProviders(<SystemSettingsPage />);

    await waitFor(() => expect(screen.getByText("Using the default logo")).toBeInTheDocument());
    const branding = within(screen.getByTestId("branding-settings"));
    await user.click(branding.getByRole("button", { name: "Edit" }));

    const primaryInput = branding.getByLabelText("Primary color");
    fireEvent.change(primaryInput, { target: { value: "#ff0000" } });

    await queryClient.refetchQueries({ queryKey: ["branding"] });
    await waitFor(() => expect(brandingApi.get).toHaveBeenCalledTimes(2));

    expect(branding.getByLabelText("Primary color")).toHaveValue("#ff0000");
    expect(branding.getByRole("button", { name: "Save" })).toBeInTheDocument();
  });
});

describe("SystemSettingsPage — report retention floor", () => {
  it("shows the current floor and saves an edit", async () => {
    vi.mocked(brandingApi.get).mockResolvedValue(UNCONFIGURED);
    vi.mocked(instanceSettingsApi.get).mockResolvedValue({ min_report_retention_days: 365, collector_poll_interval_minutes: 15 });
    vi.mocked(instanceSettingsApi.update).mockResolvedValue({ min_report_retention_days: 730, collector_poll_interval_minutes: 15 });
    vi.mocked(mailSettingsApi.get).mockResolvedValue(MAIL_UNCONFIGURED);
    const user = userEvent.setup();
    renderWithProviders(<SystemSettingsPage />, { route: "/?tab=reports" });

    const section = within(await screen.findByTestId("report-retention-settings"));
    await waitFor(() => expect(section.getByText("365")).toBeInTheDocument());

    await user.click(section.getByRole("button", { name: "Edit" }));
    const input = section.getByLabelText("Minimum retention (days)");
    await user.clear(input);
    await user.type(input, "730");
    await user.click(section.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(instanceSettingsApi.update).toHaveBeenCalledWith(730, 15));
  });

  it("shows the current poll interval and saves an edit", async () => {
    vi.mocked(brandingApi.get).mockResolvedValue(UNCONFIGURED);
    vi.mocked(instanceSettingsApi.get).mockResolvedValue({ min_report_retention_days: 365, collector_poll_interval_minutes: 15 });
    vi.mocked(instanceSettingsApi.update).mockResolvedValue({ min_report_retention_days: 365, collector_poll_interval_minutes: 30 });
    vi.mocked(mailSettingsApi.get).mockResolvedValue(MAIL_UNCONFIGURED);
    const user = userEvent.setup();
    renderWithProviders(<SystemSettingsPage />, { route: "/?tab=reports" });

    const section = within(await screen.findByTestId("report-retention-settings"));
    await waitFor(() => expect(section.getByText("15")).toBeInTheDocument());

    await user.click(section.getByRole("button", { name: "Edit" }));
    const input = section.getByLabelText("Collector poll interval (minutes)");
    await user.clear(input);
    await user.type(input, "30");
    await user.click(section.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(instanceSettingsApi.update).toHaveBeenCalledWith(365, 30));
  });
});

describe("SystemSettingsPage — mail adapter", () => {
  it("shows not-configured when no SMTP host is set", async () => {
    setupInstanceSettings();
    vi.mocked(brandingApi.get).mockResolvedValue(UNCONFIGURED);
    renderWithProviders(<SystemSettingsPage />, { route: "/?tab=mail" });

    const section = within(await screen.findByTestId("mail-settings"));
    await waitFor(() => expect(section.getByText("Not configured")).toBeInTheDocument());
    expect(section.queryByRole("button", { name: "Send test" })).not.toBeInTheDocument();
  });

  it("saves SMTP settings including a new password", async () => {
    setupInstanceSettings();
    vi.mocked(brandingApi.get).mockResolvedValue(UNCONFIGURED);
    vi.mocked(mailSettingsApi.update).mockResolvedValue({
      smtp_host: "mail.internal.example", smtp_port: 587, smtp_username: "resurgam", smtp_password_configured: true,
      use_tls: true, from_address: "backup@example.com", from_name: null, enabled: true,
    });
    const user = userEvent.setup();
    renderWithProviders(<SystemSettingsPage />, { route: "/?tab=mail" });

    const section = within(await screen.findByTestId("mail-settings"));
    await waitFor(() => expect(section.getByText("Not configured")).toBeInTheDocument());
    await user.click(section.getByRole("button", { name: "Edit" }));

    await user.type(section.getByLabelText("SMTP host"), "mail.internal.example");
    await user.type(section.getByLabelText("From address"), "backup@example.com");
    await user.type(section.getByLabelText("SMTP password"), "s3cret");
    await user.click(section.getByLabelText("Outbound mail active"));
    await user.click(section.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(mailSettingsApi.update).toHaveBeenCalledWith(
        expect.objectContaining({
          smtp_host: "mail.internal.example", from_address: "backup@example.com", smtp_password: "s3cret", enabled: true,
        }),
      ),
    );
  });

  it("omits smtp_password from the request when the field is left blank", async () => {
    setupInstanceSettings();
    vi.mocked(brandingApi.get).mockResolvedValue(UNCONFIGURED);
    vi.mocked(mailSettingsApi.get).mockResolvedValue({
      smtp_host: "mail.internal.example", smtp_port: 587, smtp_username: null, smtp_password_configured: true,
      use_tls: true, from_address: "backup@example.com", from_name: null, enabled: true,
    });
    vi.mocked(mailSettingsApi.update).mockResolvedValue({
      smtp_host: "mail2.internal.example", smtp_port: 587, smtp_username: null, smtp_password_configured: true,
      use_tls: true, from_address: "backup@example.com", from_name: null, enabled: true,
    });
    const user = userEvent.setup();
    renderWithProviders(<SystemSettingsPage />, { route: "/?tab=mail" });

    const section = within(await screen.findByTestId("mail-settings"));
    await waitFor(() => expect(section.getByText("mail.internal.example")).toBeInTheDocument());
    await user.click(section.getByRole("button", { name: "Edit" }));

    const hostInput = section.getByLabelText("SMTP host");
    await user.clear(hostInput);
    await user.type(hostInput, "mail2.internal.example");
    await user.click(section.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mailSettingsApi.update).toHaveBeenCalled());
    const call = vi.mocked(mailSettingsApi.update).mock.calls[0][0];
    expect("smtp_password" in call).toBe(false);
  });

  it("sends a test email and shows the result", async () => {
    setupInstanceSettings();
    vi.mocked(brandingApi.get).mockResolvedValue(UNCONFIGURED);
    vi.mocked(mailSettingsApi.get).mockResolvedValue({
      smtp_host: "mail.internal.example", smtp_port: 587, smtp_username: null, smtp_password_configured: false,
      use_tls: true, from_address: "backup@example.com", from_name: null, enabled: true,
    });
    vi.mocked(mailSettingsApi.sendTest).mockResolvedValue({ sent: true, error: null });
    const user = userEvent.setup();
    renderWithProviders(<SystemSettingsPage />, { route: "/?tab=mail" });

    const section = within(await screen.findByTestId("mail-settings"));
    await waitFor(() => expect(section.getByText("mail.internal.example")).toBeInTheDocument());

    // The test-send form is its own panel next to the settings (2026-10-04).
    const testPanel = within(screen.getByTestId("mail-test-email"));
    await user.type(testPanel.getByLabelText("Send a test email to"), "admin@example.com");
    await user.click(testPanel.getByRole("button", { name: "Send test" }));

    await waitFor(() => expect(mailSettingsApi.sendTest).toHaveBeenCalledWith("admin@example.com"));
    expect(await testPanel.findByText("Test email sent successfully.")).toBeInTheDocument();
  });
});

describe("SystemSettingsPage — tabs", () => {
  it("shows one settings area at a time and switches on tab click", async () => {
    vi.mocked(brandingApi.get).mockResolvedValue({ logo_configured: false, primary_color: null, accent_color: null });
    const user = userEvent.setup();
    renderWithProviders(<SystemSettingsPage />);

    await waitFor(() => expect(screen.getByTestId("branding-settings")).toBeInTheDocument());
    expect(screen.queryByTestId("mail-settings")).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Mail Adapter" }));
    expect(screen.getByRole("tab", { name: "Mail Adapter" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByTestId("branding-settings")).not.toBeInTheDocument();
  });
});
