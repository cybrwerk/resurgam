import type { TFunction } from "i18next";

import type { StatusTone } from "../components/StatusBadge";
import type { ConnectionHealthStatus, ProtectionStatus, TaskStatus, VerifyStatus } from "../api/types";

export function healthStatusPresentation(
  status: ConnectionHealthStatus,
  t: TFunction,
): { tone: StatusTone; label: string } {
  switch (status) {
    case "ok":
      return { tone: "ok", label: t("pbsInstances.healthOk") };
    case "degraded":
      return { tone: "warning", label: t("pbsInstances.healthDegraded") };
    case "unreachable":
      return { tone: "danger", label: t("pbsInstances.healthUnreachable") };
    case "tls_mismatch":
      return { tone: "danger", label: t("connection.healthTlsMismatch") };
    case "auth_failed":
      return { tone: "danger", label: t("connection.healthAuthFailed") };
    case "missing_privileges":
      return { tone: "warning", label: t("connection.healthMissingPrivileges") };
    default:
      return { tone: "unknown", label: t("pbsInstances.healthUnknown") };
  }
}

export function verifyStatusPresentation(status: VerifyStatus, t: TFunction): { tone: StatusTone; label: string } {
  switch (status) {
    case "ok":
      return { tone: "ok", label: t("pbsInstances.healthOk") };
    case "failed":
      return { tone: "danger", label: t("tasks.taskStatusError") };
    case "none":
      return { tone: "unknown", label: t("pbsInstances.healthUnknown") };
    default:
      return { tone: "unknown", label: t("pbsInstances.healthUnknown") };
  }
}

export function protectionStatusPresentation(
  status: ProtectionStatus,
  t: TFunction,
): { tone: StatusTone; label: string } {
  switch (status) {
    case "ok":
      return { tone: "ok", label: t("protectionPolicies.statusOk") };
    case "warning":
      return { tone: "warning", label: t("protectionPolicies.statusWarning") };
    case "critical":
      return { tone: "danger", label: t("protectionPolicies.statusCritical") };
    default:
      return { tone: "unknown", label: t("protectionPolicies.statusUnknown") };
  }
}

export function taskStatusPresentation(status: TaskStatus, t: TFunction): { tone: StatusTone; label: string } {
  switch (status) {
    case "ok":
      return { tone: "ok", label: t("tasks.taskStatusOk") };
    case "error":
      return { tone: "danger", label: t("tasks.taskStatusError") };
    case "running":
      return { tone: "info", label: t("tasks.taskStatusRunning") };
    default:
      return { tone: "unknown", label: t("tasks.taskStatusUnknown") };
  }
}
