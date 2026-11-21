import { useQueries, useQuery } from "@tanstack/react-query";

import { datastoresApi } from "../api/resources";
import type { BackupGroupOut } from "../api/types";

/** No "backup groups for this customer" endpoint exists, so this fetches
 * every datastore's groups (once a customer is picked, lazily) and
 * filters client-side by `effective_customer_id` -- the same pattern
 * `FileIndexPage`/`FileRecoveryPage` each had their own copy of before
 * the Unified File Workspace merged them. Fine at the real data scale
 * seen so far (a handful of datastores); would want a dedicated endpoint
 * if that grows.
 */
export function useBackupGroups(customerId: string): {
  groups: BackupGroupOut[];
  groupsLoading: boolean;
} {
  const datastoresQuery = useQuery({ queryKey: ["datastores"], queryFn: datastoresApi.list });
  const datastoreIds = datastoresQuery.data?.map((d) => d.id) ?? [];
  const groupsQueries = useQueries({
    queries:
      customerId === ""
        ? []
        : datastoreIds.map((id) => ({
            queryKey: ["backup-groups", id],
            queryFn: () => datastoresApi.backupGroups(id),
          })),
  });
  const groupsLoading = customerId !== "" && groupsQueries.some((q) => q.isLoading);
  const groups = (customerId === "" ? [] : groupsQueries.flatMap((q) => q.data ?? [])).filter(
    (group) => group.effective_customer_id === customerId,
  );
  return { groups, groupsLoading };
}
