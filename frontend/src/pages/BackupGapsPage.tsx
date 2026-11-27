import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { backupGapsApi, s3EndpointsApi } from "../api/resources";
import type {
  BackupGapsOut,
  BackupGapThresholds,
  CreateGapExceptionRequest,
  GapExceptionOut,
  GuestGapOut,
  OffsiteGapOut,
  OffsiteStatus,
  S3EndpointOut,
} from "../api/types";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { InlineAlert } from "../components/InlineAlert";
import { Modal } from "../components/Modal";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { QueryState } from "../components/QueryState";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { TabPanel, Tabs, useTabParam } from "../components/Tabs";
import { useAuth } from "../contexts/AuthContext";
import styles from "./BackupGapsPage.module.css";

const QUERY_KEY = ["backup-gaps"];

function formatDateTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

function offsiteTone(entry: OffsiteGapOut): StatusTone {
  if (entry.severity === "critical") return "danger";
  if (entry.severity === "warning") return "warning";
  if (entry.status === "ok" || entry.status === "stored_offsite") return "ok";
  if (entry.status === "excepted") return "info";
  return "unknown";
}

function formatLag(hours: number | null): string {
  if (hours === null) return "—";
  if (hours < 1) return "< 1 h";
  if (hours < 48) return `${Math.round(hours)} h`;
  return `${Math.round(hours / 24)} d`;
}

/** What an exception would cover: the narrow target and the wide one. */
interface ExceptionTarget {
  label: string;
  narrow: CreateGapExceptionRequest;
  narrowLabel: string;
  wide: CreateGapExceptionRequest;
  wideLabel: string;
  current: GapExceptionOut | null;
}

function offsiteTarget(entry: OffsiteGapOut, t: TFunction): ExceptionTarget {
  return {
    label: entry.workload_label,
    narrow: {
      target_kind: "backup_group",
      datastore_id: entry.datastore_id,
      namespace: entry.namespace,
      backup_type: entry.backup_type,
      backup_id: entry.backup_id,
      reason: "",
    },
    narrowLabel: t("backupGaps.scopeWorkload", { name: entry.workload_label }),
    wide: { target_kind: "datastore", datastore_id: entry.datastore_id, reason: "" },
    wideLabel: t("backupGaps.scopeDatastore", { name: `${entry.pbs_instance_name} / ${entry.datastore_name}` }),
    current: entry.exception,
  };
}

function guestTarget(guest: GuestGapOut, t: TFunction): ExceptionTarget {
  const name = guest.name ? `${guest.vmid} (${guest.name})` : String(guest.vmid);
  return {
    label: name,
    narrow: { target_kind: "pve_guest", pve_cluster_id: guest.pve_cluster_id, vmid: guest.vmid, reason: "" },
    narrowLabel: t("backupGaps.scopeGuest", { name }),
    wide: { target_kind: "pve_cluster", pve_cluster_id: guest.pve_cluster_id, reason: "" },
    wideLabel: t("backupGaps.scopeCluster", { name: guest.pve_cluster_name }),
    current: guest.exception,
  };
}

