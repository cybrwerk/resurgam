import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { CustomerFileIndexSettingsOut, CustomerOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { CustomersPage, slugFromName } from "./CustomersPage";

vi.mock("../api/resources", () => ({
  customersApi: {
    list: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    disable: vi.fn(),
    enable: vi.fn(),
    exportCsv: vi.fn(),
    importCsv: vi.fn(),
  },
  datastoresApi: { list: vi.fn() },
  pbsInstancesApi: { list: vi.fn() },
  billingApi: { get: vi.fn() },
  fileIndexSettingsApi: { get: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn() },
  complianceAttestationsApi: { listForCustomer: vi.fn(), createForCustomer: vi.fn() },
  legApplicabilityApi: { list: vi.fn(), update: vi.fn() },
  instanceSettingsApi: { get: vi.fn(), update: vi.fn() },
  notificationSettingsApi: { get: vi.fn(), update: vi.fn() },
  alertMailerApi: { get: vi.fn(), update: vi.fn(), sendTest: vi.fn() },
  pveClustersApi: { list: vi.fn() },
}));

const {
  customersApi, datastoresApi, pbsInstancesApi, billingApi, fileIndexSettingsApi, complianceAttestationsApi, legApplicabilityApi,
  instanceSettingsApi, notificationSettingsApi, alertMailerApi, pveClustersApi,
} = await import("../api/resources");

const ACTIVE_CUSTOMER: CustomerOut = {
  id: "cust-1",
  name: "Acme AG",
  slug: "acme",
  debitor_number: "1000",
  match_code: "ACM",
  disabled: false,
  created_at: "2026-01-01T00:00:00Z",
  report_retention_days: null,
};

const DISABLED_CUSTOMER: CustomerOut = { ...ACTIVE_CUSTOMER, id: "cust-2", name: "Beta GmbH", disabled: true };

function setup(customers: CustomerOut[]) {
  vi.mocked(customersApi.list).mockResolvedValue(customers);
  vi.mocked(datastoresApi.list).mockResolvedValue([]);
  vi.mocked(pbsInstancesApi.list).mockResolvedValue([]);
  vi.mocked(pveClustersApi.list).mockResolvedValue([]);
  vi.mocked(billingApi.get).mockResolvedValue({
    customer_id: "cust-1",
    customer_name: "Acme AG",
    customer_match_code: "ACM",
    vm_count: 0,
    ct_count: 0,
    host_count: 0,
    estimated_physical_bytes: 0,
    physical_bytes_estimate_complete: true,
    pricing_plan_id: null,
    pricing_plan_source: null,
    price_per_vm_month: null,
    price_per_ct_month: null,
    price_per_host_month: null,
    price_per_gb_month: null,
    currency: null,
    estimated_monthly_cost: null,
  });
  vi.mocked(fileIndexSettingsApi.get).mockRejectedValue(new ApiError(404, "not_found", "Not found"));
  vi.mocked(complianceAttestationsApi.listForCustomer).mockResolvedValue([]);
  vi.mocked(legApplicabilityApi.list).mockResolvedValue(
    (["copies", "media_types", "offsite", "immutable", "zero_errors"] as const).map((leg) => ({
      leg, applicable: true, updated_by_display_name: null, updated_at: null,
    })),
  );
  vi.mocked(instanceSettingsApi.get).mockResolvedValue({ min_report_retention_days: 365, collector_poll_interval_minutes: 15 });
  vi.mocked(notificationSettingsApi.get).mockResolvedValue({
    report_notification_email: null, report_notifications_enabled: false,
    alarm_notification_email: null, alarm_notifications_enabled: false,
    billing_notification_email: null, billing_notifications_enabled: false,
  });
  vi.mocked(alertMailerApi.get).mockResolvedValue({
    enabled: false, recipients: [], min_severity: "warning", notify_resolved: true, reminder_hours: null,
    open_alarm_count: 0, notified_alarm_count: 0, last_attempt_at: null, last_sent_at: null, last_error: null,
  });
}

function fileIndexSettings(overrides: Partial<CustomerFileIndexSettingsOut> = {}): CustomerFileIndexSettingsOut {
  return {
    customer_id: "cust-1",
    enabled: true,
    retention_days: 30,
    schedule_enabled: false,
    schedule_interval_hours: null,
    window_start_time: null,
    window_end_time: null,
    next_scheduled_run_at: null,
    guest_exec_pve_cluster_id: null,
    guest_exec_target_node: null,
    guest_exec_target_storage: null,
    full_reindex_every_n_runs: null,
    ...overrides,
  };
}

describe("CustomersPage — status column", () => {
  it("shows an active customer as Active, not as the 'Enable' action verb", async () => {
    setup([ACTIVE_CUSTOMER]);
    renderWithProviders(<CustomersPage />);

    const table = within(await screen.findByRole("table"));
    expect(table.getByText("Active")).toBeInTheDocument();
    expect(table.queryByText("Enable")).not.toBeInTheDocument();
    expect(table.queryByText("Disable")).not.toBeInTheDocument();
  });

  it("shows a disabled customer as Disabled, not as the 'Disable' action verb", async () => {
    setup([DISABLED_CUSTOMER]);
    renderWithProviders(<CustomersPage />);

    const table = within(await screen.findByRole("table"));
    expect(table.getByText("Disabled")).toBeInTheDocument();
    expect(table.queryByText("Enable")).not.toBeInTheDocument();
    expect(table.queryByText("Disable")).not.toBeInTheDocument();
  });

  it("shows the same correct status in the detail modal, alongside the real toggle action", async () => {
    setup([ACTIVE_CUSTOMER]);
    const user = userEvent.setup();
    renderWithProviders(<CustomersPage />);

    await user.click(await screen.findByText("Acme AG"));
    await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument());

    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByText("Active")).toBeInTheDocument();
    // the real action button still correctly offers "Disable" for an active customer
    expect(dialog.getByRole("button", { name: "Disable" })).toBeInTheDocument();
  });
});

