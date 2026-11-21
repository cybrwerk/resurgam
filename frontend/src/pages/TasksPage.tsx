import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { downloadBlob } from "../api/client";
import { pbsInstancesApi, tasksApi } from "../api/resources";
import type { SyncJobOut, TaskRunOut } from "../api/types";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import { PageHeader } from "../components/PageHeader";
import { Panel } from "../components/Panel";
import { QueryState } from "../components/QueryState";
import { StatusBadge } from "../components/StatusBadge";
import { TaskLogModal } from "../components/TaskLogModal";
import { taskStatusPresentation } from "../lib/statusPresentation";
import tasksStyles from "./TasksPage.module.css";

interface TasksPageProps {
  titleKey: string;
  /** Restricts the view to these PBS task types — the four "Jobs" nav
   * entries share this one component and this one underlying task list
   * (manifest §4.1's Collector normalizes all of them together), each
   * filtering server-side (`task_types` query param, applied before
   * `limit`) rather than being separate fake pages or over-fetching
   * every type just to filter it back out client-side. */
  taskTypes?: string[];
  /** Replication & Sync only: also show configured Sync Jobs (manifest
   * §8) above the task table. Shown separately from task history, not
   * correlated to a specific job's "last run" — that correlation is
   * unverified against a real PBS (docs/milestone-1-pbs-pve-api-assumptions.md). */
  showSyncJobs?: boolean;
}

function SyncJobsTable({ instanceId }: { instanceId: string }) {
  const { t } = useTranslation();
  const syncJobsQuery = useQuery({
    queryKey: ["sync-jobs", instanceId],
    queryFn: () => pbsInstancesApi.syncJobs(instanceId),
  });

  const columns: DataTableColumn<SyncJobOut>[] = [
    { key: "jobId", header: t("syncJobs.jobId"), render: (row) => row.job_id },
    { key: "remote", header: t("syncJobs.remote"), render: (row) => row.remote_name },
    { key: "remoteStore", header: t("syncJobs.remoteStore"), render: (row) => row.remote_store },
    { key: "localStore", header: t("syncJobs.localStore"), render: (row) => row.local_store_name },
    { key: "schedule", header: t("syncJobs.schedule"), render: (row) => row.schedule ?? "—" },
  ];

  return (
    <Panel title={t("syncJobs.title")} padded={false}>
      <QueryState query={syncJobsQuery} isEmpty={(data) => data.length === 0}>
        {(jobs) => <DataTable columns={columns} rows={jobs} rowKey={(row) => row.id} />}
      </QueryState>
    </Panel>
  );
}

const DEFAULT_TASK_LIMIT = 1000;
const TASK_LIMIT_STEP = 1000;

