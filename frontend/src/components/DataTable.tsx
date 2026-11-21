import { useMemo, useState, type ReactNode } from "react";

import styles from "./DataTable.module.css";

export interface DataTableColumn<T> {
  key: string;
  header: string;
  render: (row: T) => ReactNode;
  /** Makes the header clickable and enables sorting by this column. Omit
   * for columns that don't have a sensible sort order (e.g. an action
   * column). */
  sortValue?: (row: T) => string | number;
}

interface DataTableProps<T> {
  columns: DataTableColumn<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  onRowClick?: (row: T) => void;
  /** Extra class applied to a row's <tr> — e.g. to highlight the
   * currently selected row in a picker-style table. */
  rowClassName?: (row: T) => string | undefined;
}

type SortDirection = "asc" | "desc";

export function DataTable<T>({ columns, rows, rowKey, onRowClick, rowClassName }: DataTableProps<T>) {
  const [sort, setSort] = useState<{ key: string; direction: SortDirection } | null>(null);

  const sortedRows = useMemo(() => {
    if (!sort) return rows;
    const column = columns.find((col) => col.key === sort.key);
    if (!column?.sortValue) return rows;
    const { sortValue } = column;
    const factor = sort.direction === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      const va = sortValue(a);
      const vb = sortValue(b);
      if (va < vb) return -1 * factor;
      if (va > vb) return 1 * factor;
      return 0;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, sort, columns]);

  const toggleSort = (key: string) => {
    setSort((current) => {
      if (current?.key !== key) return { key, direction: "asc" };
      return { key, direction: current.direction === "asc" ? "desc" : "asc" };
    });
  };

  return (
    <div className={styles.tableScroll}>
      <table className="table">
        <thead>
          <tr>
            {columns.map((col) => (
              <th
                key={col.key}
                className={col.key === "actions" ? styles.actionsCell : undefined}
                aria-sort={
                  col.sortValue
                    ? sort?.key === col.key
                      ? sort.direction === "asc"
                        ? "ascending"
                        : "descending"
                      : "none"
                    : undefined
                }
              >
                {col.sortValue ? (
                  <button type="button" className={styles.sortButton} onClick={() => toggleSort(col.key)}>
                    {col.header}
                    <span
                      className={`${styles.sortIndicator} ${sort?.key === col.key ? styles.sortIndicatorActive : ""}`}
                      aria-hidden="true"
                    >
                      {sort?.key === col.key ? (sort.direction === "asc" ? "▲" : "▼") : "⇅"}
                    </span>
                  </button>
                ) : (
                  col.header
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sortedRows.map((row) => (
            <tr
              key={rowKey(row)}
              className={[onRowClick ? "tableRowLink" : "", rowClassName?.(row) ?? ""].filter(Boolean).join(" ") || undefined}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
            >
              {columns.map((col) => (
                <td key={col.key} className={col.key === "actions" ? styles.actionsCell : undefined}>
                  {col.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
