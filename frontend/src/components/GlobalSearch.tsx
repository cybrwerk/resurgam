import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { customersApi } from "../api/resources";
import { useAuth } from "../contexts/AuthContext";
import { NAV_TREE } from "../nav/navTree";
import { SearchIcon } from "./icons";
import styles from "./GlobalSearch.module.css";

interface SearchResult {
  key: string;
  label: string;
  group: string;
  path: string;
}

/** Raised directly by the user, "a global search for functions" -- a
 * command-palette over the same privilege-filtered nav tree
 * `PrimaryNavigation` renders (nothing surfaced here that section
 * wouldn't show for this role either) plus a jump straight to any
 * customer's panel by name, so switching tenants doesn't require the
 * sidebar dropdown's own scroll-and-click.
 */
export function GlobalSearch() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { hasRole } = useAuth();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLDialogElement>(null);

  const customersQuery = useQuery({ queryKey: ["customers"], queryFn: customersApi.list, enabled: open });

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setOpen(true);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.showModal();
      setQuery("");
      setActiveIndex(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  const navResults: SearchResult[] = useMemo(() => {
    const results: SearchResult[] = [];
    for (const section of NAV_TREE) {
      if (section.requiresRole && !hasRole(section.requiresRole)) continue;
      if (section.path) {
        results.push({ key: section.path, label: t(section.labelKey), group: t("globalSearch.groupPages"), path: section.path });
        continue;
      }
      for (const child of section.children ?? []) {
        results.push({ key: child.path, label: t(child.labelKey), group: t(section.labelKey), path: child.path });
      }
    }
    return results;
  }, [t, hasRole]);

  const customerResults: SearchResult[] = useMemo(
    () =>
      (customersQuery.data ?? []).map((customer) => ({
        key: `customer-${customer.id}`,
        label: customer.name,
        group: t("globalSearch.groupCustomers"),
        path: `/customers/${customer.id}/panel`,
      })),
    [customersQuery.data, t],
  );

  const results = useMemo(() => {
    const term = query.trim().toLowerCase();
    const all = [...navResults, ...customerResults];
    if (term === "") return all;
    return all.filter((result) => result.label.toLowerCase().includes(term) || result.group.toLowerCase().includes(term));
  }, [navResults, customerResults, query]);

  const close = () => setOpen(false);

  const goTo = (result: SearchResult) => {
    navigate(result.path);
    close();
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((i) => Math.min(i + 1, results.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((i) => Math.max(i - 1, 0));
    } else if (event.key === "Enter") {
      event.preventDefault();
      const result = results[activeIndex];
      if (result) goTo(result);
    }
  };

  return (
    <>
      <button type="button" className={styles.trigger} onClick={() => setOpen(true)}>
        <SearchIcon />
        <span>{t("globalSearch.trigger")}</span>
        <kbd className={styles.kbd}>{navigator.platform.includes("Mac") ? "⌘K" : "Ctrl+K"}</kbd>
      </button>

      <dialog ref={dialogRef} className={styles.dialog} onCancel={close} onClose={close} aria-label={t("globalSearch.trigger")}>
        <div className={styles.searchRow}>
          <SearchIcon className={styles.searchIcon} />
          <input
            ref={inputRef}
            type="text"
            className={styles.input}
            placeholder={t("globalSearch.placeholder")}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setActiveIndex(0);
            }}
            onKeyDown={handleKeyDown}
            aria-label={t("globalSearch.placeholder")}
          />
        </div>
        {results.length === 0 ? (
          <p className={`mutedText ${styles.empty}`}>{t("globalSearch.noResults")}</p>
        ) : (
          <ul className={styles.results}>
            {results.map((result, index) => (
              <li key={result.key}>
                <button
                  type="button"
                  className={`${styles.resultButton} ${index === activeIndex ? styles.resultActive : ""}`}
                  onMouseEnter={() => setActiveIndex(index)}
                  onClick={() => goTo(result)}
                >
                  <span className={styles.resultGroup}>{result.group}</span>
                  <span className={styles.resultLabel}>{result.label}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </dialog>
    </>
  );
}
