import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { datastoresApi } from "../api/resources";
import type { BackupGroupOut } from "../api/types";
import { Modal } from "./Modal";
import { QueryState } from "./QueryState";

interface PriorGenerationsModalProps {
  datastoreId: string;
  currentGroup: BackupGroupOut;
  onClose: () => void;
}

function formatDateTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

/** A VMID slot can be reused for a different guest -- when that happens
 * the prior guest's own BackupGroup row is closed out (superseded_at
 * set), not deleted, so its history stays intact and reviewable. This
 * is a read-only view of that history, never editable -- a closed
 * generation is frozen by design.
 */
export function PriorGenerationsModal({ datastoreId, currentGroup, onClose }: PriorGenerationsModalProps) {
  const { t } = useTranslation();

  const query = useQuery({
    queryKey: ["backup-groups", datastoreId, "history"],
    queryFn: () => datastoresApi.backupGroups(datastoreId, { includeSuperseded: true }),
  });

  const priorGenerations = (query.data ?? []).filter(
    (g) =>
      g.namespace === currentGroup.namespace &&
      g.backup_type === currentGroup.backup_type &&
      g.backup_id === currentGroup.backup_id &&
      g.superseded_at !== null,
  );

  return (
    <Modal open title={t("protectedWorkloads.priorGenerationsTitle", { id: currentGroup.backup_id })} onClose={onClose}>
      <p className="mutedText">{t("protectedWorkloads.priorGenerationsHint")}</p>
      <QueryState query={query}>
        {() =>
          priorGenerations.length === 0 ? (
            <p className="mutedText">{t("protectedWorkloads.noPriorGenerations")}</p>
          ) : (
            <ul>
              {priorGenerations
                .sort((a, b) => (b.superseded_at ?? "").localeCompare(a.superseded_at ?? ""))
                .map((g) => (
                  <li key={g.id}>
                    <strong>{g.resolved_name ?? g.backup_id}</strong>
                    {" — "}
                    {t("protectedWorkloads.closedOn", { date: formatDateTime(g.superseded_at) })}
                  </li>
                ))}
            </ul>
          )
        }
      </QueryState>
    </Modal>
  );
}
