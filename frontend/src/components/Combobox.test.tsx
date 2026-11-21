import { useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { Combobox, type ComboboxOption } from "./Combobox";

const OPTIONS: ComboboxOption[] = [
  { value: "vm-100", label: "VM 100 — tenant-a" },
  { value: "vm-101", label: "VM 101 — tenant-a" },
  { value: "ct-200", label: "CT 200 — Firestorm" },
];

function ControlledCombobox({ options = OPTIONS }: { options?: ComboboxOption[] }) {
  const [value, setValue] = useState("");
  return (
    <div>
      <label htmlFor="workload">Workload</label>
      <Combobox id="workload" options={options} value={value} onChange={setValue} placeholder="Search…" />
      <p data-testid="value">{value}</p>
    </div>
  );
}

describe("Combobox", () => {
  it("shows all options on focus", async () => {
    const user = userEvent.setup();
    render(<ControlledCombobox />);
    await user.click(screen.getByLabelText("Workload"));
    expect(screen.getByRole("option", { name: "VM 100 — tenant-a" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "CT 200 — Firestorm" })).toBeInTheDocument();
  });

  it("filters options as you type", async () => {
    const user = userEvent.setup();
    render(<ControlledCombobox />);
    await user.click(screen.getByLabelText("Workload"));
    await user.type(screen.getByLabelText("Workload"), "Firestorm");

    expect(screen.getByRole("option", { name: "CT 200 — Firestorm" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "VM 100 — tenant-a" })).not.toBeInTheDocument();
  });

  it("selects an option on click, updating the controlled value and the displayed text", async () => {
    const user = userEvent.setup();
    render(<ControlledCombobox />);
    await user.click(screen.getByLabelText("Workload"));
    await user.click(screen.getByRole("option", { name: "VM 101 — tenant-a" }));

    expect(screen.getByTestId("value")).toHaveTextContent("vm-101");
    expect(screen.getByLabelText("Workload")).toHaveValue("VM 101 — tenant-a");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("selects the highlighted option on Enter", async () => {
    const user = userEvent.setup();
    render(<ControlledCombobox />);
    await user.click(screen.getByLabelText("Workload"));
    await user.keyboard("{ArrowDown}{ArrowDown}{Enter}");

    expect(screen.getByTestId("value")).toHaveTextContent("ct-200");
  });

  it("reverts unmatched typed text back to the selection on blur", async () => {
    const user = userEvent.setup();
    render(
      <div>
        <ControlledCombobox />
        <button type="button">elsewhere</button>
      </div>,
    );
    await user.click(screen.getByLabelText("Workload"));
    await user.click(screen.getByRole("option", { name: "VM 100 — tenant-a" }));

    await user.click(screen.getByLabelText("Workload"));
    await user.type(screen.getByLabelText("Workload"), "nonsense that matches nothing");
    await user.click(screen.getByText("elsewhere"));

    await waitFor(() => expect(screen.getByLabelText("Workload")).toHaveValue("VM 100 — tenant-a"));
    expect(screen.getByTestId("value")).toHaveTextContent("vm-100");
  });

  it("clears the selection when the input is emptied", async () => {
    const user = userEvent.setup();
    render(<ControlledCombobox />);
    await user.click(screen.getByLabelText("Workload"));
    await user.click(screen.getByRole("option", { name: "VM 100 — tenant-a" }));
    expect(screen.getByTestId("value")).toHaveTextContent("vm-100");

    await user.clear(screen.getByLabelText("Workload"));
    expect(screen.getByTestId("value")).toHaveTextContent("");
  });
});
