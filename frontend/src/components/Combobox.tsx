import { useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ChevronDownIcon } from "./icons";
import styles from "./Combobox.module.css";

export interface ComboboxOption {
  value: string;
  label: string;
}

interface ComboboxProps {
  id?: string;
  options: ComboboxOption[];
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  disabled?: boolean;
  /** Shown in the dropdown instead of the filtered list — for "options
   * are still loading" vs. "nothing matches your search", which need
   * different wording. */
  loadingLabel?: string;
}

/** A type-to-filter dropdown for a `<select>` whose option count doesn't
 * scale — real data surfaced this directly: `FileRecoveryPage`'s backup
 * group picker can have 90+ real workloads in one flat list, which a
 * plain `<select>` makes tedious to scan. Controlled the same way a
 * `<select>` is (`value`/`onChange` carry the selected option's
 * `value`), so it drops into existing picker call sites without
 * changing how the parent tracks selection. */
export function Combobox({ id, options, value, onChange, placeholder, disabled, loadingLabel }: ComboboxProps) {
  const { t } = useTranslation();
  const generatedId = useId();
  const inputId = id ?? generatedId;
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const selected = options.find((o) => o.value === value) ?? null;
  const [query, setQuery] = useState(selected?.label ?? "");
  const [isOpen, setIsOpen] = useState(false);
  const [highlightedIndex, setHighlightedIndex] = useState(0);

  // Keep the displayed text in sync when the selection changes from
  // outside (a parent resetting it, or `options` finishing loading).
  useEffect(() => {
    if (!isOpen) setQuery(selected?.label ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, selected?.label]);

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setIsOpen(false);
        setQuery(selected?.label ?? "");
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.label]);

  const filtered =
    isOpen && query.trim() !== "" && query !== selected?.label
      ? options.filter((o) => o.label.toLowerCase().includes(query.toLowerCase()))
      : options;

  const selectOption = (option: ComboboxOption) => {
    onChange(option.value);
    setQuery(option.label);
    setIsOpen(false);
    inputRef.current?.blur();
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (!isOpen && (event.key === "ArrowDown" || event.key === "Enter")) {
      setIsOpen(true);
      setHighlightedIndex(0);
      event.preventDefault();
      return;
    }
    if (!isOpen) return;

    if (event.key === "ArrowDown") {
      setHighlightedIndex((i) => Math.min(i + 1, filtered.length - 1));
      event.preventDefault();
    } else if (event.key === "ArrowUp") {
      setHighlightedIndex((i) => Math.max(i - 1, 0));
      event.preventDefault();
    } else if (event.key === "Enter") {
      const option = filtered[highlightedIndex];
      if (option) selectOption(option);
      event.preventDefault();
    } else if (event.key === "Escape") {
      setIsOpen(false);
      setQuery(selected?.label ?? "");
    }
  };

  const listboxId = `${inputId}-listbox`;

  return (
    <div className={styles.root} ref={rootRef}>
      <div className={styles.inputWrapper}>
        <input
          ref={inputRef}
          id={inputId}
          role="combobox"
          aria-expanded={isOpen}
          aria-controls={listboxId}
          aria-autocomplete="list"
          className={`input ${styles.input}`}
          placeholder={placeholder}
          disabled={disabled}
          value={query}
          onFocus={() => {
            setIsOpen(true);
            setHighlightedIndex(0);
          }}
          onChange={(e) => {
            setQuery(e.target.value);
            setIsOpen(true);
            setHighlightedIndex(0);
            if (e.target.value === "") onChange("");
          }}
          onKeyDown={handleKeyDown}
        />
        <ChevronDownIcon className={styles.chevron} />
      </div>
      {isOpen && !disabled && (
        <ul id={listboxId} role="listbox" className={styles.listbox}>
          {options.length === 0 ? (
            <li className={styles.empty}>{loadingLabel ?? t("common.loading")}</li>
          ) : filtered.length === 0 ? (
            <li className={styles.empty}>{t("common.empty")}</li>
          ) : (
            filtered.map((option, index) => (
              <li
                key={option.value}
                role="option"
                aria-selected={option.value === value}
                className={`${styles.option} ${index === highlightedIndex ? styles.optionHighlighted : ""}`}
                onMouseDown={(e) => e.preventDefault()}
                onMouseEnter={() => setHighlightedIndex(index)}
                onClick={() => selectOption(option)}
              >
                {option.label}
              </li>
            ))
          )}
        </ul>
      )}
    </div>
  );
}
