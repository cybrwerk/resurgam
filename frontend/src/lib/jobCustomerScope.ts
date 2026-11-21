import type { DatastoreOut, PveStorageOut, VzdumpJobOut } from "../api/types";

/** A PBS-datastore-scoped job (prune/verify/sync/GC) inherits its customer
 * scope directly from the `Datastore` it targets -- each `Datastore` is
 * customer-scoped by design (manifest §9.1). `null` means unassigned, not
 * "no scope resolvable." */
export function resolveDatastoreJobCustomerId(
  store: string,
  datastoresByName: Map<string, DatastoreOut>,
): string | null {
  return datastoresByName.get(store)?.customer_id ?? null;
}

/** A vzdump job's scope is either an explicit override (`job.customer_id`,
 * docs/adr/0023) or, when unset, inherited from its target PVE storage's
 * real PBS datastore (`pbs_datastore`, confirmed live via PVE storage
 * config -- same resolution `RetentionComparisonHint` already uses for a
 * single job) mapped back to that datastore's own `customer_id`. Storage
 * name is never assumed to equal datastore name -- they're commonly
 * different (confirmed live: PVE storage "pbs-main" maps to PBS datastore
 * "backup-store"). */
export function resolveVzdumpJobCustomerId(
  job: VzdumpJobOut,
  storages: PveStorageOut[],
  datastoresByName: Map<string, DatastoreOut>,
): string | null {
  if (job.customer_id) return job.customer_id;
  if (!job.storage) return null;
  const matchedStorage = storages.find((s) => s.storage === job.storage);
  if (!matchedStorage?.pbs_datastore) return null;
  return datastoresByName.get(matchedStorage.pbs_datastore)?.customer_id ?? null;
}

export function buildDatastoresByName(datastores: DatastoreOut[]): Map<string, DatastoreOut> {
  return new Map(datastores.map((d) => [d.name, d]));
}
