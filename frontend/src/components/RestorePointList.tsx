import { useTranslation } from "react-i18next";

import type { BackupSnapshotOut } from "../api/types";
import { formatBytes } from "../lib/formatBytes";
import { verifyStatusPresentation } from "../lib/statusPresentation";
import { DataTable, type DataTableColumn } from "./DataTable";
import { EncryptionBadge } from "./EncryptionBadge";
import { StatusBadge } from "./StatusBadge";
import styles from "./RestorePointList.module.css";

interface RestorePointListProps {
  snapshots: BackupSnapshotOut[];
  selectedSnapshotId: string;
  onSelect: (id: string) => void;
}

/** A sortable table of a backup group's restore points — the "list"
 * half of the richer restore-point browsing raised directly by the
 * user, alongside `RestorePointCalendar`. A superset of the plain
 * type-to-filter dropdown it replaces here: sortable by any column, not
 * just filterable by the formatted date string.
 */
export function RestorePointList({ snapshots, selectedSnapshotId, onSelect }: RestorePointListProps) {
  const { t } = useTranslation();

  const columns: DataTableColumn<BackupSnapshotOut>[] = [
    {
      key: "time",
      header: t("restorePointList.dateTime"),
      render: (row) => new Date(row.snapshot_time).toLocaleString(),
      sortValue: (row) => row.snapshot_time,
    },
    {
      key: "verify",
      header: t("common.status"),
      render: (row) => {
        const { tone, label } = verifyStatusPresentation(row.verify_status, t);
        return <StatusBadge tone={tone} label={label} />;
      },
      sortValue: (row) => row.verify_status,
    },
    {
      key: "encryption",
      header: t("encryption.column"),
      render: (row) => <EncryptionBadge mode={row.crypt_mode} fingerprint={row.key_fingerprint} />,
      sortValue: (row) => row.key_fingerprint ?? row.crypt_mode ?? "",
    },
    {
      key: "size",
      header: t("restorePointList.size"),
      render: (row) => formatBytes(row.size_bytes),
      sortValue: (row) => row.size_bytes ?? 0,
    },
  ];

  return (
    <div className={styles.list}>
      <DataTable
        columns={columns}
        rows={snapshots}
        rowKey={(row) => row.id}
        onRowClick={(row) => onSelect(row.id)}
        rowClassName={(row) => (row.id === selectedSnapshotId ? styles.selectedRow : undefined)}
      />
    </div>
  );
}
