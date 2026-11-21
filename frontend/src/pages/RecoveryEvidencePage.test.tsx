import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { CustomerOut, RecoveryEvidenceReportOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { RecoveryEvidencePage } from "./RecoveryEvidencePage";

vi.mock("../api/resources", () => ({
  customersApi: { list: vi.fn() },
  reportingApi: {
    recoveryEvidence: vi.fn(),
    recoveryEvidencePdfPath: vi.fn(),
    recoveryEvidenceHistory: vi.fn(),
    recoveryEvidenceHistoryPdfPath: vi.fn(),
  },
}));

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, downloadFile: vi.fn() };
});

const { customersApi, reportingApi } = await import("../api/resources");
const { downloadFile } = await import("../api/client");

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

function report(overrides: Partial<RecoveryEvidenceReportOut> = {}): RecoveryEvidenceReportOut {
  return {
    customer_id: "cust-1",
    customer_name: "Acme AG",
    customer_match_code: "ACM",
    date_from: "2026-01-01",
    date_to: "2026-04-01",
    generated_at: "2026-04-01T00:00:00Z",
    generated_by_user_id: "user-1",
    backup_history: [],
    policy_adherence: [],
    verification_results: { summary: { ok: 0, failed: 0, none: 0, unknown: 0 }, entries: [] },
    failed_jobs: [],
    audit_trail: [],
    encryption_attestation: { instances: [] },
    restore_test_evidence: [],
    management_summary: {
      traffic_light: "green",
      backup_success_rate: 1,
      verification_pass_rate: 1,
      restore_test_pass_rate: null,
      policy_adherence_rate: 1,
      unresolved_failed_jobs: 0,
      four_eyes_approval_rate: null,
      period_timeline_svg: "<svg xmlns='http://www.w3.org/2000/svg'></svg>",
      kpi_bars_svg: "<svg xmlns='http://www.w3.org/2000/svg'></svg>",
    },
    access_control: [],
    pbs_datastore_access: [],
    protection_level: { tier: "backup_only", label: "Backup only", workloads: [], tier_distribution_svg: "<svg xmlns='http://www.w3.org/2000/svg'></svg>" },
    offsite_status: [],
    three_two_one_one_zero: [],
    framework: false,
    section_controls: {},
    ...overrides,
  };
}

function setup() {
  vi.mocked(customersApi.list).mockResolvedValue([CUSTOMER]);
  vi.mocked(reportingApi.recoveryEvidencePdfPath).mockReturnValue("/reporting/recovery-evidence/pdf?customer_id=cust-1");
  vi.mocked(reportingApi.recoveryEvidenceHistory).mockResolvedValue([]);
}

