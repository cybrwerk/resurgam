import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

const STORAGE_KEY = "resurgam.selectedCustomerId";

interface CustomerContextValue {
  /** Null means "no customer selected" -- the platform-wide view. A
   * per-viewer convenience (which tenant you were last looking at),
   * not shared state, so plain localStorage is the right place for it. */
  selectedCustomerId: string | null;
  setSelectedCustomerId: (id: string | null) => void;
}

const CustomerContext = createContext<CustomerContextValue | undefined>(undefined);

function readStoredCustomerId(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function storeCustomerId(id: string | null): void {
  try {
    if (id) {
      window.localStorage.setItem(STORAGE_KEY, id);
    } else {
      window.localStorage.removeItem(STORAGE_KEY);
    }
  } catch {
    // best-effort persistence only
  }
}

export function CustomerProvider({ children }: { children: ReactNode }) {
  const [selectedCustomerId, setSelectedCustomerIdState] = useState<string | null>(readStoredCustomerId);

  const setSelectedCustomerId = (id: string | null) => {
    setSelectedCustomerIdState(id);
    storeCustomerId(id);
  };

  const value = useMemo(() => ({ selectedCustomerId, setSelectedCustomerId }), [selectedCustomerId]);

  return <CustomerContext.Provider value={value}>{children}</CustomerContext.Provider>;
}

export function useSelectedCustomer(): CustomerContextValue {
  const context = useContext(CustomerContext);
  if (!context) {
    throw new Error("useSelectedCustomer must be used within a CustomerProvider");
  }
  return context;
}
