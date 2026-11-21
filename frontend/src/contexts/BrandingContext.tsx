import { createContext, useContext, useEffect, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";

import { brandingApi } from "../api/resources";
import type { BrandingSettingsOut } from "../api/types";

export const DEFAULT_APP_NAME = "RESURGAM";
const DEFAULT_DOCUMENT_TITLE = "RESURGAM Backup Center";

interface BrandingContextValue {
  logoConfigured: boolean;
  logoUrl: string;
  /** The white-label product name, or null for RESURGAM's own lockup. */
  appName: string | null;
}

const BrandingContext = createContext<BrandingContextValue>({ logoConfigured: false, logoUrl: "", appName: null });

/* Petrol & Brass defines separate shades per theme and role (a lighter
   petrol on dark grounds, a darker brass for brass text, the deep nav
   petrol, ...). A single admin-picked colour has no such ramp, so the
   variants are derived from it -- each mix lands on the Petrol & Brass
   value when given the default colour. tokens.css falls back to the exact
   Petrol & Brass values whenever these are absent. */
const PRIMARY_VARIANTS: Record<string, (c: string) => string> = {
  "--brand-primary": (c) => c,
  "--brand-primary-on-dark": (c) => `color-mix(in srgb, ${c} 55%, #ffffff)`,
  "--brand-primary-soft": (c) => `color-mix(in srgb, ${c} 12%, #ffffff)`,
  "--brand-primary-soft-dark": (c) => `color-mix(in srgb, ${c} 30%, #0f1718)`,
  "--brand-nav-bg": (c) => `color-mix(in srgb, ${c} 60%, #000000)`,
  "--brand-nav-bg-2": (c) => `color-mix(in srgb, ${c} 74%, #000000)`,
  "--brand-nav-bg-dark": (c) => `color-mix(in srgb, ${c} 26%, #050c0d)`,
  "--brand-nav-bg-2-dark": (c) => `color-mix(in srgb, ${c} 36%, #050c0d)`,
};

const ACCENT_VARIANTS: Record<string, (c: string) => string> = {
  "--brand-accent": (c) => c,
  "--brand-accent-text": (c) => `color-mix(in srgb, ${c} 62%, #000000)`,
  "--brand-accent-on-dark": (c) => `color-mix(in srgb, ${c} 70%, #ffffff)`,
};

function applyVariants(variants: Record<string, (c: string) => string>, colour: string | null | undefined): void {
  const root = document.documentElement;
  for (const [name, derive] of Object.entries(variants)) {
    if (colour) {
      root.style.setProperty(name, derive(colour));
    } else {
      root.style.removeProperty(name);
    }
  }
}

function applyBranding(data: BrandingSettingsOut | undefined): void {
  applyVariants(PRIMARY_VARIANTS, data?.primary_color);
  applyVariants(ACCENT_VARIANTS, data?.accent_color);
  document.title = data?.app_name || DEFAULT_DOCUMENT_TITLE;
}

/** Administration -> System Settings, confirmed per-instance (not
 * per-customer) white-label branding -- one logo, one primary color, one
 * accent color, shown to everyone including the pre-login page. Mirrors
 * `ThemeContext`'s shape: a query fetched once, applied as CSS custom
 * properties on the root element. `GET /branding` is deliberately
 * unauthenticated on the backend, so this works identically whether or
 * not the viewer is signed in yet.
 */
export function BrandingProvider({ children }: { children: ReactNode }) {
  const { data } = useQuery({ queryKey: ["branding"], queryFn: brandingApi.get });

  useEffect(() => {
    applyBranding(data);
  }, [data]);

  const value: BrandingContextValue = {
    logoConfigured: data?.logo_configured ?? false,
    logoUrl: brandingApi.logoUrl(),
    appName: data?.app_name || null,
  };

  return <BrandingContext.Provider value={value}>{children}</BrandingContext.Provider>;
}

export function useBranding(): BrandingContextValue {
  return useContext(BrandingContext);
}