function ExceptionDialog({ target, onClose }: { target: ExceptionTarget; onClose: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const revoking = target.current !== null;
  const [wide, setWide] = useState(false);
  const [reason, setReason] = useState("");
  const [expires, setExpires] = useState("");
  const [error, setError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () => {
      if (revoking && target.current) {
        const current = target.current;
        return backupGapsApi.createException({
          target_kind: current.target_kind,
          pve_cluster_id: current.pve_cluster_id,
          vmid: current.vmid,
          datastore_id: current.datastore_id,
          namespace: current.namespace,
          backup_type: current.backup_type,
          backup_id: current.backup_id,
          active: false,
          reason: reason.trim(),
        });
      }
      const base = wide ? target.wide : target.narrow;
      return backupGapsApi.createException({
        ...base,
        reason: reason.trim(),
        expires_at: expires ? new Date(`${expires}T23:59:59`).toISOString() : null,
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEY });
      queryClient.invalidateQueries({ queryKey: ["backup-gap-exceptions"] });
      queryClient.invalidateQueries({ queryKey: ["alarms"] });
      onClose();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });

  return (
    <Modal open title={revoking ? t("backupGaps.revokeTitle") : t("backupGaps.acceptTitle")} onClose={onClose}>
      {revoking && target.current ? (
        <p className="panelIntro">
          {t("backupGaps.revokeIntro", { target: target.current.target_label, reason: target.current.reason })}
        </p>
      ) : (
        <>
          <p className="panelIntro">{t("backupGaps.acceptIntro")}</p>
          <fieldset className={styles.scope}>
            <legend className="label">{t("backupGaps.scope")}</legend>
            <label className="checkboxField">
              <input type="radio" name="gap-scope" checked={!wide} onChange={() => setWide(false)} /> {target.narrowLabel}
            </label>
            <label className="checkboxField">
              <input type="radio" name="gap-scope" checked={wide} onChange={() => setWide(true)} /> {target.wideLabel}
            </label>
          </fieldset>
        </>
      )}
      {error && <InlineAlert>{error}</InlineAlert>}
      <div className="field">
        <label className="label" htmlFor="gap-reason">
          {t("backupGaps.reason")}
        </label>
        <textarea
          id="gap-reason"
          className="input"
          rows={3}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder={revoking ? "" : t("backupGaps.reasonPlaceholder")}
        />
        <p className="mutedText">{t("backupGaps.reasonHint")}</p>
      </div>
      {!revoking && (
        <div className="field">
          <label className="label" htmlFor="gap-expires">
            {t("backupGaps.expires")}
          </label>
          <input id="gap-expires" type="date" className="input" value={expires} onChange={(e) => setExpires(e.target.value)} />
        </div>
      )}
      <div className="formActions">
        <button type="button" className="button" onClick={onClose}>
          {t("common.cancel")}
        </button>
        <button
          type="button"
          className={revoking ? "button buttonDanger" : "button buttonPrimary"}
          disabled={reason.trim().length < 10 || mutation.isPending}
          onClick={() => mutation.mutate()}
        >
          {revoking ? t("backupGaps.revoke") : t("backupGaps.accept")}
        </button>
      </div>
    </Modal>
  );
}

function ExceptionNote({ exception }: { exception: GapExceptionOut | null }) {
  const { t } = useTranslation();
  if (!exception) return null;
  return (
    <div className={`mutedText ${styles.note}`} title={exception.reason}>
      {t("backupGaps.acceptedBy", {
        name: exception.created_by_name ?? "?",
        date: new Date(exception.created_at).toLocaleDateString(),
      })}
      : {exception.reason}
    </div>
  );
}

function OffsiteSection({ data, isAdmin, onException }: { data: BackupGapsOut; isAdmin: boolean; onException: (t: ExceptionTarget) => void }) {
  const { t } = useTranslation();
  const [onlyGaps, setOnlyGaps] = useState(true);
  const rows = onlyGaps ? data.offsite.filter((o) => o.status !== "ok" && o.status !== "stored_offsite" && o.status !== "not_required") : data.offsite;

  const statusLabel = (status: OffsiteStatus) => t(`backupGaps.status.${status}`);
  const columns: DataTableColumn<OffsiteGapOut>[] = [
    {
      key: "workload",
      header: t("backupGaps.workload"),
      render: (r) => (
        <>
          <div>{r.workload_label}</div>
          <div className="mutedText">
            {r.pbs_instance_name} / {r.datastore_name}
            {r.namespace ? ` / ${r.namespace}` : ""}
          </div>
        </>
      ),
    },
    { key: "customer", header: t("backupGaps.customer"), render: (r) => r.customer_name ?? "—" },
    {
      key: "status",
      header: t("common.status"),
      render: (r) => (
        <>
          <span title={t(`backupGaps.statusHint.${r.raw_status}`)}>
            <StatusBadge tone={offsiteTone(r)} label={statusLabel(r.status)} />
          </span>
          {r.status === "excepted" && <div className="mutedText">{statusLabel(r.raw_status)}</div>}
          <ExceptionNote exception={r.exception} />
        </>
      ),
    },
    {
      key: "copy",
      header: t("backupGaps.copyAt"),
      render: (r) => (
        <span title={r.sync_jobs.join("\n") || undefined}>{r.copy_location ?? (r.sync_jobs.length ? "—" : t("backupGaps.noSyncJob"))}</span>
      ),
    },
    { key: "latest", header: t("backupGaps.latestBackup"), render: (r) => formatDateTime(r.latest_backup_at) },
    { key: "latestCopy", header: t("backupGaps.latestCopy"), render: (r) => formatDateTime(r.latest_copy_at) },
    {
      key: "lag",
      header: t("backupGaps.lag"),
      render: (r) =>
        r.lag_hours === null ? (
          "—"
        ) : (
          <span title={t("backupGaps.lagHint", { count: r.uncopied_count, since: formatDateTime(r.oldest_uncopied_at) })}>
            {formatLag(r.lag_hours)}
          </span>
        ),
    },
  ];
  if (isAdmin) {
    columns.push({
      key: "actions",
      header: "",
      render: (r) =>
        r.exception ? (
          <button type="button" className="button buttonGhost" onClick={() => onException(offsiteTarget(r, t))}>
            {t("backupGaps.revoke")}
          </button>
        ) : r.severity !== null || r.status === "no_copy_job" ? (
          <button type="button" className="button buttonGhost" onClick={() => onException(offsiteTarget(r, t))}>
            {t("backupGaps.accept")}
          </button>
        ) : null,
    });
  }

  return (
    <Panel testId="backup-gaps-offsite" padded={false}>
      <div className={`toolbar ${styles.panelToolbar}`}>
        <label className="checkboxField">
          <input type="checkbox" checked={onlyGaps} onChange={(e) => setOnlyGaps(e.target.checked)} /> {t("backupGaps.onlyGaps")}
        </label>
        <span className="toolbarSpacer" />
        <span className="mutedText">
          {t("backupGaps.thresholdsNote", {
            warning: data.thresholds.offsite_lag_warning_hours,
            critical: data.thresholds.offsite_lag_critical_hours,
          })}
        </span>
      </div>
      {rows.length === 0 ? (
        <p className={`mutedText ${styles.empty}`}>{data.offsite.length === 0 ? t("backupGaps.noWorkloads") : t("backupGaps.noOffsiteGaps")}</p>
      ) : (
        <DataTable columns={columns} rows={rows} rowKey={(r) => r.backup_group_id} />
      )}
    </Panel>
  );
}

function GuestsSection({ data, onException }: { data: BackupGapsOut; onException: (t: ExceptionTarget) => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const check = useMutation({
    mutationFn: (clusterId: string) => backupGapsApi.checkCluster(clusterId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: QUERY_KEY }),
  });

  const columns: DataTableColumn<GuestGapOut>[] = [
    { key: "cluster", header: t("backupGaps.cluster"), render: (r) => r.pve_cluster_name },
    { key: "vmid", header: "VMID", render: (r) => r.vmid },
    { key: "name", header: t("backupGaps.guestName"), render: (r) => r.name ?? "—" },
    { key: "type", header: t("backupGaps.guestType"), render: (r) => (r.guest_type === "lxc" ? "CT" : "VM") },
    {
      key: "reason",
      header: t("common.status"),
      render: (r) => (
        <>
          <StatusBadge
            tone={r.exception ? "info" : "warning"}
            label={r.exception ? t("backupGaps.status.excepted") : t(`backupGaps.guestReason.${r.reason}`)}
          />
          {r.exception && <div className="mutedText">{t(`backupGaps.guestReason.${r.reason}`)}</div>}
          <ExceptionNote exception={r.exception} />
        </>
      ),
    },
    { key: "since", header: t("backupGaps.unprotectedSince"), render: (r) => formatDateTime(r.first_seen_at) },
    {
      key: "actions",
      header: "",
      render: (r) => (
        <button type="button" className="button buttonGhost" onClick={() => onException(guestTarget(r, t))}>
          {r.exception ? t("backupGaps.revoke") : t("backupGaps.accept")}
        </button>
      ),
    },
  ];

  return (
    <>
      <Panel testId="backup-gaps-clusters" title={t("backupGaps.clusterChecks")}>
        <p className="panelIntro">{t("backupGaps.guestsIntro")}</p>
        {data.pve_clusters.length === 0 ? (
          <p className="mutedText">{t("backupGaps.noClusters")}</p>
        ) : (
          <dl className="detailGrid">
            {data.pve_clusters.map((c) => (
              <div key={c.pve_cluster_id} className={styles.clusterRow}>
                <dt>{c.pve_cluster_name}</dt>
                <dd>
                  {c.error ? (
                    <StatusBadge tone="danger" label={t("backupGaps.checkFailed")} />
                  ) : (
                    <span className="mutedText">{t("backupGaps.checkedAt", { date: formatDateTime(c.checked_at) })}</span>
                  )}{" "}
                  {c.error && <span className="mutedText">{c.error}</span>}{" "}
                  <button
                    type="button"
                    className="button buttonGhost"
                    disabled={check.isPending}
                    onClick={() => check.mutate(c.pve_cluster_id)}
                  >
                    {t("backupGaps.checkNow")}
                  </button>
                </dd>
              </div>
            ))}
          </dl>
        )}
      </Panel>
      <Panel testId="backup-gaps-guests" padded={false}>
        {data.unprotected_guests.length === 0 ? (
          <p className={`mutedText ${styles.empty}`}>{t("backupGaps.noUnprotectedGuests")}</p>
        ) : (
          <DataTable columns={columns} rows={data.unprotected_guests} rowKey={(r) => `${r.pve_cluster_id}-${r.vmid}`} />
        )}
      </Panel>
    </>
  );
}

function ExceptionsSection() {
  const { t } = useTranslation();
  const query = useQuery({ queryKey: ["backup-gap-exceptions"], queryFn: backupGapsApi.exceptions });
  const columns: DataTableColumn<GapExceptionOut>[] = [
    { key: "when", header: t("backupGaps.recordedAt"), render: (r) => formatDateTime(r.created_at) },
    { key: "target", header: t("backupGaps.target"), render: (r) => r.target_label },
    {
      key: "action",
      header: t("common.status"),
      render: (r) => (
        <StatusBadge tone={r.active ? "info" : "unknown"} label={r.active ? t("backupGaps.accepted") : t("backupGaps.revoked")} />
      ),
    },
    { key: "reason", header: t("backupGaps.reason"), render: (r) => r.reason },
    { key: "expires", header: t("backupGaps.expires"), render: (r) => formatDateTime(r.expires_at) },
    { key: "by", header: t("backupGaps.recordedBy"), render: (r) => r.created_by_name ?? "—" },
  ];
  return (
    <Panel testId="backup-gaps-exceptions" padded={false}>
      <QueryState query={query} isEmpty={(rows) => rows.length === 0}>
        {(rows) => <DataTable columns={columns} rows={rows} rowKey={(r) => r.id} />}
      </QueryState>
    </Panel>
  );
}

const S3_QUERY_KEY = ["s3-endpoints"];

function S3LocationDialog({ endpoint, onClose }: { endpoint: S3EndpointOut; onClose: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const onSite = !endpoint.on_site;
  const mutation = useMutation({
    mutationFn: () => s3EndpointsApi.setLocation(endpoint.id, { on_site: onSite, reason: reason.trim() }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: S3_QUERY_KEY });
      queryClient.invalidateQueries({ queryKey: QUERY_KEY });
      queryClient.invalidateQueries({ queryKey: ["alarms"] });
      onClose();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : t("common.loadError")),
  });
  return (
    <Modal open title={onSite ? t("backupGaps.s3MarkOnSiteTitle") : t("backupGaps.s3MarkOffsiteTitle")} onClose={onClose}>
      <p className="panelIntro">
        {t(onSite ? "backupGaps.s3MarkOnSiteIntro" : "backupGaps.s3MarkOffsiteIntro", { endpoint: endpoint.name, host: endpoint.endpoint })}
      </p>
      {error && <InlineAlert>{error}</InlineAlert>}
      <div className="field">
        <label className="label" htmlFor="s3-reason">
          {t("backupGaps.reason")}
        </label>
        <textarea id="s3-reason" className="input" rows={3} value={reason} onChange={(e) => setReason(e.target.value)} />
        <p className="mutedText">{t("backupGaps.reasonHint")}</p>
      </div>
      <div className="formActions">
        <button type="button" className="button" onClick={onClose}>
          {t("common.cancel")}
        </button>
        <button
          type="button"
          className="button buttonPrimary"
          disabled={reason.trim().length < 10 || mutation.isPending}
          onClick={() => mutation.mutate()}
        >
          {onSite ? t("backupGaps.s3MarkOnSite") : t("backupGaps.s3MarkOffsite")}
        </button>
      </div>
    </Modal>
  );
}

