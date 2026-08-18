"use client";

import { useSyncExternalStore } from "react";

import { DEVICE_CLASS_MEDIA_QUERIES, getDeviceClass, type DeviceClass } from "@/lib/device-class";

function subscribe(callback: () => void) {
  const mediaQueryLists = DEVICE_CLASS_MEDIA_QUERIES.map((query) => window.matchMedia(query));
  for (const mediaQueryList of mediaQueryLists) {
    mediaQueryList.addEventListener("change", callback);
  }
  return () => {
    for (const mediaQueryList of mediaQueryLists) {
      mediaQueryList.removeEventListener("change", callback);
    }
  };
}

function getSnapshot(): DeviceClass {
  return getDeviceClass();
}

// Same client-resolves-after-mount pattern as useIsMobile(): server/first
// paint is desktop-safe so we never auto-open a phone/tablet surface on SSR.
function getServerSnapshot(): DeviceClass {
  return "desktop";
}

/** Device class from viewport width + primary pointer/hover (not user-agent). */
export function useDeviceClass(): DeviceClass {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
