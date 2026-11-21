import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation } from "react-router-dom";

import { findNavLocation } from "../nav/navTree";

import styles from "./PageHeader.module.css";

export interface Breadcrumb {
  label: string;
  to?: string;
}

interface PageHeaderProps {
  title: string;
  /** One-line explanation under the title -- replaces the ad-hoc muted
   * paragraphs pages used to render themselves. */
  description?: ReactNode;
  actions?: ReactNode;
  breadcrumbs?: Breadcrumb[];
}

/** Pages in the nav tree get their breadcrumb from it (the section, plus the
 * list page when this is a detail route under it); pages outside the tree
 * pass their own. */
function useNavBreadcrumbs(): Breadcrumb[] {
  const { t } = useTranslation();
  const { pathname } = useLocation();
  const location = findNavLocation(pathname);
  // A top-level page (e.g. Overview) would only repeat its own title.
  if (!location?.link) return [];
  const crumbs: Breadcrumb[] = [{ label: t(location.section.labelKey) }];
  if (location.link && location.link.path !== pathname) {
    crumbs.push({ label: t(location.link.labelKey), to: location.link.path });
  }
  return crumbs;
}

export function PageHeader({ title, description, actions, breadcrumbs: explicitBreadcrumbs }: PageHeaderProps) {
  const navBreadcrumbs = useNavBreadcrumbs();
  const breadcrumbs = explicitBreadcrumbs ?? navBreadcrumbs;
  return (
    <div className={styles.wrapper}>
      {breadcrumbs && breadcrumbs.length > 0 && (
        <nav className={styles.breadcrumbs} aria-label="Breadcrumb">
          {breadcrumbs.map((crumb, index) => (
            <span key={index}>
              {index > 0 && " / "}
              {crumb.to ? <Link to={crumb.to}>{crumb.label}</Link> : crumb.label}
            </span>
          ))}
        </nav>
      )}
      <div className={styles.header}>
        <h1 className={styles.title}>{title}</h1>
        {actions && <div className={styles.actions}>{actions}</div>}
      </div>
      {description && <p className={styles.description}>{description}</p>}
    </div>
  );
}
