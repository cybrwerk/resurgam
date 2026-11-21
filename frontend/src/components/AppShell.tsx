import { useEffect, useState } from "react";
import { Outlet, useLocation } from "react-router-dom";

import { PrimaryNavigation } from "./PrimaryNavigation";
import { Topbar } from "./Topbar";
import styles from "./AppShell.module.css";

/** Petrol & Brass frame: a full-width top bar, the side navigation below it
 * on the inline-start side, content beside it. Under 760px the navigation
 * slides in behind the top bar's menu button. */
export function AppShell() {
  const [navOpen, setNavOpen] = useState(false);
  const { pathname } = useLocation();

  // Following a link closes the slide-in navigation.
  useEffect(() => setNavOpen(false), [pathname]);

  return (
    <div className={styles.shell}>
      <Topbar navOpen={navOpen} onToggleNav={() => setNavOpen((open) => !open)} />
      <div className={styles.body}>
        <PrimaryNavigation mobileOpen={navOpen} />
        {navOpen && <div className={styles.scrim} onClick={() => setNavOpen(false)} aria-hidden="true" />}
        <main className={styles.content}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
