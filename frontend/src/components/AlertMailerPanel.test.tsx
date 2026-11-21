import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AlertMailerSettingsOut, WhoAmIResponse } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { AlertMailerPanel, splitRecipients } from "./AlertMailerPanel";

vi.mock("../api/resources", () => ({
  authApi: { whoami: vi.fn() },
  customersApi: { list: vi.fn() },
  alertMailerApi: { get: vi.fn(), update: vi.fn(), sendTest: vi.fn() },
}));

const { authApi, customersApi, alertMailerApi } = await import("../api/resources");

function settings(overrides: Partial<AlertMailerSettingsOut> = {}): AlertMailerSettingsOut {
  return {
    enabled: false, recipients: [], min_severity: "warning", notify_resolved: true, reminder_hours: null,
    open_alarm_count: 2, notified_alarm_count: 0, last_attempt_at: null, last_sent_at: null, last_error: null,
    ...overrides,
  };
}

function loginAs(assignments: WhoAmIResponse["assignments"]) {
  window.localStorage.setItem("resurgam.token", "fake-token");
  vi.mocked(authApi.whoami).mockResolvedValue({
    user_id: "u1", email: "a@example.com", display_name: "A", is_breakglass: false, assignments,
  });
  vi.mocked(customersApi.list).mockResolvedValue([]);
}

describe("splitRecipients", () => {
  it("accepts lines, commas and semicolons and drops duplicates", () => {
    expect(splitRecipients("a@x.ch\nb@y.ch, A@x.ch; c@z.ch ")).toEqual(["a@x.ch", "b@y.ch", "c@z.ch"]);
  });
});

describe("AlertMailerPanel", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.mocked(alertMailerApi.get).mockReset();
    vi.mocked(alertMailerApi.update).mockReset();
    vi.mocked(alertMailerApi.sendTest).mockReset();
  });

  it("shows status, recipients and the last delivery error", async () => {
    loginAs([{ role: "platform_admin", scope_type: "global", scope_id: null }]);
    vi.mocked(alertMailerApi.get).mockResolvedValue(
      settings({ enabled: true, recipients: ["ops@acme.example"], last_error: "SMTP down." }),
    );
    renderWithProviders(<AlertMailerPanel customerId="cust-1" />);

    const panel = within(await screen.findByTestId("alert-mailer"));
    await waitFor(() => expect(panel.getByText("ops@acme.example")).toBeInTheDocument());
    expect(panel.getByText(/Last delivery failed: SMTP down/)).toBeInTheDocument();
    expect(panel.getByText("2 open, 0 already mailed")).toBeInTheDocument();
  });

  it("saves the edited settings", async () => {
    loginAs([{ role: "customer_admin", scope_type: "customer", scope_id: "cust-1" }]);
    vi.mocked(alertMailerApi.get).mockResolvedValue(settings());
    vi.mocked(alertMailerApi.update).mockResolvedValue(settings({ enabled: true, recipients: ["a@x.ch", "b@y.ch"] }));
    const user = userEvent.setup();
    renderWithProviders(<AlertMailerPanel customerId="cust-1" />);

    const panel = within(await screen.findByTestId("alert-mailer"));
    await user.click(await panel.findByRole("button", { name: "Edit" }));
    await user.click(panel.getByLabelText("Send alarm mails"));
    await user.type(panel.getByLabelText("Recipients"), "a@x.ch, b@y.ch");
    await user.selectOptions(panel.getByLabelText("Alarms to mail"), "critical");
    await user.type(panel.getByLabelText("Reminder for open alarms"), "24");
    await user.click(panel.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(alertMailerApi.update).toHaveBeenCalledWith("cust-1", {
        enabled: true,
        recipients: ["a@x.ch", "b@y.ch"],
        min_severity: "critical",
        notify_resolved: true,
        reminder_hours: 24,
      }),
    );
    expect(await panel.findByText("Alert mailer settings saved.")).toBeInTheDocument();
  });

  it("sends a test mail to the saved recipients and reports the result", async () => {
    loginAs([{ role: "platform_admin", scope_type: "global", scope_id: null }]);
    vi.mocked(alertMailerApi.get).mockResolvedValue(settings({ recipients: ["ops@acme.example"] }));
    vi.mocked(alertMailerApi.sendTest).mockResolvedValue({ sent: false, recipients: ["ops@acme.example"], error: "Mail is not configured." });
    const user = userEvent.setup();
    renderWithProviders(<AlertMailerPanel customerId="cust-1" />);

    const panel = within(await screen.findByTestId("alert-mailer"));
    await user.click(await panel.findByRole("button", { name: "Send test mail" }));
    expect(alertMailerApi.sendTest).toHaveBeenCalledWith("cust-1", undefined);
    expect(await panel.findByText(/Test mail could not be sent: Mail is not configured/)).toBeInTheDocument();
  });

  it("is read-only for a viewer of another scope", async () => {
    loginAs([{ role: "viewer", scope_type: "customer", scope_id: "cust-1" }]);
    vi.mocked(alertMailerApi.get).mockResolvedValue(settings({ recipients: ["ops@acme.example"] }));
    renderWithProviders(<AlertMailerPanel customerId="cust-1" />);

    const panel = within(await screen.findByTestId("alert-mailer"));
    await waitFor(() => expect(panel.getByText("ops@acme.example")).toBeInTheDocument());
    expect(panel.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(panel.queryByRole("button", { name: "Send test mail" })).not.toBeInTheDocument();
  });
});