function csvEscape(value: string): string {
  if (/[",\n]/.test(value)) return `"${value.replace(/"/g, '""')}"`;
  return value;
}

export function TasksPage({ titleKey, taskTypes, showSyncJobs }: TasksPageProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [selectedInstanceIds, setSelectedInstanceIds] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [startedAfter, setStartedAfter] = useState("");
  const [startedBefore, setStartedBefore] = useState("");
  const [limit, setLimit] = useState(DEFAULT_TASK_LIMIT);
  const [logModalTask, setLogModalTask] = useState<TaskRunOut | null>(null);
  const hasSeeded = useRef(false);

  const instancesQuery = useQuery({ queryKey: ["pbs-instances"], queryFn: pbsInstancesApi.list });

  useEffect(() => {
    if (hasSeeded.current || !instancesQuery.data) return;
    hasSeeded.current = true;
    setSelectedInstanceIds(instancesQuery.data.map((instance) => instance.id));
  }, [instancesQuery.data]);

  const tasksQuery = useQuery({
    queryKey: ["tasks", selectedInstanceIds, taskTypes, startedAfter, startedBefore, limit],
    queryFn: () =>
      tasksApi.list({
        instanceIds: selectedInstanceIds,
        taskTypes,
        limit,
        startedAfter: startedAfter ? `${startedAfter}T00:00:00Z` : undefined,
        startedBefore: startedBefore ? `${startedBefore}T23:59:59Z` : undefined,
      }),
    enabled: selectedInstanceIds.length > 0,
  });

  const toggleInstance = (id: string) => {
    setSelectedInstanceIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
    setLimit(DEFAULT_TASK_LIMIT);
  };

  const soleInstanceId = selectedInstanceIds.length === 1 ? selectedInstanceIds[0] : null;

  const syncMutation = useMutation({
    mutationFn: () => pbsInstancesApi.sync(soleInstanceId!),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["tasks"] }),
  });

  const columns: DataTableColumn<TaskRunOut>[] = [
    {
      key: "upid",
      header: t("tasks.upid"),
      render: (row) => (
        <button
          type="button"
          className={`resurgam-mono ${tasksStyles.upidLink}`}
          onClick={() => setLogModalTask(row)}
          title={row.upid}
        >
          {row.upid}
        </button>
      ),
      sortValue: (row) => row.upid,
    },
    { key: "type", header: t("tasks.type"), render: (row) => row.task_type, sortValue: (row) => row.task_type },
    {
      key: "worker",
      header: t("tasks.worker"),
      render: (row) => row.worker_id ?? "—",
      sortValue: (row) => row.worker_id ?? "",
    },
    {
      key: "status",
      header: t("common.status"),
      render: (row) => {
        const { tone, label } = taskStatusPresentation(row.status, t);
        return <StatusBadge tone={tone} label={label} />;
      },
      sortValue: (row) => row.status,
    },
    {
      key: "started",
      header: t("tasks.started"),
      render: (row) => new Date(row.started_at).toLocaleString(),
      sortValue: (row) => row.started_at,
    },
    {
      key: "ended",
      header: t("tasks.ended"),
      render: (row) => (row.ended_at ? new Date(row.ended_at).toLocaleString() : "—"),
      sortValue: (row) => row.ended_at ?? "",
    },
  ];

  const searchTerm = search.trim().toLowerCase();
  const matchesSearch = (task: TaskRunOut) =>
    searchTerm === "" ||
    [task.upid, task.task_type, task.worker_id ?? "", task.status].join(" ").toLowerCase().includes(searchTerm);

  const hasTasks = selectedInstanceIds.length > 0 && (tasksQuery.data?.length ?? 0) > 0;
  const visibleTasks = (tasksQuery.data ?? []).filter(matchesSearch);

  const handleExportCsv = (visibleTasks: TaskRunOut[]) => {
    const header = ["upid", "type", "worker", "status", "started_at", "ended_at"];
    const rows = visibleTasks.map((task) =>
      [task.upid, task.task_type, task.worker_id ?? "", task.status, task.started_at, task.ended_at ?? ""]
        .map(csvEscape)
        .join(","),
    );
    const csv = [header.join(","), ...rows].join("\r\n");
    downloadBlob(new Blob([csv], { type: "text/csv" }), "tasks.csv");
  };

  return (
    <div>
      <PageHeader
        title={t(titleKey)}
        actions={
          soleInstanceId && (
            <button
              type="button"
              className="button buttonPrimary"
              onClick={() => syncMutation.mutate()}
              disabled={syncMutation.isPending}
            >
              {t("pbsInstances.sync")}
            </button>
          )
        }
      />

      <div className="toolbar">
        <div className="field">
          <span className="label" id="tasks-instances-label">
            {t("tasks.instances")}
          </span>
          <div className={tasksStyles.instanceGroup} role="group" aria-labelledby="tasks-instances-label">
            {instancesQuery.data?.map((instance) => (
              <label key={instance.id} className={tasksStyles.instanceCheckbox}>
                <input
                  type="checkbox"
                  checked={selectedInstanceIds.includes(instance.id)}
                  onChange={() => toggleInstance(instance.id)}
                />{" "}
                {instance.display_name}
              </label>
            ))}
          </div>
        </div>
        <div className="field">
          <label className="label" htmlFor="tasks-started-after">
            {t("tasks.startedAfter")}
          </label>
          <input
            id="tasks-started-after"
            type="date"
            className="input"
            value={startedAfter}
            onChange={(e) => {
              setStartedAfter(e.target.value);
              setLimit(DEFAULT_TASK_LIMIT);
            }}
          />
        </div>
        <div className="field">
          <label className="label" htmlFor="tasks-started-before">
            {t("tasks.startedBefore")}
          </label>
          <input
            id="tasks-started-before"
            type="date"
            className="input"
            value={startedBefore}
            onChange={(e) => {
              setStartedBefore(e.target.value);
              setLimit(DEFAULT_TASK_LIMIT);
            }}
          />
        </div>
        {/* Search + export only once there is a loaded, non-empty task
            list to search/export -- same visibility as before they moved
            into this one toolbar row. */}
        {hasTasks && (
          <>
            <div className={`field ${tasksStyles.searchField}`}>
              <label className="label" htmlFor="tasks-search">
                {t("tasks.search")}
              </label>
              <input
                id="tasks-search"
                type="text"
                className="input"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={t("tasks.searchPlaceholder")}
              />
            </div>
            <div className="toolbarSpacer" />
            <button
              type="button"
              className="button"
              onClick={() => handleExportCsv(visibleTasks)}
              disabled={visibleTasks.length === 0}
            >
              {t("tasks.exportCsv")}
            </button>
          </>
        )}
      </div>

      {selectedInstanceIds.length !== 1 && (
        <p className={`mutedText ${tasksStyles.hint}`}>{t("tasks.syncNeedsOneInstance")}</p>
      )}

      <div className="stack">
        {soleInstanceId && showSyncJobs && <SyncJobsTable instanceId={soleInstanceId} />}

        {selectedInstanceIds.length > 0 && (
          <QueryState query={tasksQuery} isEmpty={(data) => data.length === 0}>
            {(tasks) => (
              <div>
                {visibleTasks.length === 0 ? (
                  <EmptyState title={t("tasks.noSearchMatches")} />
                ) : (
                  <DataTable columns={columns} rows={visibleTasks} rowKey={(row) => row.id} />
                )}
                {tasks.length === limit && (
                  <div className={tasksStyles.loadMoreRow}>
                    <p className="mutedText">{t("tasks.showingMostRecent", { count: limit })}</p>
                    <button type="button" className="button" onClick={() => setLimit((l) => l + TASK_LIMIT_STEP)}>
                      {t("tasks.loadMore")}
                    </button>
                  </div>
                )}
              </div>
            )}
          </QueryState>
        )}
      </div>

      {logModalTask && (
        <TaskLogModal
          open
          instanceId={logModalTask.pbs_instance_id}
          upid={logModalTask.upid}
          onClose={() => setLogModalTask(null)}
        />
      )}
    </div>
  );
}
