import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { PbsInstanceOut, TaskLogOut, TaskRunOut } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { TasksPage } from "./TasksPage";

vi.mock("../api/resources", () => ({
  pbsInstancesApi: { list: vi.fn(), sync: vi.fn(), syncJobs: vi.fn(), taskLog: vi.fn() },
  tasksApi: { list: vi.fn() },
}));

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, downloadBlob: vi.fn() };
});

const { pbsInstancesApi, tasksApi } = await import("../api/resources");
const { downloadBlob } = await import("../api/client");

const INSTANCE: PbsInstanceOut = {
  id: "pbs-1",
  display_name: "Primary PBS",
  location: null,
  host: "pbs1.internal.example",
  port: 8007,
  tls_trust_mode: "ca",
  tls_fingerprint_sha256: null,
  version: "4.2.1",
  last_health_check_at: null,
  last_health_status: "ok",
  last_collector_sync_at: null,
  disabled: false,
};

const INSTANCE_B: PbsInstanceOut = { ...INSTANCE, id: "pbs-2", display_name: "Secondary PBS" };

function task(overrides: Partial<TaskRunOut>): TaskRunOut {
  return {
    id: `task-${overrides.upid}`,
    pbs_instance_id: "pbs-1",
    upid: "UPID:pbs:0001:backup:vm/100",
    task_type: "backup",
    worker_id: "vm/100",
    status: "ok",
    started_at: "2026-01-01T10:00:00Z",
    ended_at: "2026-01-01T10:05:00Z",
    task_user: null,
    last_synced_at: "2026-01-01T10:05:00Z",
    ...overrides,
  };
}

const TASKS = [
  task({ upid: "UPID:pbs:0001:backup:vm/100", worker_id: "vm/100", started_at: "2026-01-01T08:00:00Z" }),
  task({ upid: "UPID:pbs:0002:backup:ct/200", worker_id: "ct/200", started_at: "2026-01-02T08:00:00Z" }),
];

function setup(instances: PbsInstanceOut[] = [INSTANCE]) {
  vi.mocked(pbsInstancesApi.list).mockResolvedValue(instances);
  vi.mocked(tasksApi.list).mockResolvedValue(TASKS);
}

async function waitForLoaded() {
  await waitFor(() => expect(screen.getByLabelText("Primary PBS")).toBeInTheDocument());
  await waitFor(() => expect(screen.getByLabelText("Search")).toBeInTheDocument());
}

