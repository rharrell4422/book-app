/**
 * Client-side storage for the active profile (library) id.
 *
 * Profiles are data separation, not an access-control boundary (see
 * auth-storage.ts for the actual owner/share tokens) -- this is just "which
 * library is currently selected", persisted in localStorage like the owner
 * token so it survives across visits/tabs. A plain function module (not
 * React state) for the same reason auth-storage.ts is: the non-React
 * api-client fetch helper needs to read it too, without React context
 * wired through every call site.
 */

const CURRENT_PROFILE_KEY = "readerpro_current_profile_id";

export function getStoredProfileId(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(CURRENT_PROFILE_KEY);
}

export function setStoredProfileId(profileId: string) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(CURRENT_PROFILE_KEY, profileId);
}

export function getProfileHeaders(): Record<string, string> {
  const profileId = getStoredProfileId();
  return profileId ? { "X-Profile-Id": profileId } : {};
}
