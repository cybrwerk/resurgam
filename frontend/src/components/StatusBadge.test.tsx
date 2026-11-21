import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusBadge } from "./StatusBadge";

describe("StatusBadge", () => {
  it("renders the label as the pill text", () => {
    render(<StatusBadge tone="ok" label="Healthy" />);
    expect(screen.getByText("Healthy")).toBeInTheDocument();
  });

  it("applies a caller-supplied className alongside its own", () => {
    render(<StatusBadge tone="danger" label="Failed" className="extra-class" />);
    const badge = screen.getByText("Failed").closest("span");
    expect(badge?.className).toContain("extra-class");
    // still keeps its own tone/badge classes — this is additive, not a replacement.
    expect(badge?.className.split(" ").length).toBeGreaterThan(1);
  });

  it("omits the extra class list entirely when none is given", () => {
    render(<StatusBadge tone="ok" label="Healthy" />);
    const badge = screen.getByText("Healthy").closest("span");
    expect(badge?.className.trim().endsWith(" ")).toBe(false);
  });
});