describe("RecoveryEvidencePage", () => {
  it("shows an empty state before a customer is selected", async () => {
    setup();
    renderWithProviders(<RecoveryEvidencePage />);
    await waitFor(() => expect(screen.getByText("Pick a tenant to generate a report.")).toBeInTheDocument());
    expect(reportingApi.recoveryEvidence).not.toHaveBeenCalled();
  });

  it("loads and renders the report once a customer is selected", async () => {
    setup();
    vi.mocked(reportingApi.recoveryEvidence).mockResolvedValue(
      report({
        backup_history: [
          {
            backup_group_id: "group-1", workload_label: "100", backup_type: "vm", namespace: "",
            snapshot_time: "2026-03-01T00:00:00Z", size_bytes: 1024, verify_status: "ok",
          },
        ],
      }),
    );
    const user = userEvent.setup();
    renderWithProviders(<RecoveryEvidencePage />);

    await waitFor(() => expect(screen.getByRole("option", { name: "Acme AG" })).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Tenant"), "cust-1");

    await waitFor(() => expect(reportingApi.recoveryEvidence).toHaveBeenCalledWith(
      expect.objectContaining({ customerId: "cust-1" }),
    ));
    await waitFor(() => expect(screen.getByText("100")).toBeInTheDocument());
    expect(screen.getByText("Backup History")).toBeInTheDocument();
    expect(screen.getByText("No Recovery Lab restore tests in this period.")).toBeInTheDocument();
    expect(screen.getByText("Nothing to do")).toBeInTheDocument(); // green traffic light
  });

  it("downloads the PDF with the current filters", async () => {
    setup();
    vi.mocked(reportingApi.recoveryEvidence).mockResolvedValue(report());
    const user = userEvent.setup();
    renderWithProviders(<RecoveryEvidencePage />);

    await waitFor(() => expect(screen.getByRole("option", { name: "Acme AG" })).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Tenant"), "cust-1");
    await waitFor(() => expect(reportingApi.recoveryEvidence).toHaveBeenCalled());

    await user.click(screen.getByRole("button", { name: "Download PDF" }));

    expect(reportingApi.recoveryEvidencePdfPath).toHaveBeenCalledWith(
      expect.objectContaining({ customerId: "cust-1" }),
    );
    expect(downloadFile).toHaveBeenCalledWith(
      "/reporting/recovery-evidence/pdf?customer_id=cust-1",
      "recovery-evidence-cust-1.pdf",
    );
  });

  it("shows that the PDF is being generated and reports a failure", async () => {
    setup();
    vi.mocked(reportingApi.recoveryEvidence).mockResolvedValue(report());
    let finish: () => void = () => {};
    vi.mocked(downloadFile).mockImplementationOnce(() => new Promise<void>((resolve) => (finish = resolve)));
    const user = userEvent.setup();
    renderWithProviders(<RecoveryEvidencePage />);

    await waitFor(() => expect(screen.getByRole("option", { name: "Acme AG" })).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Tenant"), "cust-1");
    await user.click(await screen.findByRole("button", { name: "Download PDF" }));

    const busy = await screen.findByRole("button", { name: /Generating PDF/ });
    expect(busy).toBeDisabled();
    expect(screen.getByTestId("pdf-generating")).toHaveTextContent("The report is being generated");
    finish();
    expect(await screen.findByRole("button", { name: "Download PDF" })).toBeEnabled();
    expect(screen.queryByTestId("pdf-generating")).not.toBeInTheDocument();

    const { ApiError } = await import("../api/client");
    vi.mocked(downloadFile).mockRejectedValueOnce(new ApiError(500, "pdf_failed", "WeasyPrint crashed."));
    await user.click(screen.getByRole("button", { name: "Download PDF" }));
    expect(await screen.findByText("WeasyPrint crashed.")).toBeInTheDocument();
  });

  it("fills the date range from the 'Last 7 days' preset", async () => {
    setup();
    vi.mocked(reportingApi.recoveryEvidence).mockResolvedValue(report());
    const user = userEvent.setup();
    renderWithProviders(<RecoveryEvidencePage />);

    await waitFor(() => expect(screen.getByRole("option", { name: "Acme AG" })).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Tenant"), "cust-1");

    await user.click(screen.getByRole("button", { name: "Last 7 days" }));

    const fromInput = screen.getByLabelText("From") as HTMLInputElement;
    const toInput = screen.getByLabelText("To") as HTMLInputElement;
    expect(fromInput.value).not.toBe("");
    expect(toInput.value).not.toBe("");
    const from = new Date(fromInput.value);
    const to = new Date(toInput.value);
    const diffDays = Math.round((to.getTime() - from.getTime()) / (1000 * 60 * 60 * 24));
    expect(diffDays).toBe(7);
  });

  it("fills the date range from the 'Last month' preset", async () => {
    setup();
    vi.mocked(reportingApi.recoveryEvidence).mockResolvedValue(report());
    const user = userEvent.setup();
    renderWithProviders(<RecoveryEvidencePage />);

    await waitFor(() => expect(screen.getByRole("option", { name: "Acme AG" })).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Tenant"), "cust-1");

    await user.click(screen.getByRole("button", { name: "Last month" }));

    const fromInput = screen.getByLabelText("From") as HTMLInputElement;
    const toInput = screen.getByLabelText("To") as HTMLInputElement;
    expect(fromInput.value).not.toBe("");
    expect(toInput.value).not.toBe(fromInput.value);

    await waitFor(() =>
      expect(reportingApi.recoveryEvidence).toHaveBeenCalledWith(
        expect.objectContaining({ customerId: "cust-1", dateFrom: fromInput.value, dateTo: toInput.value }),
      ),
    );
  });
});

describe("RecoveryEvidencePage — report history", () => {
  it("shows an empty state when no reports have been generated yet", async () => {
    setup();
    vi.mocked(reportingApi.recoveryEvidence).mockResolvedValue(report());
    const user = userEvent.setup();
    renderWithProviders(<RecoveryEvidencePage />);

    await waitFor(() => expect(screen.getByRole("option", { name: "Acme AG" })).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Tenant"), "cust-1");

    await waitFor(() => expect(screen.getByText("No reports have been generated for this tenant yet.")).toBeInTheDocument());
  });

  it("lists past report generations with a checksum and a re-download link", async () => {
    setup();
    vi.mocked(reportingApi.recoveryEvidence).mockResolvedValue(report());
    vi.mocked(reportingApi.recoveryEvidenceHistory).mockResolvedValue([
      {
        id: "hist-1", report_type: "recovery_evidence", customer_id: "cust-1", date_from: "2026-01-01", date_to: "2026-04-01",
        framework: false, generated_by_user_id: "user-1", sha256_checksum: "a".repeat(64), created_at: "2026-04-01T00:00:00Z",
      },
    ]);
    vi.mocked(reportingApi.recoveryEvidenceHistoryPdfPath).mockReturnValue("/reporting/recovery-evidence/history/hist-1/pdf");
    const user = userEvent.setup();
    renderWithProviders(<RecoveryEvidencePage />);

    await waitFor(() => expect(screen.getByRole("option", { name: "Acme AG" })).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Tenant"), "cust-1");

    await waitFor(() => expect(screen.getByText(/aaaaaaaaaaaa/)).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Re-download" }));

    expect(reportingApi.recoveryEvidenceHistoryPdfPath).toHaveBeenCalledWith("hist-1");
    expect(downloadFile).toHaveBeenCalledWith("/reporting/recovery-evidence/history/hist-1/pdf", "recovery-evidence-cust-1-2026-04-01.pdf");
  });
});
