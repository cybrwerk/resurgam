import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { UseQueryResult } from "@tanstack/react-query";

import { ApiError } from "../api/client";
import { EmptyState } from "./EmptyState";
import { InlineAlert } from "./InlineAlert";

interface QueryStateProps<T> {
  query: UseQueryResult<T>;
  isEmpty?: (data: T) => boolean;
  children: (data: T) => ReactNode;
}

/** Every data page renders loading, forbidden, error, and empty states
 * explicitly rather than assuming success — a 403 shows a clear
 * "no access" message, not a crash or a blank table. Centralized here so
 * every page gets this for free instead of reimplementing it. */
export function QueryState<T>({ query, isEmpty, children }: QueryStateProps<T>) {
  const { t } = useTranslation();

  if (query.isLoading) {
    return <p className="mutedText">{t("common.loading")}</p>;
  }

  if (query.isError) {
    const error = query.error;
    if (error instanceof ApiError && error.status === 403) {
      return (
        <EmptyState title={t("common.forbidden")} description={t("common.forbiddenDescription")} />
      );
    }
    return (
      <InlineAlert tone="danger">
        {t("common.loadError")}
        {" — "}
        <button type="button" className="button buttonGhost" onClick={() => query.refetch()}>
          {t("common.retry")}
        </button>
      </InlineAlert>
    );
  }

  const data = query.data as T;
  if (isEmpty?.(data)) {
    return <EmptyState title={t("common.empty")} />;
  }

  return <>{children(data)}</>;
}