describe("CustomersPage — report retention", () => {
  it("shows the platform floor when no override is set", async () => {
    setup([ACTIVE_CUSTOMER]);
    renderWithProviders(<CustomersPage />);

    await userEvent.setup().click(await screen.findByText("Acme AG"));
    const dialog = within(await screen.findByRole("dialog"));
    await waitFor(() => expect(dialog.getByText("Using the platform floor (365 days)")).toBeInTheDocument());
  });

  it("shows a real override when one is set", async () => {
    setup([{ ...ACTIVE_CUSTOMER, report_retention_days: 400 }]);
    renderWithProviders(<CustomersPage />);

    await userEvent.setup().click(await screen.findByText("Acme AG"));
    const dialog = within(await screen.findByRole("dialog"));
    await waitFor(() => expect(dialog.getByText("400 days")).toBeInTheDocument());
  });

  it("sets an override by editing the field", async () => {
    setup([ACTIVE_CUSTOMER]);
    vi.mocked(customersApi.update).mockResolvedValue({ ...ACTIVE_CUSTOMER, report_retention_days: 400 });
    const user = userEvent.setup();
    renderWithProviders(<CustomersPage />);

    await user.click(await screen.findByText("Acme AG"));
    const dialog = within(await screen.findByRole("dialog"));
    await waitFor(() => expect(dialog.getByText("Using the platform floor (365 days)")).toBeInTheDocument());

    // The customer's own top-level "Edit" is the last one in the modal --
    // several nested sections (File Index Settings, Notifications, ...)
    // each have their own "Edit" button too.
    const editButtons = dialog.getAllByRole("button", { name: "Edit" });
    await user.click(editButtons[editButtons.length - 1]);
    await user.type(dialog.getByLabelText("Report retention"), "400");
    await user.click(dialog.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(customersApi.update).toHaveBeenCalledWith(
        "cust-1", expect.objectContaining({ report_retention_days: 400 }),
      ),
    );
  });

  it("clears an override back to the floor by emptying the field", async () => {
    setup([{ ...ACTIVE_CUSTOMER, report_retention_days: 400 }]);
    vi.mocked(customersApi.update).mockResolvedValue({ ...ACTIVE_CUSTOMER, report_retention_days: null });
    const user = userEvent.setup();
    renderWithProviders(<CustomersPage />);

    await user.click(await screen.findByText("Acme AG"));
    const dialog = within(await screen.findByRole("dialog"));
    await waitFor(() => expect(dialog.getByText("400 days")).toBeInTheDocument());

    const editButtons = dialog.getAllByRole("button", { name: "Edit" });
    await user.click(editButtons[editButtons.length - 1]);
    const input = dialog.getByLabelText("Report retention");
    await user.clear(input);
    await user.click(dialog.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(customersApi.update).toHaveBeenCalledWith(
        "cust-1", expect.objectContaining({ report_retention_days: null }),
      ),
    );
  });
});

