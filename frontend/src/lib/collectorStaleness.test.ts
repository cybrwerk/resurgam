import { describe, expect, it } from "vitest";

import type { PbsInstanceOut } from "../api/types";
import { collectorStaleness } from "./collectorStaleness";

function makeInstance(id: string, lastCollectorSyncAt: string | null): PbsInstanceOut {
  return {
    id,
    display_name: id,
    location: null,
    host: `${id}.example`,
    port: 8007,
    tls_trust_mode: "ca",
    tls_fingerprint_sha256: null,
    version: null,
    last_health_check_at: null,
    last_health_status: "ok",
    last_collector_sync_at: lastCollectorSyncAt,
    disabled: false,
  };
}

describe("collectorStaleness", () => {
  it("returns nulls when there are no relevant instances", () => {
    expect(collectorStaleness([], [])).toEqual({ oldestSyncedAt: null, anyNeverSynced: false });
    expect(collectorStaleness([makeInstance("a", "2026-09-27T00:00:00Z")], [])).toEqual({
      oldestSyncedAt: null,
      anyNeverSynced: false,
    });
  });

  it("picks the oldest sync time among the relevant instances only", () => {
    const instances = [
      makeInstance("a", "2026-09-27T10:00:00Z"),
      makeInstance("b", "2026-09-27T08:00:00Z"),
      makeInstance("c", "2026-09-01T00:00:00Z"), // not in instanceIds, must be ignored
    ];
    expect(collectorStaleness(instances, ["a", "b"])).toEqual({
      oldestSyncedAt: "2026-09-27T08:00:00Z",
      anyNeverSynced: false,
    });
  });

  it("flags anyNeverSynced when a relevant instance has never synced", () => {
    const instances = [makeInstance("a", "2026-09-27T10:00:00Z"), makeInstance("b", null)];
    const result = collectorStaleness(instances, ["a", "b"]);
    expect(result.anyNeverSynced).toBe(true);
  });
});
