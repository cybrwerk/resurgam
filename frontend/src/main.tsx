import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";

import "./i18n";
import "./styles/tokens.css";
import "./styles/app.css";
import { App } from "./App";
import { AuthProvider } from "./contexts/AuthContext";
import { BrandingProvider } from "./contexts/BrandingContext";
import { CustomerProvider } from "./contexts/CustomerContext";
import { ThemeProvider } from "./contexts/ThemeContext";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: false,
      refetchOnWindowFocus: false,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <BrandingProvider>
          <BrowserRouter>
            <AuthProvider>
              <CustomerProvider>
                <App />
              </CustomerProvider>
            </AuthProvider>
          </BrowserRouter>
        </BrandingProvider>
      </ThemeProvider>
    </QueryClientProvider>
  </StrictMode>,
);
