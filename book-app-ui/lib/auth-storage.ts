/**
 * Client-side storage for the two access tokens this app understands:
 *  - owner token (localStorage, persists across visits) -- full read/write.
 *  - share token (sessionStorage, tab-scoped) -- read-only, picked up from
 *    a `?share=<token>` link.
 *
 * These are plain functions (not React state) so both the AuthProvider and
 * the non-React api-client fetch helper can read/write the same source of
 * truth without needing React context wired through every call site.
 */

const OWNER_TOKEN_KEY = "readerpro_owner_token";
const SHARE_TOKEN_KEY = "readerpro_share_token";

export type AccessRole = "owner" | "viewer" | null;

export function getOwnerToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(OWNER_TOKEN_KEY);
}

export function setOwnerToken(token: string) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(OWNER_TOKEN_KEY, token);
}

export function clearOwnerToken() {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(OWNER_TOKEN_KEY);
}

export function getShareToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.sessionStorage.getItem(SHARE_TOKEN_KEY);
}

export function setShareToken(token: string) {
  if (typeof window === "undefined") return;
  window.sessionStorage.setItem(SHARE_TOKEN_KEY, token);
}

export function getCurrentRole(): AccessRole {
  // Local-dev-only convenience, mirroring AUTH_DISABLED on the backend
  // (routers/deps.py): set NEXT_PUBLIC_AUTH_DISABLED=true in a local
  // .env.local (gitignored, never the deployed frontend's config) to skip
  // the login screen when browsing your own machine. The backend still
  // needs its own AUTH_DISABLED set for requests to actually succeed --
  // this only controls whether this app shows the login form.
  if (process.env.NEXT_PUBLIC_AUTH_DISABLED === "true") return "owner";
  if (getOwnerToken()) return "owner";
  if (getShareToken()) return "viewer";
  return null;
}

export function getAuthHeaders(): Record<string, string> {
  const ownerToken = getOwnerToken();
  if (ownerToken) {
    return { Authorization: `Bearer ${ownerToken}` };
  }
  const shareToken = getShareToken();
  if (shareToken) {
    return { "X-Share-Token": shareToken };
  }
  return {};
}
