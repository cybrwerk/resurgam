import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ThemeProvider, useTheme } from "./ThemeContext";

function ThemeButtons() {
  const { mode, setMode } = useTheme();
  return (
    <div>
      <span data-testid="mode">{mode}</span>
      <button onClick={() => setMode("dark")}>dark</button>
      <button onClick={() => setMode("light")}>light</button>
      <button onClick={() => setMode("system")}>system</button>
    </div>
  );
}

describe("ThemeContext", () => {
  it("defaults to system (no data-theme attribute) when nothing is stored", () => {
    render(
      <ThemeProvider>
        <ThemeButtons />
      </ThemeProvider>,
    );
    expect(document.documentElement.getAttribute("data-theme")).toBeNull();
  });

  it("setting a mode updates the root data-theme attribute and persists it", async () => {
    const user = userEvent.setup();
    render(
      <ThemeProvider>
        <ThemeButtons />
      </ThemeProvider>,
    );

    await user.click(screen.getByText("dark"));

    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(window.localStorage.getItem("resurgam.theme")).toBe("dark");
  });

  it("a fresh mount reads the previously persisted mode", () => {
    window.localStorage.setItem("resurgam.theme", "dark");

    render(
      <ThemeProvider>
        <ThemeButtons />
      </ThemeProvider>,
    );

    expect(screen.getByTestId("mode")).toHaveTextContent("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("switching back to system removes the data-theme attribute", async () => {
    const user = userEvent.setup();
    window.localStorage.setItem("resurgam.theme", "dark");
    render(
      <ThemeProvider>
        <ThemeButtons />
      </ThemeProvider>,
    );

    await user.click(screen.getByText("system"));

    expect(document.documentElement.getAttribute("data-theme")).toBeNull();
    expect(window.localStorage.getItem("resurgam.theme")).toBe("system");
  });
});
