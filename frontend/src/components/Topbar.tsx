import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { AvatarMenu } from "./AvatarMenu";
import { CustomerSwitcher } from "./CustomerSwitcher";
import { GlobalSearch } from "./GlobalSearch";
import { LanguageSelect } from "./LanguageSelect";
import { MenuIcon, MonitorIcon, MoonIcon, SunIcon } from "./icons";
import { useBranding } from "../contexts/BrandingContext";
import { useTheme, type ThemeMode } from "../contexts/ThemeContext";
import styles from "./Topbar.module.css";

const THEME_CYCLE: ThemeMode[] = ["system", "light", "dark"];
const THEME_ICON = { system: MonitorIcon, light: SunIcon, dark: MoonIcon } as const;
const THEME_LABEL_KEY = {
  system: "avatarMenu.themeSystem",
  light: "avatarMenu.themeLight",
  dark: "avatarMenu.themeDark",
} as const;

function ThemeToggle() {
  const { t } = useTranslation();
  const { mode, setMode } = useTheme();
  const next = THEME_CYCLE[(THEME_CYCLE.indexOf(mode) + 1) % THEME_CYCLE.length];
  const Icon = THEME_ICON[mode];
  const label = `${t("avatarMenu.theme")}: ${t(THEME_LABEL_KEY[mode])}`;
  return (
    <button type="button" className={styles.iconButton} onClick={() => setMode(next)} aria-label={label} title={label}>
      <Icon className={styles.icon} />
    </button>
  );
}

interface TopbarProps {
  navOpen: boolean;
  onToggleNav: () => void;
}

/** Petrol & Brass top bar: always deep petrol with a 2px brass underline.
 * Start: menu button (narrow screens) and brand; centre: global search;
 * end: tenant scope, language, theme and the signed-in user. */
export function Topbar({ navOpen, onToggleNav }: TopbarProps) {
  const { t } = useTranslation();
  const { logoConfigured, logoUrl, appName } = useBranding();

  return (
    <header className={styles.topbar}>
      <button
        type="button"
        className={`${styles.iconButton} ${styles.menuButton}`}
        onClick={onToggleNav}
        aria-expanded={navOpen}
        aria-label={t("nav.menu")}
      >
        <MenuIcon className={styles.icon} />
      </button>
      <Link to="/" className={styles.brand}>
        <img src={logoConfigured ? logoUrl : "/logo-mark.svg"} alt="" className={styles.brandMark} />
        <span className={styles.wordmark} title={appName ?? undefined}>
          {appName ?? "RESURGAM"}
          {!appName && <small>by cybrwerk</small>}
        </span>
      </Link>
      <div className={styles.search}>
        <GlobalSearch />
      </div>
      <div className={styles.tools}>
        <CustomerSwitcher />
        <LanguageSelect className={`${styles.darkSelect} ${styles.languageSelect}`} />
        <ThemeToggle />
        <AvatarMenu />
      </div>
    </header>
  );
}
