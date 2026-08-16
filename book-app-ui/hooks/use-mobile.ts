"use client";

import { useSyncExternalStore } from "react";

const MOBILE_BREAKPOINT_QUERY = "(max-width: 767px)";

function subscribe(callback: () => void) {
  const mediaQueryList = window.matchMedia(MOBILE_BREAKPOINT_QUERY);
  mediaQueryList.addEventListener("change", callback);
  return () => mediaQueryList.removeEventListener("change", callback);
}

function getSnapshot() {
  return window.matchMedia(MOBILE_BREAKPOINT_QUERY).matches;
}

// Server/first-paint snapshot is always "desktop" -- this app already gates
// all real content behind a client-only AuthGate with its own "resolving"
// state, so following the same client-resolves-after-mount pattern here
// (rather than server-side UA sniffing) keeps this consistent with the rest
// of the app's architecture.
function getServerSnapshot() {
  return false;
}

/** True once the viewport has been confirmed (client-side, via matchMedia)
 * to be mobile-width. useSyncExternalStore (not useState+useEffect) is the
 * correct primitive for subscribing to an external browser API like this --
 * it re-renders synchronously on change without the extra render pass an
 * effect-driven setState would cause. */
export function useIsMobile(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
