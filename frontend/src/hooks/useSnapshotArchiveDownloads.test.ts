import { describe, expect, it } from "vitest";

import type { FileRestoreJobOut } from "../api/types";
import { archivePollInterval } from "./useSnapshotArchiveDownloads";

const job = (status: FileRestoreJobOut["status"]) => ({ status }) as FileRestoreJobOut;

describe("archivePollInterval", () => {
  it("polls while the job is unknown or in flight and stops once it ended", () => {
    expect(archivePollInterval({ data: undefined, error: null, errorUpdateCount: 0 })).toBe(1500);
    expect(archivePollInterval({ data: job("running"), error: null, errorUpdateCount: 0 })).toBe(1500);
    expect(archivePollInterval({ data: job("completed"), error: null, errorUpdateCount: 0 })).toBe(false);
    expect(archivePollInterval({ data: job("cancelled"), error: null, errorUpdateCount: 0 })).toBe(false);
  });

  it("backs off when the job can't be read and gives up after a few failures", () => {
    const err = new Error("Not found");
    expect(archivePollInterval({ data: undefined, error: err, errorUpdateCount: 1 })).toBe(3000);
    expect(archivePollInterval({ data: undefined, error: err, errorUpdateCount: 3 })).toBe(12000);
    expect(archivePollInterval({ data: undefined, error: err, errorUpdateCount: 5 })).toBe(false);
  });
});
