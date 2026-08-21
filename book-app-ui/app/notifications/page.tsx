"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { BellIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useToast } from "@/components/ui/use-toast";
import { fetchApiWithFallback } from "@/lib/api-client";
import { refreshNotificationsBadgeCount } from "@/lib/notifications-badge";

type NotificationItem = {
  id: number;
  series_id: number | null;
  series_name: string | null;
  count_new_books: number;
  created_at: string;
};

function formatNotificationDate(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return parsed.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

/**
 * Durable series-level discovery notifications (see the "Durable
 * Series-Level Discovery Notifications" design chat's finalized spec) --
 * a persistent, browsable history that survives long absences, entirely
 * separate from the ephemeral in-session popup shown by the series/
 * settings pages right after a Check Now / Full Auto Discovery run.
 * Nothing here depends on that popup, and nothing there depends on this.
 */
export default function NotificationsPage() {
  const { toast } = useToast();
  const [items, setItems] = useState<NotificationItem[] | null>(null);
  const [dismissingId, setDismissingId] = useState<number | null>(null);
  const [dismissingAll, setDismissingAll] = useState(false);

  const loadNotifications = useCallback(async () => {
    try {
      const response = await fetchApiWithFallback("/notifications/unseen", { cache: "no-store" });
      const data = await response.json();
      setItems(Array.isArray(data) ? data : []);
    } catch {
      setItems([]);
      toast({
        title: "Couldn't load notifications",
        description: "Please try again.",
      });
    }
  }, [toast]);

  useEffect(() => {
    // loadNotifications sets state before its first await -- standard
    // "fetch on mount" pattern, not derived state.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadNotifications();
  }, [loadNotifications]);

  async function handleDismiss(id: number) {
    setDismissingId(id);
    try {
      await fetchApiWithFallback(`/notifications/${id}/dismiss`, { method: "POST" });
      setItems((current) => (current ? current.filter((item) => item.id !== id) : current));
      refreshNotificationsBadgeCount();
    } catch {
      toast({
        title: "Couldn't dismiss notification",
        description: "Please try again.",
      });
    } finally {
      setDismissingId(null);
    }
  }

  async function handleDismissAll() {
    setDismissingAll(true);
    try {
      await fetchApiWithFallback("/notifications/dismiss", { method: "POST" });
      setItems([]);
      refreshNotificationsBadgeCount();
    } catch {
      toast({
        title: "Couldn't dismiss all notifications",
        description: "Please try again.",
      });
    } finally {
      setDismissingAll(false);
    }
  }

  return (
    <div className="mx-auto max-w-2xl space-y-4 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground">Library</p>
          <h1 className="flex items-center gap-2 text-2xl font-bold">
            <BellIcon className="h-6 w-6" />
            Notifications
          </h1>
        </div>
        <div className="flex items-center gap-2">
          {items && items.length > 0 ? (
            <Button variant="outline" size="sm" onClick={handleDismissAll} disabled={dismissingAll}>
              {dismissingAll ? "Dismissing..." : "Dismiss all"}
            </Button>
          ) : null}
          <Link href="/books">
            <Button variant="outline" size="sm">
              Back to Library
            </Button>
          </Link>
        </div>
      </div>

      {items === null ? (
        <p className="text-sm text-muted-foreground">Loading notifications...</p>
      ) : items.length === 0 ? (
        <Card>
          <CardContent className="py-8 text-center text-sm text-muted-foreground">
            No discovery notifications yet. When Check Now or Full Auto Discovery finds new books for a series, it
            will show up here -- even if you&rsquo;re not around when it happens.
          </CardContent>
        </Card>
      ) : (
        <ul className="flex flex-col gap-2">
          {items.map((item) => (
            <li key={item.id}>
              <Card>
                <CardContent className="flex items-center justify-between gap-3 py-3">
                  <p className="text-sm">
                    <span className="font-medium">
                      {item.count_new_books} new book{item.count_new_books === 1 ? "" : "s"}
                    </span>{" "}
                    added to{" "}
                    {item.series_id ? (
                      <Link href={`/series/${item.series_id}`} className="font-medium underline underline-offset-2">
                        {item.series_name || "a series"}
                      </Link>
                    ) : (
                      <span className="font-medium">{item.series_name || "a series"}</span>
                    )}{" "}
                    on {formatNotificationDate(item.created_at)}.
                  </p>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => handleDismiss(item.id)}
                    disabled={dismissingId === item.id}
                  >
                    {dismissingId === item.id ? "Dismissing..." : "Dismiss"}
                  </Button>
                </CardContent>
              </Card>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
