import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { EncryptionKeyOut, WhoAmIResponse } from "../api/types";
import { renderWithProviders } from "../test/testUtils";
import { EncryptionKeysPanel, keyfileProblem } from "./EncryptionKeys";

vi.mock("../api/resources", () => ({
  authApi: { whoami: vi.fn() },
  customersApi: { list: vi.fn() },
  encryptionKeysApi: {
    list: vi.fn(),
    missing: vi.fn(),
    upload: vi.fn(),
    update: vi.fn(),
    remove: vi.fn(),
    download: vi.fn(),
    keySheet: vi.fn(),
    confirmCopy: vi.fn(),
    exportAll: vi.fn(),
    importAll: vi.fn(),
    tenantPermission: vi.fn(),
    setTenantPermission: vi.fn(),
  },
}));

const { authApi, customersApi, encryptionKeysApi } =
  await import("../api/resources");

const FP = Array(32).fill("ad").join(":");
const KEYFILE = JSON.stringify({
  kdf: null,
  created: "2026-11-22T10:00:00Z",
  data: "abc",
  fingerprint: FP,
});

function key(overrides: Partial<EncryptionKeyOut> = {}): EncryptionKeyOut {
  return {
    id: "key-1",
    fingerprint: FP,
    label: "pve storage pbs-main",
    customer_id: "cust-a",
    customer_name: "Tenant A",
    kdf: "none",
    source: "upload",
    key_created_at: "2026-11-22T09:00:00Z",
    created_at: "2026-11-22T10:00:00Z",
    copy_confirmed_at: null,
    backup_count: 4,
    can_download: true,
    ...overrides,
  };
}

function loginAs(
  role: string,
  scope: { scope_type: string; scope_id: string | null } = {
    scope_type: "global",
    scope_id: null,
  },
) {
  window.localStorage.setItem("resurgam.token", "fake-token");
  vi.mocked(authApi.whoami).mockResolvedValue({
    user_id: "u-me",
    email: "me@example.com",
    display_name: "Me",
    is_breakglass: false,
    assignments: [{ role, ...scope }],
  } as WhoAmIResponse);
}

afterEach(() => {
  window.localStorage.removeItem("resurgam.token");
  vi.clearAllMocks();
});

describe("keyfileProblem", () => {
  it("accepts a PBS keyfile without passphrase and rejects the rest", () => {
    expect(keyfileProblem(KEYFILE)).toBeNull();
    expect(keyfileProblem("not json")).toBe("encryptionKeys.invalidJson");
    expect(keyfileProblem('{"foo": 1}')).toBe("encryptionKeys.invalidJson");
    expect(
      keyfileProblem(JSON.stringify({ kdf: { Scrypt: {} }, data: "abc" })),
    ).toBe("encryptionKeys.passwordProtected");
  });
});

