import { describe, expect, it } from "vitest";

import type { TaskRunOut } from "../api/types";
import { lastBackupRun, lastGcRun, lastPruneRun, lastSyncRun, lastVerifyRun } from "./jobLastRun";

function task(overrides: Partial<TaskRunOut>): TaskRunOut {
  return {
    id: "t1", pbs_instance_id: "pbs-1", upid: "UPID:x", task_type: "backup", worker_id: null,
    status: "ok", started_at: "2026-01-01T00:00:00Z", ended_at: null, task_user: null,
    last_synced_at: "2026-01-01T00:00:00Z", ...overrides,
  };
}

describe("lastPruneRun", () => {
  it("matches by store:ns and picks the most recent", () => {
    const tasks = [
      task({ task_type: "prunejob", worker_id: "store1:tenant-a", started_at: "2026-01-01T00:00:00Z", status: "ok" }),
      task({ task_type: "prunejob", worker_id: "store1:tenant-a", started_at: "2026-02-01T00:00:00Z", status: "error" }),
      task({ task_type: "prunejob", worker_id: "store1:Other", started_at: "2026-03-01T00:00:00Z" }),
      task({ task_type: "prune", worker_id: "store1:tenant-a:vm/100", started_at: "2026-04-01T00:00:00Z" }),
    ];
    const result = lastPruneRun(tasks, "store1", "tenant-a");
    expect(result).toEqual({ status: "error", startedAt: "2026-02-01T00:00:00Z" });
  });

  it("matches bare store when ns is root", () => {
    const tasks = [task({ task_type: "prunejob", worker_id: "store1", started_at: "2026-01-01T00:00:00Z" })];
    expect(lastPruneRun(tasks, "store1", null)).toEqual({ status: "ok", startedAt: "2026-01-01T00:00:00Z" });
  });

  it("returns null when nothing matches", () => {
    expect(lastPruneRun([], "store1", "tenant-a")).toBeNull();
  });
});

describe("lastVerifyRun", () => {
  it("matches by store:job_id, not namespace", () => {
    const tasks = [
      task({ task_type: "verify", worker_id: "store1:v-abc123", started_at: "2026-01-01T00:00:00Z", status: "error" }),
      task({ task_type: "verify", worker_id: "store1:v-other", started_at: "2026-02-01T00:00:00Z" }),
    ];
    expect(lastVerifyRun(tasks, "store1", "v-abc123")).toEqual({ status: "error", startedAt: "2026-01-01T00:00:00Z" });
  });
});

describe("lastSyncRun", () => {
  it("mirrors verify's store:job_id shape", () => {
    const tasks = [task({ task_type: "sync", worker_id: "store1:s-xyz", started_at: "2026-01-01T00:00:00Z" })];
    expect(lastSyncRun(tasks, "store1", "s-xyz")).toEqual({ status: "ok", startedAt: "2026-01-01T00:00:00Z" });
  });
});

describe("lastGcRun", () => {
  it("matches bare store with no job scoping", () => {
    const tasks = [task({ task_type: "garbage_collection", worker_id: "store1", started_at: "2026-01-01T00:00:00Z" })];
    expect(lastGcRun(tasks, "store1")).toEqual({ status: "ok", startedAt: "2026-01-01T00:00:00Z" });
  });
});

describe("lastBackupRun", () => {
  it("matches vm/ct worker ids for any targeted vmid", () => {
    const tasks = [
      task({ task_type: "backup", worker_id: "store1:vm/116", started_at: "2026-01-01T00:00:00Z", status: "ok" }),
      task({ task_type: "backup", worker_id: "store1:ct/200", started_at: "2026-02-01T00:00:00Z", status: "error" }),
      task({ task_type: "backup", worker_id: "store1:vm/999", started_at: "2026-03-01T00:00:00Z" }),
    ];
    const result = lastBackupRun(tasks, "store1", ["116", "200"]);
    expect(result).toEqual({ status: "error", startedAt: "2026-02-01T00:00:00Z" });
  });

  it("returns null for an empty vmid list", () => {
    expect(lastBackupRun([task({ task_type: "backup", worker_id: "store1:vm/1" })], "store1", [])).toBeNull();
  });
});
