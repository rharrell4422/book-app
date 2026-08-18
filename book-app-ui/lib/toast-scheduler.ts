/**
 * Pure helpers behind the toast system's auto-dismiss + accumulation cap.
 * Kept free of React so they're directly unit-testable; use-toast.tsx wires
 * these into its useState-backed toast list.
 */

export const TOAST_AUTO_DISMISS_MS = 2500;

/** Toasts visible at once -- older ones are dropped once a new one arrives. */
export const MAX_VISIBLE_TOASTS = 3;

export type ToastRecord = {
  id: string;
  title?: string;
  description?: string;
  action?: unknown;
};

/** Appends a toast, capping the list so toasts can no longer accumulate forever. */
export function addToastRecord<T extends ToastRecord>(toasts: T[], record: T): T[] {
  const next = [...toasts, record];
  return next.length > MAX_VISIBLE_TOASTS ? next.slice(next.length - MAX_VISIBLE_TOASTS) : next;
}

export function removeToastRecord<T extends ToastRecord>(toasts: T[], id: string): T[] {
  return toasts.filter((toast) => toast.id !== id);
}

/**
 * Schedules onDismiss to run after `ms` and returns a canceler. Isolates the
 * timer plumbing so auto-dismiss timing can be unit-tested with fake timers
 * without needing to render the ToastProvider/useToast React tree.
 */
export function scheduleToastAutoDismiss(onDismiss: () => void, ms: number = TOAST_AUTO_DISMISS_MS): () => void {
  const timeoutId = setTimeout(onDismiss, ms);
  return () => clearTimeout(timeoutId);
}
