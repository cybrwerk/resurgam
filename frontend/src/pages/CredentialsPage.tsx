import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { credentialsApi } from "../api/resources";
import type { CredentialOut } from "../api/types";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { InlineAlert } from "../components/InlineAlert";
import { Modal } from "../components/Modal";
import { PageHeader } from "../components/PageHeader";
import { QueryState } from "../components/QueryState";
import styles from "./CredentialsPage.module.css";

function RotateCredentialModal({ credential, onClose }: { credential: CredentialOut; onClose: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [newValue, setNewValue] = useState("");
  const [error, setError] = useState<string | null>(null);

  const rotateMutation = useMutation({
    mutationFn: () => credentialsApi.rotate(credential.secret_reference_id, newValue),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["credentials"] });
      onClose();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  return (
    <Modal open title={t("credentials.rotateTitle", { owner: credential.owner_label })} onClose={onClose}>
      <p className={styles.hint}>{t("credentials.rotateHint", { owner: credential.owner_label })}</p>
      {error && (
        <div className={styles.alert}>
          <InlineAlert>{error}</InlineAlert>
        </div>
      )}
      <div className="field">
        <label className="label" htmlFor="credential-new-value">
          {t("credentials.newValue")}
        </label>
        <input
          id="credential-new-value"
          type="password"
          autoComplete="off"
          className="input"
          value={newValue}
          onChange={(e) => setNewValue(e.target.value)}
        />
      </div>
      <div className="formActions">
        <button type="button" className="button" onClick={onClose} disabled={rotateMutation.isPending}>
          {t("common.cancel")}
        </button>
        <button
          type="button"
          className="button buttonPrimary"
          onClick={() => rotateMutation.mutate()}
          disabled={rotateMutation.isPending || newValue.trim() === ""}
        >
          {t("credentials.rotate")}
        </button>
      </div>
    </Modal>
  );
}

/** Manifest §6.1/§6.2/§14, ADR 0005 -- a single place to see every PBS/PVE
 * API token's SecretReference (never the decrypted value) and rotate one
 * without editing the owning instance/cluster. Deliberately does NOT list
 * per-user MFA secrets, which live in the same underlying table -- the
 * backend excludes those by construction (`api/v1/credentials.py`), not
 * by filtering here.
 */
export function CredentialsPage() {
  const { t } = useTranslation();
  const [rotating, setRotating] = useState<CredentialOut | null>(null);

  const credentialsQuery = useQuery({ queryKey: ["credentials"], queryFn: credentialsApi.list });

  const columns: DataTableColumn<CredentialOut>[] = [
    { key: "owner", header: t("credentials.owner"), render: (r) => r.owner_label, sortValue: (r) => r.owner_label },
    {
      key: "type",
      header: t("common.type"),
      render: (r) => (r.owner_type === "pbs_instance" ? t("credentials.pbsInstance") : t("credentials.pveCluster")),
    },
    { key: "description", header: t("common.name"), render: (r) => r.description },
    { key: "createdAt", header: t("common.createdAt"), render: (r) => new Date(r.created_at).toLocaleString() },
    {
      key: "lastRotatedAt",
      header: t("credentials.lastRotatedAt"),
      render: (r) => new Date(r.last_rotated_at).toLocaleString(),
      sortValue: (r) => r.last_rotated_at,
    },
    {
      key: "actions",
      header: t("common.actions"),
      render: (r) => (
        <button type="button" className="button buttonGhost" onClick={() => setRotating(r)}>
          {t("credentials.rotate")}
        </button>
      ),
    },
  ];

  return (
    <div>
      <PageHeader title={t("nav.administrationCredentials")} description={t("credentials.description")} />

      <QueryState query={credentialsQuery} isEmpty={(data) => data.length === 0}>
        {(data) => <DataTable columns={columns} rows={data} rowKey={(r) => r.secret_reference_id} />}
      </QueryState>

      {rotating && <RotateCredentialModal credential={rotating} onClose={() => setRotating(null)} />}
    </div>
  );
}
