import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { NavLink, useLocation } from "react-router-dom";

import { ChevronRightIcon, SidebarToggleIcon } from "./icons";
import { useAuth } from "../contexts/AuthContext";
import { NAV_TREE, findNavLocation } from "../nav/navTree";
import styles from "./PrimaryNavigation.module.css";

const RAIL_STORAGE_KEY = "resurgam.navRail";

function readRail(): boolean {
  try {
    return window.localStorage.getItem(RAIL_STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

function storeRail(rail: boolean): void {
  try {
    window.localStorage.setItem(RAIL_STORAGE_KEY, String(rail));
  } catch {
    // best-effort persistence only
  }
}

/** OPNsense-style sidebar, styled per Petrol & Brass's SideNav (2026-10-04):
 * deep-petrol ground below the top bar; every section shows its icon, and
 * only one section is open at a time -- the one holding the current page,
 * unless the user opens another. The previous "every section expanded,
 * collapse state remembered per section" tree listed all 33 entries at
 * once and ran past the bottom of the screen. The sidebar can also shrink
 * to an icon rail, where each section's entries open as a flyout on hover
 * or keyboard focus. */
interface PrimaryNavigationProps {
  /** Narrow screens (<760px): whether the slide-in navigation is shown. */
  mobileOpen?: boolean;
}

export function PrimaryNavigation({ mobileOpen = false }: PrimaryNavigationProps) {
  const { t } = useTranslation();
  const { hasRole } = useAuth();
  const location = useLocation();
  const activeSectionKey = findNavLocation(location.pathname)?.section.labelKey ?? null;
  const [openSection, setOpenSection] = useState<string | null>(activeSectionKey);
  const [rail, setRail] = useState<boolean>(readRail);

  // Navigating into another section (links, global search, back button)
  // opens that section, so the current page is never hidden in the menu.
  useEffect(() => {
    if (activeSectionKey) setOpenSection(activeSectionKey);
  }, [activeSectionKey]);

  // A real UX-audit finding (2026-09-23): every nav entry showed for
  // every signed-in user regardless of role, including sections whose
  // every real action 403s for anyone but a platform_admin -- see
  // `NavSection.requiresRole`'s own docstring in navTree.ts for exactly
  // which backend gates this mirrors.
  const visibleSections = NAV_TREE.filter((section) => !section.requiresRole || hasRole(section.requiresRole));

  const toggleRail = () => {
    setRail((prev) => {
      storeRail(!prev);
      return !prev;
    });
  };

  const childClassName = ({ isActive }: { isActive: boolean }) =>
    isActive ? `${styles.childLink} ${styles.childLinkActive}` : styles.childLink;

  return (
    <nav
      className={[styles.nav, rail ? styles.rail : "", mobileOpen ? styles.mobileOpen : ""].join(" ")}
      aria-label="Primary"
    >

      <ul className={styles.sections}>
        {visibleSections.map((section) => {
          const Icon = section.icon;
          const label = t(section.labelKey);
          const isActiveSection = section.labelKey === activeSectionKey;

          if (section.path) {
            return (
              <li key={section.labelKey} className={styles.sectionItem}>
                <NavLink
                  to={section.path}
                  end
                  title={rail ? label : undefined}
                  className={({ isActive }) =>
                    isActive ? `${styles.sectionHeader} ${styles.sectionHeaderActive}` : styles.sectionHeader
                  }
                >
                  <Icon className={styles.sectionIcon} />
                  <span className={styles.sectionLabel}>{label}</span>
                </NavLink>
              </li>
            );
          }

          const isOpen = openSection === section.labelKey;
          const listId = `nav-section-${section.labelKey.replace(/\W/g, "-")}`;
          return (
            <li key={section.labelKey} className={styles.sectionItem}>
              <button
                type="button"
                className={[
                  styles.sectionHeader,
                  isActiveSection ? styles.sectionHeaderActive : "",
                ].join(" ")}
                onClick={() => setOpenSection((prev) => (prev === section.labelKey ? null : section.labelKey))}
                aria-expanded={rail ? undefined : isOpen}
                aria-controls={listId}
              >
                <Icon className={styles.sectionIcon} />
                <span className={styles.sectionLabel}>{label}</span>
                <ChevronRightIcon className={isOpen ? styles.chevronOpen : styles.chevron} />
              </button>
              {/* Rendered whenever open in the full sidebar; always rendered
                  in the rail, where CSS shows it as a hover/focus flyout. */}
              {(isOpen || rail) && (
                <ul id={listId} className={styles.children}>
                  {rail && (
                    <li className={styles.flyoutTitle} aria-hidden="true">
                      {label}
                    </li>
                  )}
                  {section.children?.map((child) => (
                    <li key={child.path}>
                      <NavLink to={child.path} className={childClassName}>
                        {t(child.labelKey)}
                      </NavLink>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          );
        })}
      </ul>

      <button
        type="button"
        className={styles.railToggle}
        onClick={toggleRail}
        aria-pressed={rail}
        title={rail ? t("nav.expandSidebar") : t("nav.collapseSidebar")}
      >
        <SidebarToggleIcon className={styles.sectionIcon} />
        <span className={styles.sectionLabel}>{rail ? t("nav.expandSidebar") : t("nav.collapseSidebar")}</span>
      </button>
    </nav>
  );
}
