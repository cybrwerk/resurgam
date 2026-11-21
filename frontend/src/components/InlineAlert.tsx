import type { ReactNode } from "react";

import { CheckCircleIcon, InfoCircleIcon, WarningTriangleIcon } from "./icons";

type Tone = "danger" | "info" | "success";

interface InlineAlertProps {
  /** `danger` (default): something failed or needs attention. `info`: a
   * neutral notice. `success`: something was saved / is done. */
  tone?: Tone;
  /** Leave out the tone icon, e.g. when the message starts with a spinner. */
  hideIcon?: boolean;
  children: ReactNode;
}

const TONE_CLASS: Record<Tone, string> = {
  danger: "inlineAlert inlineAlertDanger",
  info: "inlineAlert inlineAlertInfo",
  success: "inlineAlert inlineAlertSuccess",
};

const TONE_ICON: Record<Tone, typeof WarningTriangleIcon> = {
  danger: WarningTriangleIcon,
  info: InfoCircleIcon,
  success: CheckCircleIcon,
};

export function InlineAlert({ tone = "danger", hideIcon = false, children }: InlineAlertProps) {
  const Icon = TONE_ICON[tone];
  return (
    <div className={TONE_CLASS[tone]} role={tone === "danger" ? "alert" : "status"}>
      {!hideIcon && <Icon className="inlineAlertIcon" aria-hidden="true" />}
      <span>{children}</span>
    </div>
  );
}
