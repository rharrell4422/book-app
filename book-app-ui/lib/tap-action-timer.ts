/**
 * Pure delay controller behind the phone/tablet "toast, then fire" pattern
 * for destructive/ambiguous actions (mark read/unread, delete). Kept free of
 * React so it's directly unit-testable with fake timers.
 *
 * Tapping again while a fire is pending cancels the pending one and starts
 * a fresh delay from zero, rather than stacking timers or firing twice.
 */
export type TapActionTimer = {
  /** Start (or restart, if already pending) the delay before onFire runs. */
  trigger: () => void;
  /** Cancel a pending fire without starting a new one. Safe to call anytime. */
  cancel: () => void;
  /** Whether a fire is currently pending. */
  isPending: () => boolean;
};

export function createTapActionTimer({
  delayMs,
  onFire,
}: {
  delayMs: number;
  onFire: () => void;
}): TapActionTimer {
  let timeoutId: ReturnType<typeof setTimeout> | null = null;

  function cancel() {
    if (timeoutId !== null) {
      clearTimeout(timeoutId);
      timeoutId = null;
    }
  }

  function trigger() {
    cancel();
    timeoutId = setTimeout(() => {
      timeoutId = null;
      onFire();
    }, delayMs);
  }

  function isPending() {
    return timeoutId !== null;
  }

  return { trigger, cancel, isPending };
}