function S3EndpointsSection({ isAdmin }: { isAdmin: boolean }) {
  const { t } = useTranslation();
  const query = useQuery({ queryKey: S3_QUERY_KEY, queryFn: s3EndpointsApi.list });
  const [editing, setEditing] = useState<S3EndpointOut | null>(null);
  const columns: DataTableColumn<S3EndpointOut>[] = [
    { key: "pbs", header: t("backupGaps.s3Pbs"), render: (r) => r.pbs_instance_name },
    {
      key: "endpoint",
      header: t("backupGaps.s3Endpoint"),
      render: (r) => (
        <>
          <div>{r.name}</div>
          <div className="mutedText">
            {r.endpoint}
            {r.port ? `:${r.port}` : ""}
            {r.region ? ` · ${r.region}` : ""}
          </div>
        </>
      ),
    },
    { key: "datastores", header: t("backupGaps.s3Datastores"), render: (r) => (r.datastores.length ? r.datastores.join(", ") : "—") },
    {
      key: "location",
      header: t("backupGaps.s3Location"),
      render: (r) => (
        <>
          <StatusBadge
            tone={r.removed_from_pbs_at ? "unknown" : r.on_site ? "warning" : "ok"}
            label={r.removed_from_pbs_at ? t("backupGaps.s3Removed") : r.on_site ? t("backupGaps.s3OnSite") : t("backupGaps.s3Offsite")}
          />
          {r.on_site_reason && (
            <div className="mutedText">
              {r.on_site_reason} ({r.on_site_changed_by ?? "?"}, {formatDateTime(r.on_site_changed_at)})
            </div>
          )}
        </>
      ),
    },
    ...(isAdmin
      ? [
          {
            key: "actions",
            header: "",
            render: (r: S3EndpointOut) => (
              <button type="button" className="button buttonGhost" onClick={() => setEditing(r)}>
                {r.on_site ? t("backupGaps.s3MarkOffsite") : t("backupGaps.s3MarkOnSite")}
              </button>
            ),
          },
        ]
      : []),
  ];
  return (
    <Panel testId="backup-gaps-s3" title={t("backupGaps.s3Title")} padded={false}>
      <p className="panelIntro">{t("backupGaps.s3Intro")}</p>
      <QueryState query={query}>
        {(rows) =>
          rows.length === 0 ? (
            <p className="panelIntro mutedText">{t("backupGaps.s3Empty")}</p>
          ) : (
            <DataTable columns={columns} rows={rows} rowKey={(r) => r.id} />
          )
        }
      </QueryState>
      {editing && <S3LocationDialog endpoint={editing} onClose={() => setEditing(null)} />}
    </Panel>
  );
}