describe("TasksPage", () => {
  it("pre-checks every visible instance by default and fetches from all of them", async () => {
    setup([INSTANCE, INSTANCE_B]);
    renderWithProviders(<TasksPage titleKey="nav.jobsBackup" />);

    await waitFor(() => expect(screen.getByLabelText("Primary PBS")).toBeChecked());
    expect(screen.getByLabelText("Secondary PBS")).toBeChecked();
    await waitFor(() =>
      expect(tasksApi.list).toHaveBeenCalledWith(
        expect.objectContaining({ instanceIds: expect.arrayContaining(["pbs-1", "pbs-2"]) }),
      ),
    );
  });

  it("unchecking one instance narrows the selection and refetches", async () => {
    setup([INSTANCE, INSTANCE_B]);
    const user = userEvent.setup();
    renderWithProviders(<TasksPage titleKey="nav.jobsBackup" />);
    await waitForLoaded();

    await user.click(screen.getByLabelText("Secondary PBS"));
    await waitFor(() =>
      expect(tasksApi.list).toHaveBeenCalledWith(expect.objectContaining({ instanceIds: ["pbs-1"] })),
    );
  });

  it("shows every task with no filter typed", async () => {
    setup();
    renderWithProviders(<TasksPage titleKey="nav.jobsBackup" />);
    await waitForLoaded();

    expect(screen.getByText("vm/100")).toBeInTheDocument();
    expect(screen.getByText("ct/200")).toBeInTheDocument();
  });

  it("filters tasks by worker as you type", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<TasksPage titleKey="nav.jobsBackup" />);
    await waitForLoaded();

    await user.type(screen.getByLabelText("Search"), "ct/200");
    expect(screen.getByText("ct/200")).toBeInTheDocument();
    expect(screen.queryByText("vm/100")).not.toBeInTheDocument();
  });

  it("shows a distinct empty state when the search matches nothing", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<TasksPage titleKey="nav.jobsBackup" />);
    await waitForLoaded();

    await user.type(screen.getByLabelText("Search"), "does-not-exist-anywhere");
    await waitFor(() => expect(screen.getByText("No tasks match your search.")).toBeInTheDocument());
  });

  it("sorts by worker when the column header is clicked", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<TasksPage titleKey="nav.jobsBackup" />);
    await waitForLoaded();

    const table = screen.getByRole("table");
    await user.click(within(table).getByRole("button", { name: /Worker/ }));
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows[0].textContent).toContain("ct/200");
    expect(rows[1].textContent).toContain("vm/100");
  });

  it("filters by started-after date", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<TasksPage titleKey="nav.jobsBackup" />);
    await waitForLoaded();

    await user.type(screen.getByLabelText("Started after"), "2026-01-15");
    await waitFor(() =>
      expect(tasksApi.list).toHaveBeenCalledWith(
        expect.objectContaining({ startedAfter: "2026-01-15T00:00:00Z" }),
      ),
    );
  });

  it("asks the server for this page's task types and a bounded default limit", async () => {
    setup();
    renderWithProviders(<TasksPage titleKey="nav.jobsBackup" taskTypes={["backup"]} />);
    await waitForLoaded();

    expect(tasksApi.list).toHaveBeenCalledWith(
      expect.objectContaining({ taskTypes: ["backup"], limit: 1000, instanceIds: ["pbs-1"] }),
    );
  });

  it(
    "offers Load more when the server returns exactly the requested limit, and bumps it on click",
    async () => {
      setup();
      vi.mocked(tasksApi.list).mockResolvedValue(
        Array.from({ length: 1000 }, (_, i) => task({ upid: `UPID:pbs:${i}:backup:vm/100`, id: `task-${i}` })),
      );
      const user = userEvent.setup();
      renderWithProviders(<TasksPage titleKey="nav.jobsBackup" />);
      await waitForLoaded();

      await waitFor(() => expect(screen.getByText("Showing the most recent 1000 tasks.")).toBeInTheDocument(), {
        timeout: 10000,
      });
      await user.click(screen.getByRole("button", { name: "Load more" }));

      await waitFor(() => expect(tasksApi.list).toHaveBeenCalledWith(expect.objectContaining({ limit: 2000 })));
    },
    // Rendering 1000 real DataTable rows in jsdom is legitimately heavier
    // than this file's other cases (which use a handful of fixture rows)
    // -- under CPU contention from the rest of the suite running
    // alongside it, that render alone can exceed vitest's default 5s
    // test timeout even though nothing is actually hung. DEFAULT_TASK_LIMIT
    // (TasksPage.tsx) is a fixed module constant, not a prop, so the
    // fixture can't be shrunk without testing a different code path than
    // "exactly at the real limit" -- widening the time budget instead.
    20000,
  );

  it("does not offer Load more when fewer rows than the limit come back", async () => {
    setup();
    renderWithProviders(<TasksPage titleKey="nav.jobsBackup" />);
    await waitForLoaded();

    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("hides Sync and the Sync Jobs table when more than one instance is checked", async () => {
    setup([INSTANCE, INSTANCE_B]);
    renderWithProviders(<TasksPage titleKey="jobs.replicationSyncTitle" showSyncJobs />);
    await waitFor(() => expect(screen.getByLabelText("Primary PBS")).toBeChecked());

    expect(screen.queryByRole("button", { name: "Sync now" })).not.toBeInTheDocument();
    expect(screen.getByText("Sync is available when exactly one PBS instance is selected.")).toBeInTheDocument();
  });

  it("shows Sync once the selection narrows to exactly one instance", async () => {
    setup([INSTANCE, INSTANCE_B]);
    const user = userEvent.setup();
    renderWithProviders(<TasksPage titleKey="nav.jobsBackup" />);
    await waitFor(() => expect(screen.getByLabelText("Primary PBS")).toBeChecked());

    await user.click(screen.getByLabelText("Secondary PBS"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Sync now" })).toBeInTheDocument());
  });

  it("clicking Export CSV triggers a blob download", async () => {
    setup();
    const user = userEvent.setup();
    renderWithProviders(<TasksPage titleKey="nav.jobsBackup" />);
    await waitForLoaded();

    await user.click(screen.getByRole("button", { name: "Export CSV" }));
    expect(downloadBlob).toHaveBeenCalledTimes(1);
    const [blob, filename] = vi.mocked(downloadBlob).mock.calls[0];
    expect(filename).toBe("tasks.csv");
    const text = await blob.text();
    expect(text).toContain("UPID:pbs:0001:backup:vm/100");
    expect(text).toContain("UPID:pbs:0002:backup:ct/200");
  });

  it("clicking a UPID opens the log modal and renders fetched log lines", async () => {
    setup();
    const logResult: TaskLogOut = { lines: [{ n: 1, text: "line one" }, { n: 2, text: "TASK OK" }], truncated: false };
    vi.mocked(pbsInstancesApi.taskLog).mockResolvedValue(logResult);
    const user = userEvent.setup();
    renderWithProviders(<TasksPage titleKey="nav.jobsBackup" />);
    await waitForLoaded();

    await user.click(screen.getByText("UPID:pbs:0001:backup:vm/100"));
    await waitFor(() => expect(screen.getByText(/line one/)).toBeInTheDocument());
    expect(pbsInstancesApi.taskLog).toHaveBeenCalledWith("pbs-1", "UPID:pbs:0001:backup:vm/100");
  });

  it("shows an error state when the log fetch fails", async () => {
    setup();
    vi.mocked(pbsInstancesApi.taskLog).mockRejectedValue(new Error("boom"));
    const user = userEvent.setup();
    renderWithProviders(<TasksPage titleKey="nav.jobsBackup" />);
    await waitForLoaded();

    await user.click(screen.getByText("UPID:pbs:0001:backup:vm/100"));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getByText(/Something went wrong/)).toBeInTheDocument());
  });
});
