import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "../test/testUtils";
import { VzdumpScheduleField } from "./VzdumpScheduleField";

function Harness({ initial, onChange }: { initial: string; onChange: (value: string) => void }) {
  const [value, setValue] = useState(initial);
  return (
    <VzdumpScheduleField
      value={value}
      onChange={(next) => {
        setValue(next);
        onChange(next);
      }}
    />
  );
}

function renderField(initial: string, onChange = vi.fn()) {
  renderWithProviders(<Harness initial={initial} onChange={onChange} />);
  return onChange;
}

describe("VzdumpScheduleField", () => {
  it("recognizes an existing fixed daily time and previews it", async () => {
    renderField("21:00");
    expect(screen.getByLabelText("Schedule")).toHaveValue("daily");
    expect(screen.getByDisplayValue("21:00")).toBeInTheDocument();
    expect(screen.getByText("Saved as:")).toBeInTheDocument();
    expect(screen.getByText("21:00")).toBeInTheDocument();
  });

  it("falls back to Advanced for a value the picker doesn't model, without mangling it", () => {
    renderField("mon..fri 21:00");
    expect(screen.getByLabelText("Schedule")).toHaveValue("advanced");
    expect(screen.getByDisplayValue("mon..fri 21:00")).toBeInTheDocument();
  });

  it("builds a weekly schedule string from chosen days and a time", async () => {
    const onChange = renderField("21:00");
    const user = userEvent.setup();

    await user.selectOptions(screen.getByLabelText("Schedule"), "weekly");
    expect(screen.getByText("Pick at least one day.")).toBeInTheDocument();

    await user.click(screen.getByText("Mon"));
    await user.click(screen.getByText("Wed"));
    await user.click(screen.getByText("Fri"));

    expect(onChange).toHaveBeenLastCalledWith("mon,wed,fri 00:00");
    expect(screen.queryByText("Pick at least one day.")).not.toBeInTheDocument();
  });

  it("builds an interval schedule string in PVE's step syntax", async () => {
    const onChange = renderField("21:00");
    const user = userEvent.setup();

    await user.selectOptions(screen.getByLabelText("Schedule"), "interval");
    const valueInput = screen.getByDisplayValue("15");
    await user.clear(valueInput);
    await user.type(valueInput, "30");

    expect(onChange).toHaveBeenLastCalledWith("*/30");

    await user.selectOptions(screen.getByDisplayValue("minutes"), "hours");
    expect(onChange).toHaveBeenLastCalledWith("0/30:00");
  });

  it("seeds Advanced mode from what was just configured, not the initial value", async () => {
    // Real bug found live-verifying this component: Advanced's raw text
    // was seeded once at mount and never updated, so configuring an
    // interval schedule then switching to Advanced to fine-tune it
    // silently threw the configured interval away and showed the
    // original "02:30" instead.
    renderField("02:30");
    const user = userEvent.setup();

    await user.selectOptions(screen.getByLabelText("Schedule"), "interval");
    const valueInput = screen.getByDisplayValue("15");
    await user.clear(valueInput);
    await user.type(valueInput, "30");
    expect(screen.getByText("*/30")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Schedule"), "advanced");
    expect(screen.getByDisplayValue("*/30")).toBeInTheDocument();
  });

  it("lets Advanced mode edit the raw string directly", async () => {
    const onChange = renderField("advanced-garbage-not-modeled");
    const user = userEvent.setup();

    const raw = screen.getByDisplayValue("advanced-garbage-not-modeled");
    await user.clear(raw);
    await user.type(raw, "sat 03:00");

    expect(onChange).toHaveBeenLastCalledWith("sat 03:00");
  });
});
