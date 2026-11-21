import { useTranslation } from "react-i18next";

import { EmptyState } from "../components/EmptyState";

export function NotAvailablePage() {
  const { t } = useTranslation();
  return <EmptyState title={t("common.notAvailable")} description={t("common.notAvailableDescription")} />;
}
