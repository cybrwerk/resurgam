import { screen, waitFor } from "@testing-library/react";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { WhoAmIResponse } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { ProtectedRoute } from "./ProtectedRoute";

vi.mock("../api/resources", () => ({
  authApi: {
    whoami: vi.fn(),
  },
}));

const { authApi } = await import("../api/resources");

function renderProtected() {
  return renderWithProviders(
    <Routes>
      <Route path="/login" element={<div>Login Page</div>} />
      <Route
        path="/"
        element={
          <ProtectedRoute>
            <div>Secret content</div>
          </ProtectedRoute>
        }
      />
    </Routes>,
  );
}

describe("ProtectedRoute", () => {
  it("redirects to /login when there is no stored token", () => {
    renderProtected();
    expect(screen.getByText("Login Page")).toBeInTheDocument();
  });

  it("renders protected children once a valid session loads", async () => {
    window.localStorage.setItem("resurgam.token", "fake-token");
    vi.mocked(authApi.whoami).mockResolvedValue({
      user_id: "u1",
      email: "a@example.com",
      display_name: "A",
      is_breakglass: true,
      assignments: [],
    } satisfies WhoAmIResponse);

    renderProtected();

    await waitFor(() => expect(screen.getByText("Secret content")).toBeInTheDocument());
  });
});
