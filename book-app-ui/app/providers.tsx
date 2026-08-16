"use client";

import { ToastProvider } from "@/components/ui/use-toast";
import { Toaster } from "@/components/ui/toaster";
import { AuthProvider } from "@/lib/auth-context";
import { ProfileProvider } from "@/lib/profile-context";
import { AuthGate } from "@/components/auth-gate";

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <ToastProvider>
      <AuthProvider>
        <ProfileProvider>
          <AuthGate>{children}</AuthGate>
        </ProfileProvider>
      </AuthProvider>
      <Toaster />
    </ToastProvider>
  );
}

