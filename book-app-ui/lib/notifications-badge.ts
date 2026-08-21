"use client";

/**
 * Unread-count badge for the Notifications bell/entry in the app shell
 * (see components/auth-gate.tsx's TopBar and MobileProfileSheet). Polls
 * GET /notifications/unseen on an interval plus whenever
 * refreshNotificationsBadgeCount() is called -- the Notifications page
 * calls that after a dismiss/dismiss-all so the badge updates immediately
 * instead of waiting for the next poll, same pattern the old
 * triggerNewBooksCheck() event used for the now-retired popup.
 */

import { useEffect, useState } from "react";

import { fetchApiWithFallback } from "./api-client";

const NOTIFICATIONS_COUNT_CHANGED_EVENT = "library:notifications-count-changed";
const POLL_INTERVAL_MS = 60_000;

export function refreshNotificationsBadgeCount() {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(NOTIFICATIONS_COUNT_CHANGED_EVENT));
  }
}

export function useUnseenNotificationCount(enabled: boolean): number {
  const [count, setCount] = useState(0);

  useEffect(() => {
    if (!enabled) {
      return;
    }

    let cancelled = false;

    async function loadCount() {
      try {
        const response = await fetchApiWithFallback("/notifications/unseen", { cache: "no-store" });
        const data = await response.json();
        if (!cancelled && Array.isArray(data)) {
          setCount(data.length);
        }
      } catch {
        // Silent by design -- a failed badge refresh shouldn't surface an
        // error toast for what's just a small unread-count indicator.
      }
    }

    void loadCount();
    const interval = window.setInterval(loadCount, POLL_INTERVAL_MS);
    window.addEventListener(NOTIFICATIONS_COUNT_CHANGED_EVENT, loadCount);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
      window.removeEventListener(NOTIFICATIONS_COUNT_CHANGED_EVENT, loadCount);
    };
  }, [enabled]);

  return count;
}
