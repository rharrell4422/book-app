"use client";

import { useSyncExternalStore } from "react";

/**
 * Keyboard overlap below the visual viewport, in CSS pixels.
 * Used by /add-book and /edit-book/[id] so the sticky Save bar stays above
 * the on-screen keyboard. Returns 0 when visualViewport is unavailable.
 */
function getBottomInset(): number {
  if (typeof window === "undefined") {
    return 0;
  }

  const visualViewport = window.visualViewport;
  if (!visualViewport) {
    return 0;
  }

  const inset = window.innerHeight - visualViewport.height - visualViewport.offsetTop;
  return Math.max(0, Math.round(inset));
}

function subscribe(callback: () => void) {
  const visualViewport = window.visualViewport;
  if (!visualViewport) {
    return () => {};
  }

  visualViewport.addEventListener("resize", callback);
  visualViewport.addEventListener("scroll", callback);
  window.addEventListener("resize", callback);
  return () => {
    visualViewport.removeEventListener("resize", callback);
    visualViewport.removeEventListener("scroll", callback);
    window.removeEventListener("resize", callback);
  };
}

export function useVisualViewportBottomInset(): number {
  return useSyncExternalStore(subscribe, getBottomInset, () => 0);
}