describe("CustomersPage — File Index settings (manifest §13, ADR 0016)", () => {
  it("offers an Enable CTA when indexing was never configured", async () => {
    setup([ACTIVE_CUSTOMER]);
    const user = userEvent.setup();
    renderWithProviders(<CustomersPage />);

    await user.click(await screen.findByText("Acme AG"));
    const dialog = within(await screen.findByRole("dialog"));
    await waitFor(() =>
      expect(dialog.getByText("File indexing has not been enabled for this tenant.")).toBeInTheDocument(),
    );
  });

  it("creates settings from the Enable CTA", async () => {
    setup([ACTIVE_CUSTOMER]);
    vi.mocked(fileIndexSettingsApi.create).mockResolvedValue(fileIndexSettings());
    const user = userEvent.setup();
    renderWithProviders(<CustomersPage />);

    await user.click(await screen.findByText("Acme AG"));
    const dialog = within(await screen.findByRole("dialog"));
    await waitFor(() => expect(dialog.getByText("File indexing has not been enabled for this tenant.")).toBeInTheDocument());

    await user.click(dialog.getByRole("button", { name: "Enable" }));
    await user.clear(dialog.getByLabelText("Retention (days)"));
    await user.type(dialog.getByLabelText("Retention (days)"), "90");
    await user.click(dialog.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(fileIndexSettingsApi.create).toHaveBeenCalledWith("cust-1", {
        retention_days: 90,
        schedule_enabled: false,
        schedule_interval_hours: null,
        window_start_time: null,
        window_end_time: null,
        guest_exec_pve_cluster_id: null,
        full_reindex_every_n_runs: null,
      }),
    );
  });

  it("shows existing settings read-only and toggles enabled", async () => {
    setup([ACTIVE_CUSTOMER]);
    vi.mocked(fileIndexSettingsApi.get).mockResolvedValue(fileIndexSettings({ retention_days: 45 }));
    vi.mocked(fileIndexSettingsApi.update).mockResolvedValue(fileIndexSettings({ retention_days: 45, enabled: false }));
    const user = userEvent.setup();
    renderWithProviders(<CustomersPage />);

    await user.click(await screen.findByText("Acme AG"));
    await waitFor(() => expect(screen.getByTestId("file-index-settings")).toHaveTextContent("45"));

    // Two "Disable" buttons coexist in the modal (the customer's own, and
    // file indexing's own toggle) -- scoping to the section's own
    // data-testid disambiguates which one this is.
    const section = within(screen.getByTestId("file-index-settings"));
    await user.click(section.getByRole("button", { name: "Disable" }));

    await waitFor(() => expect(fileIndexSettingsApi.update).toHaveBeenCalledWith("cust-1", { enabled: false }));
  });

  it("deletes settings after confirming, warning about purging entries", async () => {
    setup([ACTIVE_CUSTOMER]);
    vi.mocked(fileIndexSettingsApi.get).mockResolvedValue(fileIndexSettings());
    vi.mocked(fileIndexSettingsApi.remove).mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderWithProviders(<CustomersPage />);

    await user.click(await screen.findByText("Acme AG"));
    const dialog = within(await screen.findByRole("dialog"));
    await waitFor(() => expect(dialog.getByText("30")).toBeInTheDocument());

    await user.click(dialog.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(screen.getByText("Delete file index settings?")).toBeInTheDocument());
    expect(screen.getByText(/permanently deletes every indexed entry/)).toBeInTheDocument();

    await user.click(screen.getAllByRole("button", { name: "Delete" })[1]);
    await waitFor(() => expect(fileIndexSettingsApi.remove).toHaveBeenCalledWith("cust-1"));
  });
});

