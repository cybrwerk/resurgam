import type { ReactNode } from "react";

interface PanelProps {
  /** Shown in the panel's title strip; omit for a plain framed box. */
  title?: ReactNode;
  /** Right-aligned in the title strip (e.g. an "Edit" button or a status). */
  actions?: ReactNode;
  /** Pad the body (default). Turn off when the body is a table or list
   * that should run edge to edge. */
  padded?: boolean;
  className?: string;
  testId?: string;
  children: ReactNode;
}

/** OPNsense-style content panel (2026-10-04): a title strip with optional
 * actions, then the body. The one container pages use for a group of
 * settings, a detail block or an embedded table, so every page reads the
 * same way. */
export function Panel({ title, actions, padded = true, className, testId, children }: PanelProps) {
  return (
    <section className={className ? `panel ${className}` : "panel"} data-testid={testId}>
      {(title || actions) && (
        <header className="panelHeader">
          {title && <h2 className="panelTitle">{title}</h2>}
          {actions && <div className="panelActions">{actions}</div>}
        </header>
      )}
      {padded ? <div className="panelBody">{children}</div> : children}
    </section>
  );
}
