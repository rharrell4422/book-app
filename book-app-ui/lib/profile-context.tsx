"use client";

import { useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

import { fetchApiWithFallback } from "./api-client";
import { useAuth } from "./auth-context";
import { getStoredProfileId, setStoredProfileId } from "./profile-storage";

export type Profile = {
  id: string;
  display_name: string;
  is_default: boolean;
  book_count: number;
  has_data: boolean;
  last_full_discovery_run_at?: string | null;
};

type ProfileContextValue = {
  profileId: string | null;
  profiles: Profile[];
  activeProfile: Profile | null;
  ready: boolean;
  setProfileId: (id: string) => void;
  refreshProfiles: () => Promise<Profile[]>;
  createProfile: (id: string, displayName: string) => Promise<Profile>;
  renameProfile: (id: string, displayName: string) => Promise<Profile>;
  resetActiveProfileLibrary: () => Promise<void>;
};

const ProfileContext = createContext<ProfileContextValue>({
  profileId: null,
  profiles: [],
  activeProfile: null,
  ready: false,
  setProfileId: () => {},
  refreshProfiles: async () => [],
  createProfile: async () => {
    throw new Error("ProfileProvider not mounted");
  },
  renameProfile: async () => {
    throw new Error("ProfileProvider not mounted");
  },
  resetActiveProfileLibrary: async () => {
    throw new Error("ProfileProvider not mounted");
  },
});

export function ProfileProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const { role, ready: authReady } = useAuth();
  const [profileId, setProfileIdState] = useState<string | null>(null);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [ready, setReady] = useState(false);

  // Tracks the previously-active profile without making setProfileId
  // depend on (and re-create every time) `profileId` itself.
  const profileIdRef = useRef<string | null>(null);
  useEffect(() => {
    profileIdRef.current = profileId;
  }, [profileId]);

  const setProfileId = useCallback(
    (id: string) => {
      const isSwitchingToADifferentProfile = profileIdRef.current !== null && profileIdRef.current !== id;
      setStoredProfileId(id);
      setProfileIdState(id);
      if (isSwitchingToADifferentProfile) {
        // Series/book ids aren't shared across profiles -- staying on a
        // series or book detail route after switching would keep that
        // route's id mounted under the *new* profile and try to load data
        // that either doesn't exist there or belongs to someone else
        // entirely (live bug: switching profiles mid-troubleshooting left
        // the series detail page open, showing stale/wrong data with no
        // indication the profile had even changed). The main library is
        // the one page guaranteed to make sense for any profile.
        router.push("/books");
      }
    },
    [router]
  );

  const refreshProfiles = useCallback(async () => {
    try {
      const response = await fetchApiWithFallback("/profiles");
      const fetchedProfiles: Profile[] = await response.json();
      setProfiles(fetchedProfiles);
      return fetchedProfiles;
    } catch {
      // A failed /profiles fetch shouldn't block the rest of the app --
      // requests still work without an X-Profile-Id header (the backend
      // falls back to the default profile), so just leave the switcher
      // empty rather than showing a hard error.
      return profiles;
    }
  }, [profiles]);

  const createProfile = useCallback(
    async (id: string, displayName: string) => {
      const response = await fetchApiWithFallback("/profiles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id, display_name: displayName }),
      });
      const created: Profile = await response.json();
      await refreshProfiles();
      setProfileId(created.id);
      return created;
    },
    [refreshProfiles, setProfileId]
  );

  const renameProfile = useCallback(
    async (id: string, displayName: string) => {
      const response = await fetchApiWithFallback(`/profiles/${encodeURIComponent(id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_name: displayName }),
      });
      const updated: Profile = await response.json();
      await refreshProfiles();
      return updated;
    },
    [refreshProfiles]
  );

  const resetActiveProfileLibrary = useCallback(async () => {
    // Targets whichever profile is active server-side (X-Profile-Id header,
    // see profile-storage.ts) -- callers must only surface this from a spot
    // where the currently-selected profile is unambiguous (the profile
    // menu), same caution as the backend route's own docstring.
    await fetchApiWithFallback("/import/reset_profile", { method: "POST" });
    await refreshProfiles();
  }, [refreshProfiles]);

  useEffect(() => {
    if (!authReady || !role) return;

    let cancelled = false;

    async function hydrate() {
      let fetchedProfiles: Profile[] = [];
      try {
        const response = await fetchApiWithFallback("/profiles");
        fetchedProfiles = await response.json();
      } catch {
        // See refreshProfiles() above for why this fails open.
      }
      if (cancelled) return;
      setProfiles(fetchedProfiles);

      // A read-only share link can pin the viewer to a specific profile
      // via `?profile=<id>` (see ShareLinkButton) -- read directly from the
      // URL here, independent of auth-context's own URL hydration, so this
      // doesn't depend on which provider's mount effect runs first.
      const params = new URLSearchParams(window.location.search);
      const profileParam = params.get("profile");
      const profileParamIsValid = Boolean(profileParam) && fetchedProfiles.some((p) => p.id === profileParam);

      const resolvedId =
        (profileParamIsValid ? profileParam : null) ||
        getStoredProfileId() ||
        fetchedProfiles.find((p) => p.is_default)?.id ||
        fetchedProfiles[0]?.id ||
        null;

      if (resolvedId) {
        setStoredProfileId(resolvedId);
        setProfileIdState(resolvedId);
      }
      setReady(true);
    }

    hydrate();
    return () => {
      cancelled = true;
    };
  }, [authReady, role]);

  const activeProfile = profiles.find((p) => p.id === profileId) ?? null;

  return (
    <ProfileContext.Provider
      value={{
        profileId,
        profiles,
        activeProfile,
        ready,
        setProfileId,
        refreshProfiles,
        createProfile,
        renameProfile,
        resetActiveProfileLibrary,
      }}
    >
      {children}
    </ProfileContext.Provider>
  );
}

export function useProfile() {
  return useContext(ProfileContext);
}