describe("CustomersPage — 3-2-1-1-0 self-assessment", () => {
  it("shows not-attested defaults, then records a production copy count", async () => {
    setup([ACTIVE_CUSTOMER]);
    vi.mocked(complianceAttestationsApi.createForCustomer).mockResolvedValue({
      id: "attest-1", scope_type: "customer", scope_id: "cust-1", dimension: "production_copy_count", value: 3,
      notes: null, asserted_by_user_id: "user-1", asserted_by_display_name: "Admin One", created_at: "2026-01-01T00:00:00Z",
    });
    const user = userEvent.setup();
    renderWithProviders(<CustomersPage />);

    await user.click(await screen.findByText("Acme AG"));
    const dialog = within(await screen.findByRole("dialog"));
    await waitFor(() => expect(dialog.getAllByText("Not attested yet").length).toBe(2));

    await user.clear(dialog.getByLabelText("Record copy count"));
    await user.type(dialog.getByLabelText("Record copy count"), "3");
    await user.click(dialog.getAllByRole("button", { name: "Record" })[0]);

    await waitFor(() =>
      expect(complianceAttestationsApi.createForCustomer).toHaveBeenCalledWith("cust-1", {
        dimension: "production_copy_count", value: 3,
      }),
    );
  });

  it("records media type diversity as a comma-separated list", async () => {
    setup([ACTIVE_CUSTOMER]);
    vi.mocked(complianceAttestationsApi.createForCustomer).mockResolvedValue({
      id: "attest-2", scope_type: "customer", scope_id: "cust-1", dimension: "media_type_diversity", value: ["disk", "tape"],
      notes: null, asserted_by_user_id: "user-1", asserted_by_display_name: "Admin One", created_at: "2026-01-01T00:00:00Z",
    });
    const user = userEvent.setup();
    renderWithProviders(<CustomersPage />);

    await user.click(await screen.findByText("Acme AG"));
    const dialog = within(await screen.findByRole("dialog"));
    await waitFor(() => expect(dialog.getByLabelText("Record media types")).toBeInTheDocument());

    await user.type(dialog.getByLabelText("Record media types"), "disk, tape");
    await user.click(dialog.getAllByRole("button", { name: "Record" })[1]);

    await waitFor(() =>
      expect(complianceAttestationsApi.createForCustomer).toHaveBeenCalledWith("cust-1", {
        dimension: "media_type_diversity", value: ["disk", "tape"],
      }),
    );
  });

  it("shows the current attested values, not just history", async () => {
    setup([ACTIVE_CUSTOMER]);
    vi.mocked(complianceAttestationsApi.listForCustomer).mockResolvedValue([
      {
        id: "attest-1", scope_type: "customer", scope_id: "cust-1", dimension: "production_copy_count", value: 3,
        notes: null, asserted_by_user_id: "user-1", asserted_by_display_name: "Admin One", created_at: "2026-01-01T00:00:00Z",
      },
      {
        id: "attest-2", scope_type: "customer", scope_id: "cust-1", dimension: "media_type_diversity", value: ["disk", "tape"],
        notes: null, asserted_by_user_id: "user-1", asserted_by_display_name: "Admin One", created_at: "2026-01-01T00:00:00Z",
      },
    ]);
    const user = userEvent.setup();
    renderWithProviders(<CustomersPage />);

    await user.click(await screen.findByText("Acme AG"));
    const dialog = within(await screen.findByRole("dialog"));
    await waitFor(() => expect(dialog.getByText("disk, tape")).toBeInTheDocument());
  });
});

describe("CustomersPage — 3-2-1-1-0 leg applicability", () => {
  it("shows every leg as applicable (checked) by default", async () => {
    setup([ACTIVE_CUSTOMER]);
    const user = userEvent.setup();
    renderWithProviders(<CustomersPage />);

    await user.click(await screen.findByText("Acme AG"));
    const section = within(await screen.findByTestId("leg-applicability"));
    await waitFor(() => expect(section.getAllByRole("checkbox").length).toBe(5));
    expect(section.getAllByRole("checkbox").every((cb) => (cb as HTMLInputElement).checked)).toBe(true);
  });

  it("toggles a leg off through the checkbox", async () => {
    setup([ACTIVE_CUSTOMER]);
    vi.mocked(legApplicabilityApi.update).mockResolvedValue({
      leg: "offsite", applicable: false, updated_by_display_name: "Admin One", updated_at: "2026-01-01T00:00:00Z",
    });
    const user = userEvent.setup();
    renderWithProviders(<CustomersPage />);

    await user.click(await screen.findByText("Acme AG"));
    const section = within(await screen.findByTestId("leg-applicability"));
    await waitFor(() => expect(section.getAllByRole("checkbox").length).toBe(5));

    await user.click(section.getByLabelText("Offsite"));

    await waitFor(() => expect(legApplicabilityApi.update).toHaveBeenCalledWith("cust-1", "offsite", false));
  });
});

