import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { billingApi } from "../api/resources";
import type { CustomerBillingOut } from "../api/types";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { PageHeader } from "../components/PageHeader";
import { QueryState } from "../components/QueryState";
import { StatusBadge } from "../components/StatusBadge";
import { formatBytes } from "../lib/formatBytes";

export function BillingPage() {
  const { t } = useTranslation();
  const query = useQuery({ queryKey: ["billing-customers"], queryFn: billingApi.list });
  const exportMutation = useMutation({ mutationFn: () => billingApi.exportCsv() });

  const pricingPlanLabel = (row: CustomerBillingOut) => {
    if (row.pricing_plan_source === "customer") return t("billing.pricingPlanCustomer");
    if (row.pricing_plan_source === "global") return t("billing.pricingPlanGlobal");
    return t("billing.pricingPlanNone");
  };

  const columns: DataTableColumn<CustomerBillingOut>[] = [
    {
      key: "customer",
      header: t("common.name"),
      render: (row) => `${row.customer_name} (${row.customer_match_code})`,
    },
    { key: "vmCount", header: t("billing.vmCount"), render: (row) => row.vm_count },
    { key: "ctCount", header: t("billing.ctCount"), render: (row) => row.ct_count },
    { key: "hostCount", header: t("billing.hostCount"), render: (row) => row.host_count },
    {
      key: "bytes",
      header: t("billing.estimatedBytes"),
      render: (row) => (
        <span>
          {formatBytes(row.estimated_physical_bytes)}
          {!row.physical_bytes_estimate_complete && (
            <>
              {" "}
              <StatusBadge tone="warning" label={t("billing.estimateIncomplete")} />
            </>
          )}
          {row.estimated_s3_bytes > 0 && (
            <span className="mutedText" style={{ display: "block" }}>
              {t(
                row.pricing_plan_id !== null && row.price_per_s3_gb_month === null
                  ? "billing.s3PartNotCharged"
                  : "billing.s3Part",
                { size: formatBytes(row.estimated_s3_bytes) },
              )}
            </span>
          )}
        </span>
      ),
    },
    { key: "plan", header: t("billing.pricingPlan"), render: (row) => pricingPlanLabel(row) },
    {
      key: "cost",
      header: t("billing.estimatedMonthlyCost"),
      render: (row) =>
        row.estimated_monthly_cost === null ? "—" : `${row.estimated_monthly_cost} ${row.currency}`,
    },
  ];

  return (
    <div>
      <PageHeader
        title={t("nav.reportingTenantUsage")}
        actions={
          <button type="button" className="button" onClick={() => exportMutation.mutate()} disabled={exportMutation.isPending}>
            {t("billing.exportCsv")}
          </button>
        }
      />
      <QueryState query={query} isEmpty={(data) => data.length === 0}>
        {(rows) => <DataTable columns={columns} rows={rows} rowKey={(row) => row.customer_id} />}
      </QueryState>
    </div>
  );
}
