import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderWithProviders } from "../test/testUtils";
import { EncryptionBadge, shortFingerprint } from "./EncryptionBadge";

const FP = "ad:a6:9b:4c:ec:20:02:30:68:c5:12:2c:7f:ee:73:eb:9b:62:40:70:88:a7:be:19:c7:49:d1:1e:11:8a:5f:7d";

describe("EncryptionBadge", () => {
  it("shortens a fingerprint to its first four bytes", () => {
    expect(shortFingerprint(FP)).toBe("ad:a6:9b:4c:…");
  });

  it("shows the key of an encrypted backup, with the full fingerprint as title", () => {
    renderWithProviders(<EncryptionBadge mode="encrypt" fingerprint={FP} />);
    const badge = screen.getByText("Encrypted · ad:a6:9b:4c:…");
    expect(badge.closest("[title]")?.getAttribute("title")).toContain(FP);
  });

  it("says when a backup is not encrypted, and shows nothing while unknown", () => {
    const { unmount } = renderWithProviders(<EncryptionBadge mode="none" />);
    expect(screen.getByText("Not encrypted")).toBeInTheDocument();
    unmount();
    const { container } = renderWithProviders(<EncryptionBadge mode={null} />);
    expect(container.textContent).toBe("");
  });

  it("flags a key change", () => {
    renderWithProviders(<EncryptionBadge mode="encrypt" fingerprint={FP} keyChanged />);
    expect(screen.getByText("Key changed")).toBeInTheDocument();
  });
});