function ThresholdsSection({ thresholds }: { thresholds: BackupGapThresholds }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [warning, setWarning] = useState(String(thresholds.offsite_lag_warning_hours));
  const [critical, setCritical] = useState(String(thresholds.offsite_lag_critical_hours));
  const [message, setMessage] = useState<string | null>(null);
  const mutation = useMutation({
    mutationFn: () =>
      backupGapsApi.updateThresholds({ offsite_lag_warning_hours: Number(warning), offsite_lag_critical_hours: Number(critical) }),
    onSuccess: () => {
      setMessage(null);
      queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
    onError: (err) => setMessage(err instanceof ApiError ? err.message : t("common.loadError")),
  });
  const invalid = !(Number(warning) >= 1 && Number(critical) >= Number(warning));
  return (
    <Panel testId="backup-gaps-thresholds" title={t("backupGaps.thresholdsTitle")}>
      <p className="panelIntro">{t("backupGaps.thresholdsIntro")}</p>
      {message && <InlineAlert>{message}</InlineAlert>}
      <div className="formGrid">
        <div className="field">
          <label className="label" htmlFor="gap-warning">
            {t("backupGaps.warningHours")}
          </label>
          <input id="gap-warning" type="number" min={1} className="input" value={warning} onChange={(e) => setWarning(e.target.value)} />
        </div>
        <div className="field">
          <label className="label" htmlFor="gap-critical">
            {t("backupGaps.criticalHours")}
          </label>
          <input id="gap-critical" type="number" min={1} className="input" value={critical} onChange={(e) => setCritical(e.target.value)} />
        </div>
      </div>
      <div className="formActions">
        <button type="button" className="button buttonPrimary" disabled={invalid || mutation.isPending} onClick={() => mutation.mutate()}>
          {t("common.save")}
        </button>
      </div>
    </Panel>
  );
}

export function BackupGapsPage() {
  const { t } = useTranslation();
  const { hasRole } = useAuth();
  const isAdmin = hasRole("platform_admin");
  const canSeeHistory = isAdmin || hasRole("auditor");
  const query = useQuery({ queryKey: QUERY_KEY, queryFn: backupGapsApi.get });
  const [target, setTarget] = useState<ExceptionTarget | null>(null);

  const tabIds = [
    "offsite",
    ...(isAdmin ? ["guests"] : []),
    ...(canSeeHistory ? ["exceptions", "s3"] : []),
    ...(isAdmin ? ["settings"] : []),
  ];
  const [tab, setTab] = useTabParam(tabIds);
  const summary = query.data?.summary;
  const count = (n: number | undefined) => (n ? ` (${n})` : "");

  return (
    <div>
      <PageHeader title={t("backupGaps.title")} description={t("backupGaps.description")} />
      <Tabs
        label={t("backupGaps.title")}
        tabs={[
          { id: "offsite", label: t("backupGaps.tabOffsite") + count(summary?.offsite_gaps) },
          ...(isAdmin ? [{ id: "guests", label: t("backupGaps.tabGuests") + count(summary?.unprotected_guests) }] : []),
          ...(canSeeHistory
            ? [
                { id: "exceptions", label: t("backupGaps.tabExceptions") },
                { id: "s3", label: t("backupGaps.tabS3") },
              ]
            : []),
          ...(isAdmin ? [{ id: "settings", label: t("backupGaps.tabSettings") }] : []),
        ]}
        active={tab}
        onChange={setTab}
      />
      {tab === "exceptions" && canSeeHistory ? (
        <TabPanel id="exceptions">
          <ExceptionsSection />
        </TabPanel>
      ) : tab === "s3" && canSeeHistory ? (
        <TabPanel id="s3">
          <S3EndpointsSection isAdmin={isAdmin} />
        </TabPanel>
      ) : (
        <QueryState query={query}>
          {(data) => (
            <>
              {tab === "offsite" && (
                <TabPanel id="offsite">
                  <OffsiteSection data={data} isAdmin={isAdmin} onException={setTarget} />
                </TabPanel>
              )}
              {tab === "guests" && isAdmin && (
                <TabPanel id="guests">
                  <GuestsSection data={data} onException={setTarget} />
                </TabPanel>
              )}
              {tab === "settings" && isAdmin && (
                <TabPanel id="settings">
                  <ThresholdsSection thresholds={data.thresholds} />
                </TabPanel>
              )}
            </>
          )}
        </QueryState>
      )}
      {target && <ExceptionDialog target={target} onClose={() => setTarget(null)} />}
    </div>
  );
}