describe("EncryptionKeysPanel", () => {
  it("lists keys and warns about missing ones and unconfirmed copies", async () => {
    loginAs("platform_admin");
    vi.mocked(encryptionKeysApi.list).mockResolvedValue([key()]);
    vi.mocked(encryptionKeysApi.missing).mockResolvedValue([
      Array(32).fill("11").join(":"),
    ]);
    renderWithProviders(<EncryptionKeysPanel />);

    expect(await screen.findByText("pve storage pbs-main")).toBeInTheDocument();
    expect(screen.getByText("Tenant A")).toBeInTheDocument();
    expect(screen.getByText("Not confirmed")).toBeInTheDocument();
    expect(
      await screen.findByText(/Backups are encrypted with 1 key/),
    ).toBeInTheDocument();
  });

  it("downloads only after a confirmation and then records the copy", async () => {
    loginAs("platform_admin");
    vi.mocked(encryptionKeysApi.list).mockResolvedValue([key()]);
    vi.mocked(encryptionKeysApi.missing).mockResolvedValue([]);
    vi.mocked(encryptionKeysApi.keySheet)
      .mockRejectedValueOnce(
        new ApiError(403, "confirmation_required", "x", undefined, {
          method: "password",
        }),
      )
      .mockResolvedValueOnce(undefined);
    vi.mocked(encryptionKeysApi.confirmCopy).mockResolvedValue(
      key({ copy_confirmed_at: "2026-11-24T08:00:00Z" }),
    );
    const user = userEvent.setup();
    renderWithProviders(<EncryptionKeysPanel />);

    await user.click(await screen.findByRole("button", { name: "Key sheet" }));
    const dialog = await screen.findByRole("dialog");
    const download = within(dialog).getByRole("button", {
      name: "Download key sheet (PDF)",
    });
    await user.click(download);
    expect(
      await within(dialog).findByText(
        "Enter your password or an authenticator code.",
      ),
    ).toBeInTheDocument();

    await user.type(within(dialog).getByLabelText("Password"), "secret");
    await user.click(download);
    await waitFor(() =>
      expect(encryptionKeysApi.keySheet).toHaveBeenLastCalledWith("key-1", {
        password: "secret",
        totp_code: null,
      }),
    );
    await user.click(
      await within(dialog).findByRole("button", {
        name: "Copy is stored safely",
      }),
    );
    await waitFor(() =>
      expect(encryptionKeysApi.confirmCopy).toHaveBeenCalledWith("key-1"),
    );
  });

  it("uploads a pasted key and blocks a passphrase-protected one", async () => {
    loginAs("platform_admin");
    vi.mocked(encryptionKeysApi.list).mockResolvedValue([]);
    vi.mocked(encryptionKeysApi.missing).mockResolvedValue([]);
    vi.mocked(customersApi.list).mockResolvedValue([]);
    vi.mocked(encryptionKeysApi.upload).mockResolvedValue(key());
    const user = userEvent.setup();
    renderWithProviders(<EncryptionKeysPanel />);

    await user.click(await screen.findByRole("button", { name: "Upload key" }));
    const dialog = await screen.findByRole("dialog");
    const textarea = within(dialog).getByLabelText("…or paste its content");
    const save = within(dialog).getByRole("button", { name: "Upload key" });
    await user.click(textarea);
    await user.paste(JSON.stringify({ kdf: { Scrypt: {} }, data: "abc" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("password");
    expect(save).toBeDisabled();

    await user.clear(textarea);
    await user.paste(KEYFILE);
    await user.click(save);
    await waitFor(() =>
      expect(encryptionKeysApi.upload).toHaveBeenCalledWith({
        keyfile_content: KEYFILE,
        label: "",
        customer_id: null,
      }),
    );
  });

  it("export needs a long, repeated passphrase", async () => {
    loginAs("platform_admin");
    vi.mocked(encryptionKeysApi.list).mockResolvedValue([key()]);
    vi.mocked(encryptionKeysApi.missing).mockResolvedValue([]);
    vi.mocked(encryptionKeysApi.exportAll).mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderWithProviders(<EncryptionKeysPanel />);

    await user.click(await screen.findByRole("button", { name: "Export all" }));
    const dialog = await screen.findByRole("dialog");
    const go = within(dialog).getByRole("button", { name: "Export all" });
    await user.type(within(dialog).getByLabelText("Passphrase"), "short");
    expect(
      within(dialog).getByText("At least 12 characters."),
    ).toBeInTheDocument();
    await user.clear(within(dialog).getByLabelText("Passphrase"));
    await user.type(
      within(dialog).getByLabelText("Passphrase"),
      "a long passphrase",
    );
    await user.type(
      within(dialog).getByLabelText("Repeat passphrase"),
      "a long passphrase",
    );
    await user.type(
      within(dialog).getByLabelText("Authenticator code"),
      "123456",
    );
    expect(go).toBeEnabled();
    await user.click(go);
    await waitFor(() =>
      expect(encryptionKeysApi.exportAll).toHaveBeenCalledWith(
        "a long passphrase",
        {
          password: null,
          totp_code: "123456",
        },
      ),
    );
  });

  it("tenant admins see no management actions and no download without permission", async () => {
    loginAs("customer_admin", { scope_type: "customer", scope_id: "cust-a" });
    vi.mocked(encryptionKeysApi.list).mockResolvedValue([
      key({ can_download: false }),
    ]);
    vi.mocked(encryptionKeysApi.tenantPermission).mockResolvedValue({
      customer_id: "cust-a",
      allowed: false,
    });
    renderWithProviders(<EncryptionKeysPanel customerId="cust-a" />);

    expect(await screen.findByText("pve storage pbs-main")).toBeInTheDocument();
    expect(
      await screen.findByText(/not enabled for this tenant/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Key sheet" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Upload key" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Delete" }),
    ).not.toBeInTheDocument();
    expect(encryptionKeysApi.missing).not.toHaveBeenCalled();
  });
});
