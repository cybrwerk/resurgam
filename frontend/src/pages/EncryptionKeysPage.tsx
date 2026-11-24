import { useTranslation } from "react-i18next";

import { EncryptionKeysPanel } from "../components/EncryptionKeys";
import { PageHeader } from "../components/PageHeader";

/** Administration -> Encryption keys (ADR 0031, stage 2): every PBS
 * client-side encryption key RESURGAM holds, by fingerprint. */
export function EncryptionKeysPage() {
  const { t } = useTranslation();
  return (
    <div>
      <PageHeader
        title={t("nav.administrationEncryptionKeys")}
        description={t("encryptionKeys.description")}
      />
      <EncryptionKeysPanel />
    </div>
  );
}
