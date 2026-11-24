import { useTranslation } from "react-i18next";

import type { CryptMode } from "../api/types";
import { StatusBadge } from "./StatusBadge";

/** "ad:a6:9b:4c:…" -- enough to tell keys apart at a glance; the title shows all of it. */
export function shortFingerprint(fingerprint: string): string {
  return `${fingerprint.split(":").slice(0, 4).join(":")}:…`;
}

interface EncryptionBadgeProps {
  mode: CryptMode | null | undefined;
  fingerprint?: string | null;
  /** The newest backup uses a different key than the one before it. */
  keyChanged?: boolean;
}

/** A backup's client-side encryption as PBS reports it (ADR 0031). */
export function EncryptionBadge({ mode, fingerprint, keyChanged }: EncryptionBadgeProps) {
  const { t } = useTranslation();
  if (!mode) return null; // not known yet: the next inventory sync fills it in
  if (mode === "none") return <span className="mutedText">{t("encryption.none")}</span>;
  const title = fingerprint ? t("encryption.keyTitle", { fingerprint }) : undefined;
  const label =
    mode === "encrypt"
      ? fingerprint
        ? t("encryption.encryptedWith", { key: shortFingerprint(fingerprint) })
        : t("encryption.encrypted")
      : mode === "sign-only"
        ? t("encryption.signOnly")
        : t("encryption.mixed");
  return (
    <span title={title}>
      <StatusBadge tone={mode === "mixed" ? "warning" : "info"} label={label} />
      {keyChanged && (
        <span title={t("encryption.keyChangedHint")}>
          {" "}
          <StatusBadge tone="warning" label={t("encryption.keyChanged")} />
        </span>
      )}
    </span>
  );
}
