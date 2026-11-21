import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { API_DOCS_URL, downloadOpenApiSchema } from "../api/client";
import { LanguageSelect } from "./LanguageSelect";
import { useAuth } from "../contexts/AuthContext";
import styles from "./AvatarMenu.module.css";

function initialsFor(displayName: string, email: string): string {
  // Letters only: "Break-glass (admin)" must give "BA", not "B(".
  const source = (displayName.trim() || email).replace(/[^\p{L}\p{N}\s]/gu, " ").trim() || "?";
  const parts = source.split(/\s+/).filter(Boolean);
  if (parts.length >= 2) {
    return (parts[0][0] + parts[1][0]).toUpperCase();
  }
  return source.slice(0, 2).toUpperCase();
}

/** Manifest §26.2's topbar "Benutzeraktion" slot: account info, profile and
 * logout. Language and theme are their own top-bar controls (Petrol & Brass
 * top bar, 2026-10-04). */
export function AvatarMenu() {
  const { t } = useTranslation();
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [downloadError, setDownloadError] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handleClick = (event: MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", handleClick);
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("mousedown", handleClick);
      document.removeEventListener("keydown", handleKey);
    };
  }, [open]);

  if (!user) return null;

  const handleLogout = () => {
    setOpen(false);
    logout();
    navigate("/login", { replace: true });
  };

  const handleDownloadOpenApi = () => {
    setDownloadError(false);
    downloadOpenApiSchema()
      .then(() => setOpen(false))
      .catch(() => setDownloadError(true));
  };

  const handleProfile = () => {
    setOpen(false);
    navigate("/profile");
  };

  return (
    <div className={styles.wrapper} ref={wrapperRef}>

      <button
        type="button"
        className={styles.trigger}
        onClick={() => {
          setDownloadError(false);
          setOpen((v) => !v);
        }}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t("avatarMenu.account")}
      >
        <span className={styles.avatarCircle} aria-hidden>
          {initialsFor(user.display_name, user.email)}
        </span>
      </button>

      {open && (
        <div className={styles.menu} role="menu">
          <div className={styles.accountBlock}>
            <div className={styles.accountName}>{user.display_name}</div>
            <div className={styles.accountEmail}>{user.email}</div>
          </div>

          {/* On phones the top bar has no room for the language select. */}
          <div className={styles.mobileLanguage}>
            <LanguageSelect className={`select ${styles.mobileLanguageSelect}`} />
          </div>
          <button type="button" className={styles.menuItem} role="menuitem" onClick={handleProfile}>
            {t("avatarMenu.profile")}
          </button>
          <div className={styles.menuDivider} role="separator" />
          <a
            className={styles.menuItem}
            role="menuitem"
            href={API_DOCS_URL}
            target="_blank"
            rel="noopener noreferrer"
            onClick={() => setOpen(false)}
          >
            {t("avatarMenu.apiDocs")}
          </a>
          <button type="button" className={styles.menuItem} role="menuitem" onClick={handleDownloadOpenApi}>
            {t("avatarMenu.downloadOpenApi")}
          </button>
          {downloadError && (
            <div className={styles.menuError} role="alert">
              {t("avatarMenu.downloadOpenApiFailed")}
            </div>
          )}
          <div className={styles.menuDivider} role="separator" />
          <button type="button" className={styles.menuItem} role="menuitem" onClick={handleLogout}>
            {t("avatarMenu.logout")}
          </button>
        </div>
      )}
    </div>
  );
}
