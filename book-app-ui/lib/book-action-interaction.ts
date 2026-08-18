import type { DeviceClass } from "@/lib/device-class";

/** Matches the icon-xs/icon-sm/icon-md size variants on <Button>. */
export type BookActionIconSize = "icon-xs" | "icon-sm" | "icon-md";

const DEFAULT_ICON_SIZE_BY_DEVICE_CLASS: Record<DeviceClass, BookActionIconSize> = {
  desktop: "icon-xs",
  tablet: "icon-sm",
  phone: "icon-md",
};

/** Default touch-target size per device class -- callers can still override via the `size` prop. */
export function getDefaultBookActionIconSize(deviceClass: DeviceClass): BookActionIconSize {
  return DEFAULT_ICON_SIZE_BY_DEVICE_CLASS[deviceClass];
}

export type BookActionTapPlan =
  | { kind: "disabled" }
  | { kind: "immediate" }
  | { kind: "delayed"; delayMs: number };

export const DESTRUCTIVE_ACTION_DELAY_MS = 500;

/**
 * Decides what a tap should do on phone/tablet, given whether the action is
 * a navigation action (immediate, no toast) or a destructive/ambiguous one
 * (toast + short delay before firing). Desktop never calls this -- desktop
 * always fires immediately behind a hover/focus tooltip instead.
 */
export function resolvePhoneOrTabletTapPlan({
  delayed,
  disabled,
}: {
  delayed: boolean;
  disabled: boolean;
}): BookActionTapPlan {
  if (disabled) return { kind: "disabled" };
  if (!delayed) return { kind: "immediate" };
  return { kind: "delayed", delayMs: DESTRUCTIVE_ACTION_DELAY_MS };
}