describe("CustomersPage — notification settings", () => {
  it("shows no email set by default for reports and billing (alarms have their own panel)", async () => {
    setup([ACTIVE_CUSTOMER]);
    renderWithProviders(<CustomersPage />);

    await userEvent.setup().click(await screen.findByText("Acme AG"));
    const section = within(await screen.findByTestId("notification-settings"));
    await waitFor(() => expect(section.getAllByText("No email set").length).toBe(2));
    expect(section.queryByText("Alarm notifications")).not.toBeInTheDocument();
    expect(await screen.findByTestId("alert-mailer")).toBeInTheDocument();
  });

  it("shows a configured recipient and its enabled status", async () => {
    setup([ACTIVE_CUSTOMER]);
    vi.mocked(notificationSettingsApi.get).mockResolvedValue({
      report_notification_email: "reports@example.com", report_notifications_enabled: true,
      alarm_notification_email: null, alarm_notifications_enabled: false,
      billing_notification_email: null, billing_notifications_enabled: false,
    });
    renderWithProviders(<CustomersPage />);

    await userEvent.setup().click(await screen.findByText("Acme AG"));
    const section = within(await screen.findByTestId("notification-settings"));
    await waitFor(() => expect(section.getByText(/reports@example.com/)).toBeInTheDocument());
  });

  it("saves reports and billing in one PUT without touching the alarm fields", async () => {
    setup([ACTIVE_CUSTOMER]);
    vi.mocked(notificationSettingsApi.update).mockResolvedValue({
      report_notification_email: "reports@example.com", report_notifications_enabled: true,
      alarm_notification_email: null, alarm_notifications_enabled: false,
      billing_notification_email: "billing@example.com", billing_notifications_enabled: false,
    });
    const user = userEvent.setup();
    renderWithProviders(<CustomersPage />);

    await user.click(await screen.findByText("Acme AG"));
    const section = within(await screen.findByTestId("notification-settings"));
    await waitFor(() => expect(section.getAllByText("No email set").length).toBe(2));
    await user.click(section.getByRole("button", { name: "Edit" }));

    await user.type(section.getByLabelText("Report notifications"), "reports@example.com");
    await user.type(section.getByLabelText("Billing notifications"), "billing@example.com");
    await user.click(section.getAllByRole("checkbox")[0]); // report enabled
    await user.click(section.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(notificationSettingsApi.update).toHaveBeenCalledWith("cust-1", {
        report_notification_email: "reports@example.com", report_notifications_enabled: true,
        billing_notification_email: "billing@example.com", billing_notifications_enabled: false,
      }),
    );
  });
});

describe("CustomersPage — create form", () => {
  it("derives a valid slug from a display name", () => {
    expect(slugFromName("Müller & Söhne AG")).toBe("mueller-soehne-ag");
    expect(slugFromName("  Grosse Strasse 5 ")).toBe("grosse-strasse-5");
    expect(slugFromName("Café Zürich")).toBe("cafe-zuerich");
  });

  it("fills the slug from the name until it is edited, and uppercases the match code", async () => {
    vi.mocked(customersApi.list).mockResolvedValue([]);
    vi.mocked(customersApi.create).mockResolvedValue(ACTIVE_CUSTOMER);
    const user = userEvent.setup();
    renderWithProviders(<CustomersPage />);

    await user.click(await screen.findByRole("button", { name: "Create" }));
    await user.type(screen.getByLabelText("Name"), "Müller AG");
    expect(screen.getByLabelText("Slug")).toHaveValue("mueller-ag");

    await user.clear(screen.getByLabelText("Slug"));
    await user.type(screen.getByLabelText("Slug"), "mag");
    await user.type(screen.getByLabelText("Name"), " Basel");
    expect(screen.getByLabelText("Slug")).toHaveValue("mag");

    await user.type(screen.getByLabelText("Debitor number"), "D-1");
    await user.type(screen.getByLabelText("Match code"), "mag");
    expect(screen.getByLabelText("Match code")).toHaveValue("MAG");

    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(customersApi.create).toHaveBeenCalledWith({ name: "Müller AG Basel", slug: "mag", debitor_number: "D-1", match_code: "MAG" }),
    );
  });
});
