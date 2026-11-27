import { useQueries, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { customersApi, datastoresApi } from "../api/resources";
import type { CapacityForecastOut, DatastoreOut, DatastoreUsageOut, S3TrafficOut } from "../api/types";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { PageHeader } from "../components/PageHeader";
import { QueryState } from "../components/QueryState";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { formatBytes } from "../lib/formatBytes";

function usagePercentTone(percent: number): StatusTone {
  if (percent >= 95) return "danger";
  if (percent >= 80) return "warning";
  return "ok";
}

function formatChange(bytes: number | null): string {
  if (bytes === null) return "—";
  const sign = bytes > 0 ? "+" : "";
  return `${sign}${formatBytes(bytes)}`;
}

// Same thresholds as the backend's `datastore_full_soon_*` alarms
// (inventory/usage.py FORECAST_CRITICAL_DAYS / FORECAST_WARNING_DAYS).
function fullInTone(days: number): StatusTone {
  if (days <= 14) return "danger";
  if (days <= 30) return "warning";
  return "ok";
}

function totalRequests(traffic: S3TrafficOut): number {
  return (
    traffic.s3_get_requests +
    traffic.s3_put_requests +
    traffic.s3_post_requests +
    traffic.s3_head_requests +
    traffic.s3_delete_requests
  );
}

interface Row {
  datastore: DatastoreOut;
  usage: DatastoreUsageOut | undefined;
  isLoading: boolean;
}

export function CapacityTrendsPage() {
  const { t } = useTranslation();

  const renderFullIn = (forecast: CapacityForecastOut | undefined) => {
    if (!forecast) return "—";
    if (forecast.status === "not_applicable") {
      return <span className="mutedText">{t("capacityTrends.s3NoForecast")}</span>;
    }
    if (forecast.status === "insufficient_history") {
      return <span className="mutedText">{t("capacityTrends.insufficientHistory", { count: forecast.basis_days })}</span>;
    }
    if (forecast.status === "not_growing" || forecast.days_until_full === null) {
      return <span className="mutedText">{t("capacityTrends.notGrowing")}</span>;
    }
    const days = forecast.days_until_full;
    const label =
      days < 1
        ? t("capacityTrends.fullNow")
        : days > 365
          ? t("capacityTrends.fullInOverYear")
          : t("capacityTrends.fullInDays", { count: Math.round(days) });
    const hint = t("capacityTrends.forecastHint", {
      days: forecast.basis_days,
      date: forecast.full_at ? new Date(forecast.full_at).toLocaleDateString() : "—",
    });
    return (
      <span title={hint}>
        <StatusBadge tone={fullInTone(days)} label={label} />
      </span>
    );
  };

  const datastoresQuery = useQuery({ queryKey: ["datastores"], queryFn: datastoresApi.list });
  const customersQuery = useQuery({ queryKey: ["customers"], queryFn: customersApi.list });

  const usageQueries = useQueries({
    queries: (datastoresQuery.data ?? []).map((datastore) => ({
      queryKey: ["datastore-usage", datastore.id],
      queryFn: () => datastoresApi.usage(datastore.id),
    })),
  });

  const customerNameFor = (id: string | null) => {
    if (!id) return t("datastores.unassigned");
    return customersQuery.data?.find((c) => c.id === id)?.name ?? id;
  };

  const rows: Row[] = (datastoresQuery.data ?? []).map((datastore, index) => ({
    datastore,
    usage: usageQueries[index]?.data,
    isLoading: usageQueries[index]?.isLoading ?? false,
  }));

  const columns: DataTableColumn<Row>[] = [
    {
      key: "name",
      header: t("common.name"),
      render: (row) =>
        row.datastore.backend_type === "s3" ? (
          <>
            <div>
              {row.datastore.name} <StatusBadge tone="info" label="S3" />
            </div>
            <div className="mutedText">
              {row.datastore.s3_bucket ?? "?"} @ {row.datastore.s3_endpoint_host ?? row.datastore.s3_endpoint_name ?? "?"}
            </div>
          </>
        ) : (
          row.datastore.name
        ),
    },
    { key: "customer", header: t("datastores.customer"), render: (row) => customerNameFor(row.datastore.customer_id) },
    {
      key: "usage",
      header: t("capacityTrends.usage"),
      render: (row) => {
        if (row.isLoading) return "…";
        const latest = row.usage?.latest;
        if (!latest) return <span className="mutedText">{t("capacityTrends.noData")}</span>;
        if (row.usage?.backend_type === "s3") {
          const traffic = row.usage.s3_traffic_30d;
          return (
            <>
              <div>
                {latest.s3_data_bytes != null
                  ? t("capacityTrends.s3InBucket", { size: formatBytes(latest.s3_data_bytes) })
                  : t("capacityTrends.s3SizeAfterGc")}
              </div>
              <div title={t("capacityTrends.s3CacheHint")}>
                <StatusBadge
                  tone={usagePercentTone(latest.usage_percent)}
                  label={t("capacityTrends.s3Cache", {
                    percent: latest.usage_percent,
                    used: formatBytes(latest.used_bytes),
                    total: formatBytes(latest.total_bytes),
                  })}
                />
              </div>
              {traffic && (
                <div className="mutedText" title={t("capacityTrends.s3TrafficHint")}>
                  {t("capacityTrends.s3Traffic", {
                    up: formatBytes(traffic.s3_uploaded_bytes),
                    down: formatBytes(traffic.s3_downloaded_bytes),
                    requests: totalRequests(traffic).toLocaleString(),
                  })}
                </div>
              )}
            </>
          );
        }
        return (
          <StatusBadge
            tone={usagePercentTone(latest.usage_percent)}
            label={`${latest.usage_percent}% (${formatBytes(latest.used_bytes)} / ${formatBytes(latest.total_bytes)})`}
          />
        );
      },
    },
    {
      key: "logical",
      header: t("capacityTrends.logicalSize"),
      render: (row) => {
        const latest = row.usage?.latest;
        // S3 (ADR 0033): the physical side is the bucket, not the cache disk.
        const physical = row.usage?.backend_type === "s3" ? latest?.s3_data_bytes : latest?.used_bytes;
        if (!latest || latest.logical_bytes === 0 || !physical) return "—";
        const ratio = latest.logical_bytes / physical;
        return `${formatBytes(latest.logical_bytes)} (${ratio.toFixed(1)}x)`;
      },
    },
    {
      key: "groups",
      header: t("capacityTrends.groups"),
      render: (row) => {
        const latest = row.usage?.latest;
        if (!latest) return "—";
        return `${latest.group_count_vm} VM · ${latest.group_count_ct} CT · ${latest.group_count_host} Host`;
      },
    },
    {
      key: "snapshots",
      header: t("capacityTrends.snapshots"),
      render: (row) => row.usage?.latest?.snapshot_count ?? "—",
    },
    {
      key: "dailyChange",
      header: t("capacityTrends.dailyChange"),
      render: (row) => formatChange(row.usage?.daily_change_bytes ?? null),
    },
    {
      key: "monthlyChange",
      header: t("capacityTrends.monthlyChange"),
      render: (row) => formatChange(row.usage?.monthly_change_bytes ?? null),
    },
    {
      key: "growthPerDay",
      header: t("capacityTrends.growthPerDay"),
      render: (row) => formatChange(row.usage?.forecast.growth_bytes_per_day ?? null),
    },
    {
      key: "fullIn",
      header: t("capacityTrends.fullIn"),
      render: (row) => (row.isLoading ? "…" : renderFullIn(row.usage?.forecast)),
    },
    {
      key: "lastGc",
      header: t("capacityTrends.lastGc"),
      render: (row) => {
        const gc = row.usage?.latest?.last_successful_gc_at;
        return gc ? new Date(gc).toLocaleString() : "—";
      },
    },
  ];

  return (
    <div>
      <PageHeader title={t("capacityTrends.title")} />
      <QueryState query={datastoresQuery} isEmpty={(data) => data.length === 0}>
        {() => <DataTable columns={columns} rows={rows} rowKey={(row) => row.datastore.id} />}
      </QueryState>
    </div>
  );
}
