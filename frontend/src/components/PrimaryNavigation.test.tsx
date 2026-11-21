import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { WhoAmIResponse } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { PrimaryNavigation } from "./PrimaryNavigation";

vi.mock("../api/resources", () => ({
  authApi: { whoami: vi.fn() },
  customersApi: { list: vi.fn() },
}));

const { authApi, customersApi } = await import("../api/resources");

function whoami(overrides: Partial<WhoAmIResponse> = {}): WhoAmIResponse {
  return {
    user_id: "u1", email: "a@example.com", display_name: "A", is_breakglass: false,
    assignments: [{ role: "restore_operator", scope_type: "global", scope_id: null }],
    ...overrides,
  };
}

async function renderNavAs(user: WhoAmIResponse, route = "/") {
  window.localStorage.setItem("resurgam.token", "fake-token");
  vi.mocked(authApi.whoami).mockResolvedValue(user);
  vi.mocked(customersApi.list).mockResolvedValue([]);
  const result = renderWithProviders(<PrimaryNavigation />, { route });
  await waitFor(() => expect(screen.getAllByText("Backups")[0]).toBeInTheDocument());
  return result;
}

describe("PrimaryNavigation — privilege-based visibility", () => {
  beforeEach(() => window.localStorage.clear());

  it("hides Infrastruktur and Administration for a user without platform_admin", async () => {
    // A real UX-audit finding (2026-09-23): every nav entry showed for
    // every signed-in user, including sections whose every real action
    // 403s for anyone but a platform_admin.
    await renderNavAs(whoami({ assignments: [{ role: "restore_operator", scope_type: "global", scope_id: null }] }));

    expect(screen.queryByText("Infrastructure")).not.toBeInTheDocument();
    expect(screen.queryByText("Administration")).not.toBeInTheDocument();
    // Scope-checked (not platform_admin-gated) sections stay visible for
    // any authenticated role.
    expect(screen.getByText("Backups")).toBeInTheDocument();
    expect(screen.getByText("Recovery")).toBeInTheDocument();
    expect(screen.getByText("Reporting")).toBeInTheDocument();
  });

  it("shows every section for a platform_admin", async () => {
    await renderNavAs(whoami({ assignments: [{ role: "platform_admin", scope_type: "global", scope_id: null }] }));

    expect(screen.getByText("Infrastructure")).toBeInTheDocument();
    expect(screen.getByText("Administration")).toBeInTheDocument();
  });

  it("hides Infrastruktur/Administration for a user with no role assignments at all", async () => {
    await renderNavAs(whoami({ assignments: [] }));

    expect(screen.queryByText("Infrastructure")).not.toBeInTheDocument();
    expect(screen.queryByText("Administration")).not.toBeInTheDocument();
  });
});

const admin = () => whoami({ assignments: [{ role: "platform_admin", scope_type: "global", scope_id: null }] });

describe("PrimaryNavigation — single-open accordion", () => {
  beforeEach(() => window.localStorage.clear());

  it("opens only the section holding the current page", async () => {
    await renderNavAs(admin(), "/backups/vms");

    expect(screen.getByText("Virtual Machines")).toBeInTheDocument();
    // Every other section stays closed -- the old tree listed all 33 entries.
    expect(screen.queryByText("Restore Center")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Backups/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: /Recovery/ })).toHaveAttribute("aria-expanded", "false");
  });

  it("opening another section closes the open one", async () => {
    const user = userEvent.setup();
    await renderNavAs(admin(), "/backups/vms");

    await user.click(screen.getByRole("button", { name: /Recovery/ }));

    expect(screen.getByText("Restore Center")).toBeInTheDocument();
    expect(screen.queryByText("Virtual Machines")).not.toBeInTheDocument();
  });

  it("clicking the open section closes it", async () => {
    const user = userEvent.setup();
    await renderNavAs(admin(), "/backups/vms");

    await user.click(screen.getByRole("button", { name: /Backups/ }));

    expect(screen.queryByText("Virtual Machines")).not.toBeInTheDocument();
  });

  it("keeps everything closed on a page outside any section", async () => {
    await renderNavAs(admin(), "/");

    expect(screen.queryByText("Virtual Machines")).not.toBeInTheDocument();
    expect(screen.queryByText("Restore Center")).not.toBeInTheDocument();
  });
});

describe("PrimaryNavigation — icon rail", () => {
  beforeEach(() => window.localStorage.clear());

  it("remembers the collapsed rail across a remount", async () => {
    const user = userEvent.setup();
    const { unmount } = await renderNavAs(admin());

    await user.click(screen.getByRole("button", { name: "Collapse menu" }));
    expect(screen.getByRole("button", { name: "Expand menu" })).toHaveAttribute("aria-pressed", "true");
    unmount();

    await renderNavAs(admin());
    expect(screen.getByRole("button", { name: "Expand menu" })).toHaveAttribute("aria-pressed", "true");
  });

  it("renders every section's entries for the hover/focus flyouts", async () => {
    window.localStorage.setItem("resurgam.navRail", "true");
    await renderNavAs(admin(), "/backups/vms");

    // In the rail each section's list is always in the DOM (CSS shows it
    // as a flyout), so closed sections' entries are reachable too.
    expect(screen.getByText("Virtual Machines")).toBeInTheDocument();
    expect(screen.getByText("Restore Center")).toBeInTheDocument();
  });
});
