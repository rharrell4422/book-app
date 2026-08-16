"use client";

import { useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ToastProvider } from "@/components/ui/use-toast";
import { Toaster } from "@/components/ui/toaster";
import { AuthProvider } from "@/lib/auth-context";
import { ProfileProvider } from "@/lib/profile-context";
import { AuthGate } from "@/components/auth-gate";

export function Providers({ children }: { children: React.ReactNode }) {
  // Created once per mount via useState (not module scope) so each browser
  // tab/test gets its own cache instead of sharing one across requests --
  // the standard App Router pattern for client-side singletons like this.
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // The library data this backs (books/series) doesn't change from
            // other users mid-session -- avoid the default aggressive
            // refetch-on-window-focus behavior, which would otherwise re-pull
            // the whole (currently unpaginated) library every time this tab
            // regains focus.
            staleTime: 60_000,
            refetchOnWindowFocus: false,
          },
        },
      })
  );

  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <AuthProvider>
          <ProfileProvider>
            <AuthGate>{children}</AuthGate>
          </ProfileProvider>
        </AuthProvider>
        <Toaster />
      </ToastProvider>
    </QueryClientProvider>
  );
}

