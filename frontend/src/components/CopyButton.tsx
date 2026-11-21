import { useState } from "react";
import { useTranslation } from "react-i18next";

/** Copies `text` to the clipboard; shows "Copied" for two seconds. */
export function CopyButton({ text, label }: { text: string; label: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="button"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          window.setTimeout(() => setCopied(false), 2000);
        } catch {
          setCopied(false);
        }
      }}
    >
      {copied ? t("labSetup.copied") : label}
    </button>
  );
}
