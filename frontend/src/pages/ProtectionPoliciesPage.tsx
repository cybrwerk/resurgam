import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { customersApi, protectionPoliciesApi } from "../api/resources";
import type { ProtectionPolicyOut } from "../api/types";
import { ConfirmActionDialog } from "../components/ConfirmActionDialog";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { InlineAlert } from "../components/InlineAlert";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { QueryState } from "../components/QueryState";
import styles from "./ProtectionPoliciesPage.module.css";

const QUERY_KEY = ["protection-policies"];

function CreateProtectionPolicyForm({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const customersQuery = useQuery({ queryKey: ["customers"], queryFn: customersApi.list });

  const [customerId, setCustomerId] = useState("");
  const [name, setName] = useState("");
  const [expectedIntervalHours, setExpectedIntervalHours] = useState(24);
  const [maxTimeSinceLastBackupHours, setMaxTimeSinceLastBackupHours] = useState(48);
  const [expectedVerifyDeadlineHours, setExpectedVerifyDeadlineHours] = useState<string>("");
  const [minSnapshotCount, setMinSnapshotCount] = useState(1);
  const [syncDrRequired, setSyncDrRequired] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const createMutation = useMutation({
    mutationFn: () =>
      protectionPoliciesApi.create({
        customer_id: customerId,
        name,
        expected_interval_hours: expectedIntervalHours,
        max_time_since_last_backup_hours: maxTimeSinceLastBackupHours,
        expected_verify_deadline_hours: expectedVerifyDeadlineHours ? Number(expectedVerifyDeadlineHours) : null,
        min_snapshot_count: minSnapshotCount,
        sync_dr_required: syncDrRequired,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEY });
      onDone();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  return (
    <Panel title={t("protectionPolicies.createTitle")} className={styles.createPanel}>
      <form
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          setError(null);
          createMutation.mutate();
        }}
      >
        {error && (
          <div className={styles.alert}>
            <InlineAlert>{error}</InlineAlert>
          </div>
        )}
        <div className="formGrid">
          <div className="field">
            <label className="label" htmlFor="policy-customer">
              {t("protectionPolicies.customer")}
            </label>
            <select
              id="policy-customer"
              className="select"
              value={customerId}
              onChange={(e) => setCustomerId(e.target.value)}
              required
            >
              <option value="">—</option>
              {customersQuery.data?.map((customer) => (
                <option key={customer.id} value={customer.id}>
                  {customer.name}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label className="label" htmlFor="policy-name">
              {t("common.name")}
            </label>
            <input id="policy-name" className="input" value={name} onChange={(e) => setName(e.target.value)} required />
          </div>
          <div className="field">
            <label className="label" htmlFor="policy-interval">
              {t("protectionPolicies.expectedIntervalHours")}
            </label>
            <input
              id="policy-interval"
              type="number"
              min={1}
              className="input"
              value={expectedIntervalHours}
              onChange={(e) => setExpectedIntervalHours(Number(e.target.value))}
              required
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="policy-max-time">
              {t("protectionPolicies.maxTimeSinceLastBackupHours")}
            </label>
            <input
              id="policy-max-time"
              type="number"
              min={1}
              className="input"
              value={maxTimeSinceLastBackupHours}
              onChange={(e) => setMaxTimeSinceLastBackupHours(Number(e.target.value))}
              required
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="policy-verify-deadline">
              {t("protectionPolicies.expectedVerifyDeadlineHours")}
            </label>
            <input
              id="policy-verify-deadline"
              type="number"
              min={1}
              className="input"
              value={expectedVerifyDeadlineHours}
              onChange={(e) => setExpectedVerifyDeadlineHours(e.target.value)}
              placeholder={t("protectionPolicies.optional")}
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="policy-min-snapshots">
              {t("protectionPolicies.minSnapshotCount")}
            </label>
            <input
              id="policy-min-snapshots"
              type="number"
              min={0}
              className="input"
              value={minSnapshotCount}
              onChange={(e) => setMinSnapshotCount(Number(e.target.value))}
              required
            />
          </div>
          <div className="field">
            <span className="label" aria-hidden="true" />
            <label className="checkboxField" htmlFor="policy-sync-dr">
              <input
                id="policy-sync-dr"
                type="checkbox"
                checked={syncDrRequired}
                onChange={(e) => setSyncDrRequired(e.target.checked)}
              />{" "}
              {t("protectionPolicies.syncDrRequired")}
            </label>
          </div>
        </div>
        <div className="formActions">
          <button type="button" className="button" onClick={onDone}>
            {t("common.cancel")}
          </button>
          <button type="submit" className="button buttonPrimary" disabled={createMutation.isPending}>
            {t("common.create")}
          </button>
        </div>
      </form>
    </Panel>
  );
}

export function ProtectionPoliciesPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<ProtectionPolicyOut | null>(null);

  const query = useQuery({ queryKey: QUERY_KEY, queryFn: protectionPoliciesApi.list });
  const customersQuery = useQuery({ queryKey: ["customers"], queryFn: customersApi.list });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => protectionPoliciesApi.remove(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEY }),
  });

  const customerNameFor = (id: string) => customersQuery.data?.find((c) => c.id === id)?.name ?? id;

  const columns: DataTableColumn<ProtectionPolicyOut>[] = [
    { key: "name", header: t("common.name"), render: (row) => row.name },
    { key: "customer", header: t("protectionPolicies.customer"), render: (row) => customerNameFor(row.customer_id) },
    {
      key: "interval",
      header: t("protectionPolicies.expectedIntervalHours"),
      render: (row) => row.expected_interval_hours,
    },
    {
      key: "maxTime",
      header: t("protectionPolicies.maxTimeSinceLastBackupHours"),
      render: (row) => row.max_time_since_last_backup_hours,
    },
    { key: "minSnapshots", header: t("protectionPolicies.minSnapshotCount"), render: (row) => row.min_snapshot_count },
    {
      key: "actions",
      header: t("common.actions"),
      render: (row) => (
        <button type="button" className="button buttonDanger" onClick={() => setPendingDelete(row)}>
          {t("common.delete")}
        </button>
      ),
    },
  ];

  return (
    <div>
      <PageHeader
        title={t("protectionPolicies.title")}
        actions={
          !showCreateForm && (
            <button type="button" className="button buttonPrimary" onClick={() => setShowCreateForm(true)}>
              {t("common.create")}
            </button>
          )
        }
      />

      {showCreateForm && <CreateProtectionPolicyForm onDone={() => setShowCreateForm(false)} />}

      <QueryState query={query} isEmpty={(data) => data.length === 0}>
        {(policies) => <DataTable columns={columns} rows={policies} rowKey={(row) => row.id} />}
      </QueryState>

      <ConfirmActionDialog
        open={pendingDelete !== null}
        title={t("protectionPolicies.confirmDeleteTitle")}
        body={t("protectionPolicies.confirmDeleteBody", { name: pendingDelete?.name })}
        danger
        confirmLabel={t("common.delete")}
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          if (pendingDelete) deleteMutation.mutate(pendingDelete.id);
          setPendingDelete(null);
        }}
      />
    </div>
  );
}
