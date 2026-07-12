"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";

import { ApiError, fetchApiWithFallback } from "./api-client";
import {
  AccessRole,
  clearOwnerToken,
  getCurrentRole,
  getShareToken,
  setOwnerToken,
  setShareToken,
} from "./auth-storage";

type AuthContextValue = {
  role: AccessRole;
  ready: boolean;
  login: (password: string) => Promise<{ ok: boolean; error?: string }>;
  logout: () => void;
};

const AuthContext = createContext<AuthContextValue>({
  role: null,
  ready: false,
  login: async () => ({ ok: false, error: "Not initialized" }),
  logout: () => {},
});

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [role, setRole] = useState<AccessRole>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const shareParam = params.get("share");
    if (shareParam) {
      setShareToken(shareParam);
    }
    setRole(getCurrentRole());
    setReady(true);
  }, []);

  const login = useCallback(async (password: string) => {
    try {
      const response = await fetchApiWithFallback("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      const data = await response.json();
      setOwnerToken(data.token);
      setRole("owner");
      return { ok: true };
    } catch (error) {
      if (error instanceof ApiError) {
        return { ok: false, error: error.message };
      }
      return { ok: false, error: "Couldn't reach the server. Please try again." };
    }
  }, []);

  const logout = useCallback(() => {
    clearOwnerToken();
    setRole(getShareToken() ? "viewer" : null);
  }, []);

  return (
    <AuthContext.Provider value={{ role, ready, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
