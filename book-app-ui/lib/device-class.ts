export type DeviceClass = "phone" | "tablet" | "desktop";

const PHONE_QUERY = "(max-width: 767px)";
const COARSE_POINTER_QUERY = "(pointer: coarse)";
const NO_HOVER_QUERY = "(hover: none)";

/**
 * Viewport + primary pointer/hover classifier (no user-agent).
 * Phone: width < 768.
 * Tablet: width >= 768 and (coarse pointer OR hover: none) — iPad portrait
 * and landscape land here.
 * Desktop: width >= 768 with a fine pointer and hover.
 */
export function getDeviceClass(): DeviceClass {
  if (typeof window === "undefined") {
    return "desktop";
  }

  if (window.matchMedia(PHONE_QUERY).matches) {
    return "phone";
  }

  const isCoarsePointer = window.matchMedia(COARSE_POINTER_QUERY).matches;
  const hasNoHover = window.matchMedia(NO_HOVER_QUERY).matches;
  if (isCoarsePointer || hasNoHover) {
    return "tablet";
  }

  return "desktop";
}

export const DEVICE_CLASS_MEDIA_QUERIES = [PHONE_QUERY, COARSE_POINTER_QUERY, NO_HOVER_QUERY] as const;
