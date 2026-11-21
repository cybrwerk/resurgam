import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { auditApi, usersApi } from "../api/resources";
import type { AuditEventOut } from "../api/types";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import { PageHeader } from "../components/PageHeader";
import { QueryState } from "../components/QueryState";
import { StatusBadge } from "../components/StatusBadge";
import styles from "./AuditLogPage.module.css";

const DEFAULT_LIMIT = 500;
const LIMIT_STEP = 500;

/** Manifest §8's append-only audit trail (`AuditEvent`, immutable both in
 * application code and at the DB level) — previously a nav placeholder
 * despite ~50 call sites across the backend already writing to it. Read
 * side only: platform_admin-only (the model has no customer_id of its
 * own to scope by — see `api/v1/audit.py`'s docstring), and, like task
 * history, an unbounded, ever-growing log — same bounded-limit +
 * "Load more" pattern as `TasksPage`, this time with server-side
 * action/target_type filters instead of task_types.
 */
export function AuditLogPage() {
  const { t } = useTranslation();
  const [search, setSearch] = useState("");
  const [limit, setLimit] = useState(DEFAULT_LIMIT);

  const usersQuery = useQuery({ queryKey: ["users"], queryFn: usersApi.list });
  const eventsQuery = useQuery({
    queryKey: ["audit-events", limit],
    queryFn: () => auditApi.list({ limit }),
  });

  // `actor_label` is snapshotted server-side at write time (survives the
  // actor's own user row being disabled/deleted later) -- preferred over
  // the live users-list join, which only ever reflects who exists *now*.
  // The join is kept as a fallback for rows written before that column
  // existed; the bare ID is a last resort so a truly unresolvable actor
  // never silently reads as "System".
  const actorLabelFor = (event: AuditEventOut) => {
    if (event.actor_label) return event.actor_label;
    if (!event.actor_user_id) return t("auditLog.systemActor");
    const user = usersQuery.data?.find((u) => u.id === event.actor_user_id);
    return user ? `${user.display_name} (${user.email})` : event.actor_user_id;
  };

  const columns: DataTableColumn<AuditEventOut>[] = [
    {
      key: "occurredAt",
      header: t("auditLog.occurredAt"),
      render: (row) => <span className={styles.nowrap}>{new Date(row.occurred_at).toLocaleString()}</span>,
      sortValue: (row) => row.occurred_at,
    },
    { key: "action", header: t("auditLog.action"), render: (row) => row.action, sortValue: (row) => row.action },
    {
      key: "targetType",
      header: t("auditLog.targetType"),
      render: (row) => row.target_type ?? "—",
      sortValue: (row) => row.target_type ?? "",
    },
    {
      key: "targetId",
      header: t("auditLog.targetId"),
      render: (row) =>
        row.target_id ? (
          <span className={`resurgam-mono ${styles.nowrap}`} title={row.target_id}>
            {row.target_id}
          </span>
        ) : (
          "—"
        ),
      sortValue: (row) => row.target_id ?? "",
    },
    {
      key: "actor",
      header: t("auditLog.actor"),
      render: (row) => (
        <span className={styles.actorCell}>
          {actorLabelFor(row)}
          {row.actor_is_breakglass && (
            <StatusBadge tone="warning" label={t("auditLog.breakglassBadge")} className={styles.breakglassBadge} />
          )}
        </span>
      ),
      sortValue: (row) => actorLabelFor(row),
    },
  ];

  const searchTerm = search.trim().toLowerCase();
  const matchesSearch = (event: AuditEventOut) =>
    searchTerm === "" ||
    [event.action, event.target_type ?? "", event.target_id ?? "", actorLabelFor(event)]
      .join(" ")
      .toLowerCase()
      .includes(searchTerm);

  return (
    <div>
      <PageHeader title={t("auditLog.title")} description={t("auditLog.description")} />

      <div className="toolbar">
        <div className="field">
          <label className="label" htmlFor="audit-log-search">
            {t("auditLog.search")}
          </label>
          <input
            id="audit-log-search"
            type="text"
            className={`input ${styles.searchInput}`}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("auditLog.searchPlaceholder")}
          />
        </div>
      </div>

      <QueryState query={eventsQuery} isEmpty={(data) => data.length === 0}>
        {(events) => {
          const visibleEvents = events.filter(matchesSearch);
          return (
            <>
              {visibleEvents.length === 0 ? (
                <EmptyState title={t("auditLog.noSearchMatches")} />
              ) : (
                <DataTable columns={columns} rows={visibleEvents} rowKey={(row) => row.id} />
              )}
              {events.length === limit && (
                <div className={styles.loadMoreRow}>
                  <p className="mutedText">{t("auditLog.showingMostRecent", { count: limit })}</p>
                  <button type="button" className="button" onClick={() => setLimit((l) => l + LIMIT_STEP)}>
                    {t("auditLog.loadMore")}
                  </button>
                </div>
              )}
            </>
          );
        }}
      </QueryState>
    </div>
  );
}
