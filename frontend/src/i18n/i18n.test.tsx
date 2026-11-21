import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import i18n from "./index";
import { useTranslation } from "react-i18next";

function LanguageProbe() {
  const { t } = useTranslation();
  return <div>{t("auth.signIn")}</div>;
}

describe("i18n", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("renders English by default", () => {
    render(<LanguageProbe />);
    expect(screen.getByText("Sign in")).toBeInTheDocument();
  });

  it("switching to German updates rendered text and persists the choice", async () => {
    render(<LanguageProbe />);

    await i18n.changeLanguage("de");

    await waitFor(() => expect(screen.getByText("Anmelden")).toBeInTheDocument());
    expect(window.localStorage.getItem("resurgam.language")).toBe("de");
  });
});
