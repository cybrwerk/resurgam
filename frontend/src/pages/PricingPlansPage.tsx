import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { customersApi, pricingPlansApi } from "../api/resources";
import type { PricingPlanOut } from "../api/types";
import { ConfirmActionDialog } from "../components/ConfirmActionDialog";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { InlineAlert } from "../components/InlineAlert";
import { Modal } from "../components/Modal";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { QueryState } from "../components/QueryState";
import styles from "./PricingPlansPage.module.css";

const QUERY_KEY = ["pricing-plans"];

function CreatePricingPlanForm({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const customersQuery = useQuery({ queryKey: ["customers"], queryFn: customersApi.list });

  const [scope, setScope] = useState<"global" | "customer">("global");
  const [customerId, setCustomerId] = useState("");
  const [name, setName] = useState("");
  const [pricePerVm, setPricePerVm] = useState("");
  const [pricePerCt, setPricePerCt] = useState("0");
  const [pricePerHost, setPricePerHost] = useState("0");
  const [pricePerGb, setPricePerGb] = useState("");
  const [currency, setCurrency] = useState("CHF");
  const [error, setError] = useState<string | null>(null);

  const createMutation = useMutation({
    mutationFn: () =>
      pricingPlansApi.create({
        customer_id: scope === "customer" ? customerId : null,
        name,
        price_per_vm_month: pricePerVm,
        price_per_ct_month: pricePerCt,
        price_per_host_month: pricePerHost,
        price_per_gb_month: pricePerGb,
        currency,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEY });
      onDone();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  return (
    <Panel title={t("pricingPlans.createTitle")} className={styles.createPanel}>
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
            <label className="label" htmlFor="plan-scope">
              {t("pricingPlans.scope")}
            </label>
            <select
              id="plan-scope"
              className="select"
              value={scope}
              onChange={(e) => setScope(e.target.value as "global" | "customer")}
            >
              <option value="global">{t("pricingPlans.scopeGlobal")}</option>
              <option value="customer">{t("pricingPlans.scopeCustomer")}</option>
            </select>
          </div>
          {scope === "customer" && (
            <div className="field">
              <label className="label" htmlFor="plan-customer">
                {t("protectionPolicies.customer")}
              </label>
              <select id="plan-customer" className="select" value={customerId} onChange={(e) => setCustomerId(e.target.value)} required>
                <option value="">—</option>
                {customersQuery.data?.map((customer) => (
                  <option key={customer.id} value={customer.id}>
                    {customer.name}
                  </option>
                ))}
              </select>
            </div>
          )}
          <div className="field">
            <label className="label" htmlFor="plan-name">
              {t("common.name")}
            </label>
            <input id="plan-name" className="input" value={name} onChange={(e) => setName(e.target.value)} required />
          </div>
          <div className="field">
            <label className="label" htmlFor="plan-price-vm">
              {t("pricingPlans.pricePerVm")}
            </label>
            <input
              id="plan-price-vm"
              type="number"
              step="0.01"
              min={0}
              className="input"
              value={pricePerVm}
              onChange={(e) => setPricePerVm(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="plan-price-ct">
              {t("pricingPlans.pricePerCt")}
            </label>
            <input
              id="plan-price-ct"
              type="number"
              step="0.01"
              min={0}
              className="input"
              value={pricePerCt}
              onChange={(e) => setPricePerCt(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="plan-price-host">
              {t("pricingPlans.pricePerHost")}
            </label>
            <input
              id="plan-price-host"
              type="number"
              step="0.01"
              min={0}
              className="input"
              value={pricePerHost}
              onChange={(e) => setPricePerHost(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="plan-price-gb">
              {t("pricingPlans.pricePerGb")}
            </label>
            <input
              id="plan-price-gb"
              type="number"
              step="0.0001"
              min={0}
              className="input"
              value={pricePerGb}
              onChange={(e) => setPricePerGb(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <label className="label" htmlFor="plan-currency">
              {t("pricingPlans.currency")}
            </label>
            <input
              id="plan-currency"
              className="input"
              maxLength={3}
              value={currency}
              onChange={(e) => setCurrency(e.target.value.toUpperCase())}
              required
            />
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

function EditPricingPlanModal({ plan, onClose }: { plan: PricingPlanOut; onClose: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const [name, setName] = useState(plan.name);
  const [pricePerVm, setPricePerVm] = useState(plan.price_per_vm_month);
  const [pricePerCt, setPricePerCt] = useState(plan.price_per_ct_month);
  const [pricePerHost, setPricePerHost] = useState(plan.price_per_host_month);
  const [pricePerGb, setPricePerGb] = useState(plan.price_per_gb_month);
  const [currency, setCurrency] = useState(plan.currency);
  const [error, setError] = useState<string | null>(null);

  const updateMutation = useMutation({
    mutationFn: () =>
      pricingPlansApi.update(plan.id, {
        name,
        price_per_vm_month: pricePerVm,
        price_per_ct_month: pricePerCt,
        price_per_host_month: pricePerHost,
        price_per_gb_month: pricePerGb,
        currency,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEY });
      onClose();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  return (
    <Modal open title={t("pricingPlans.editTitle", { name: plan.name })} onClose={onClose}>
      <form
        className={styles.modalForm}
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          setError(null);
          updateMutation.mutate();
        }}
      >
        {error && (
          <div className={`${styles.alert} ${styles.fullRow}`}>
            <InlineAlert>{error}</InlineAlert>
          </div>
        )}
        <div className="field">
          <label className="label" htmlFor="edit-plan-name">
            {t("common.name")}
          </label>
          <input
            id="edit-plan-name"
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
          />
        </div>
        <div className="field">
          <label className="label" htmlFor="edit-plan-price-vm">
            {t("pricingPlans.pricePerVm")}
          </label>
          <input
            id="edit-plan-price-vm"
            type="number"
            step="0.01"
            min={0}
            className="input"
            value={pricePerVm}
            onChange={(e) => setPricePerVm(e.target.value)}
            required
          />
        </div>
        <div className="field">
          <label className="label" htmlFor="edit-plan-price-ct">
            {t("pricingPlans.pricePerCt")}
          </label>
          <input
            id="edit-plan-price-ct"
            type="number"
            step="0.01"
            min={0}
            className="input"
            value={pricePerCt}
            onChange={(e) => setPricePerCt(e.target.value)}
            required
          />
        </div>
        <div className="field">
          <label className="label" htmlFor="edit-plan-price-host">
            {t("pricingPlans.pricePerHost")}
          </label>
          <input
            id="edit-plan-price-host"
            type="number"
            step="0.01"
            min={0}
            className="input"
            value={pricePerHost}
            onChange={(e) => setPricePerHost(e.target.value)}
            required
          />
        </div>
        <div className="field">
          <label className="label" htmlFor="edit-plan-price-gb">
            {t("pricingPlans.pricePerGb")}
          </label>
          <input
            id="edit-plan-price-gb"
            type="number"
            step="0.0001"
            min={0}
            className="input"
            value={pricePerGb}
            onChange={(e) => setPricePerGb(e.target.value)}
            required
          />
        </div>
        <div className="field">
          <label className="label" htmlFor="edit-plan-currency">
            {t("pricingPlans.currency")}
          </label>
          <input
            id="edit-plan-currency"
            className="input"
            maxLength={3}
            value={currency}
            onChange={(e) => setCurrency(e.target.value.toUpperCase())}
            required
          />
        </div>
        <div className="formActions">
          <button type="button" className="button" onClick={onClose} disabled={updateMutation.isPending}>
            {t("common.cancel")}
          </button>
          <button type="submit" className="button buttonPrimary" disabled={updateMutation.isPending}>
            {t("common.save")}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function PricingPlansPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<PricingPlanOut | null>(null);
  const [editingPlan, setEditingPlan] = useState<PricingPlanOut | null>(null);

  const query = useQuery({ queryKey: QUERY_KEY, queryFn: pricingPlansApi.list });
  const customersQuery = useQuery({ queryKey: ["customers"], queryFn: customersApi.list });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => pricingPlansApi.remove(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEY }),
  });

  const scopeLabelFor = (row: PricingPlanOut) =>
    row.customer_id
      ? (customersQuery.data?.find((c) => c.id === row.customer_id)?.name ?? row.customer_id)
      : t("pricingPlans.scopeGlobal");

  const columns: DataTableColumn<PricingPlanOut>[] = [
    { key: "name", header: t("common.name"), render: (row) => row.name },
    { key: "scope", header: t("pricingPlans.scope"), render: (row) => scopeLabelFor(row) },
    {
      key: "priceVm",
      header: t("pricingPlans.pricePerVm"),
      render: (row) => `${row.price_per_vm_month} ${row.currency}`,
    },
    {
      key: "priceCt",
      header: t("pricingPlans.pricePerCt"),
      render: (row) => `${row.price_per_ct_month} ${row.currency}`,
    },
    {
      key: "priceHost",
      header: t("pricingPlans.pricePerHost"),
      render: (row) => `${row.price_per_host_month} ${row.currency}`,
    },
    {
      key: "priceGb",
      header: t("pricingPlans.pricePerGb"),
      render: (row) => `${row.price_per_gb_month} ${row.currency}`,
    },
    {
      key: "actions",
      header: t("common.actions"),
      render: (row) => (
        <div className={styles.rowActions}>
          <button type="button" className="button" onClick={() => setEditingPlan(row)}>
            {t("common.edit")}
          </button>
          <button type="button" className="button buttonDanger" onClick={() => setPendingDelete(row)}>
            {t("common.delete")}
          </button>
        </div>
      ),
    },
  ];

  return (
    <div>
      <PageHeader
        title={t("pricingPlans.title")}
        actions={
          !showCreateForm && (
            <button type="button" className="button buttonPrimary" onClick={() => setShowCreateForm(true)}>
              {t("common.create")}
            </button>
          )
        }
      />

      {showCreateForm && <CreatePricingPlanForm onDone={() => setShowCreateForm(false)} />}

      <QueryState query={query} isEmpty={(data) => data.length === 0}>
        {(plans) => <DataTable columns={columns} rows={plans} rowKey={(row) => row.id} />}
      </QueryState>

      {editingPlan && <EditPricingPlanModal plan={editingPlan} onClose={() => setEditingPlan(null)} />}

      <ConfirmActionDialog
        open={pendingDelete !== null}
        title={t("pricingPlans.confirmDeleteTitle")}
        body={t("pricingPlans.confirmDeleteBody", { name: pendingDelete?.name })}
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
