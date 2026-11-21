import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DataTable, type DataTableColumn } from "./DataTable";

interface Row {
  id: string;
  name: string;
  count: number;
}

const ROWS: Row[] = [
  { id: "b", name: "Beta", count: 2 },
  { id: "a", name: "Alpha", count: 10 },
  { id: "c", name: "Charlie", count: 1 },
];

const COLUMNS: DataTableColumn<Row>[] = [
  { key: "name", header: "Name", render: (row) => row.name, sortValue: (row) => row.name },
  { key: "count", header: "Count", render: (row) => String(row.count), sortValue: (row) => row.count },
  { key: "id", header: "ID", render: (row) => row.id },
];

function rowLabels() {
  return within(screen.getByRole("table"))
    .getAllByRole("row")
    .slice(1)
    .map((row) => row.textContent ?? "");
}

describe("DataTable", () => {
  it("renders rows in the given order when unsorted", () => {
    render(<DataTable columns={COLUMNS} rows={ROWS} rowKey={(row) => row.id} />);
    const labels = rowLabels();
    expect(labels[0]).toContain("Beta");
    expect(labels[1]).toContain("Alpha");
    expect(labels[2]).toContain("Charlie");
  });

  it("does not make a column without sortValue clickable", () => {
    render(<DataTable columns={COLUMNS} rows={ROWS} rowKey={(row) => row.id} />);
    expect(screen.queryByRole("button", { name: "ID" })).not.toBeInTheDocument();
  });

  it("sorts ascending on first click and descending on second click", async () => {
    const user = userEvent.setup();
    render(<DataTable columns={COLUMNS} rows={ROWS} rowKey={(row) => row.id} />);

    await user.click(screen.getByRole("button", { name: /Name/ }));
    expect(rowLabels().map((r) => r.trim())).toEqual([
      expect.stringContaining("Alpha"),
      expect.stringContaining("Beta"),
      expect.stringContaining("Charlie"),
    ]);

    await user.click(screen.getByRole("button", { name: /Name/ }));
    expect(rowLabels().map((r) => r.trim())).toEqual([
      expect.stringContaining("Charlie"),
      expect.stringContaining("Beta"),
      expect.stringContaining("Alpha"),
    ]);
  });

  it("switching sort column resets to ascending", async () => {
    const user = userEvent.setup();
    render(<DataTable columns={COLUMNS} rows={ROWS} rowKey={(row) => row.id} />);

    await user.click(screen.getByRole("button", { name: /Name/ })); // ascending by name
    await user.click(screen.getByRole("button", { name: /Count/ })); // ascending by count
    expect(rowLabels()[0]).toContain("Charlie"); // count 1
    expect(rowLabels()[2]).toContain("Alpha"); // count 10
  });
});
