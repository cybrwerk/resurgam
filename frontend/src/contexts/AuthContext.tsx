import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { authApi } from "../api/resources";
import { ApiError, setAuthToken, setUnauthorizedHandler } from "../api/client";
import type { WhoAmIResponse } from "../api/types";

const TOKEN_STORAGE_KEY = "resurgam.token";

interface AuthContextValue {
  token: string | null;
  user: WhoAmIResponse | null;
  isLoading: boolean;
  loginWithToken: (token: string) => Promise<void>;
  logout: () => void;
  hasRole: (role: string) => boolean;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

function readStoredToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

function storeToken(token: string | null): void {
  try {
    if (token) {
      window.localStorage.setItem(TOKEN_STORAGE_KEY, token);
    } else {
      window.localStorage.removeItem(TOKEN_STORAGE_KEY);
    }
  } catch {
    // best-effort only — an unpersisted session still works for this tab
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(readStoredToken);
  const [user, setUser] = useState<WhoAmIResponse | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(() => readStoredToken() !== null);

  const logout = () => {
    setToken(null);
    setUser(null);
    storeToken(null);
    setAuthToken(null);
    setIsLoading(false);
  };

  useEffect(() => {
    setUnauthorizedHandler(logout);
    return () => setUnauthorizedHandler(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    setAuthToken(token);
    if (!token) {
      setUser(null);
      setIsLoading(false);
      return;
    }

    let cancelled = false;
    setIsLoading(true);
    authApi
      .whoami()
      .then((whoami) => {
        if (!cancelled) {
          setUser(whoami);
        }
      })
      .catch((error) => {
        if (!cancelled && error instanceof ApiError) {
          logout();
        }
      })
      .finally(() => {
        if (!cancelled) {
          setIsLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const loginWithToken = async (newToken: string) => {
    storeToken(newToken);
    setToken(newToken);
  };

  const hasRole = (role: string): boolean => user?.assignments.some((a) => a.role === role) ?? false;

  const value = useMemo(
    () => ({ token, user, isLoading, loginWithToken, logout, hasRole }),
    [token, user, isLoading],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}
