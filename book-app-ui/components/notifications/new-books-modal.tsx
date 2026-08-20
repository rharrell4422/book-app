"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { fetchApiWithFallback } from "@/lib/api-client";

type NotificationItem = {
  id: number;
  kind: string;
  book_id: number | null;
  book_title: string | null;
  series_id: number | null;
  series_name: string | null;
  created_at: string;
};

/**
 * "New Books Added to Library" popup (Auto Discovery MVP spec, §3).
 * Fetches undismissed notifications once on mount (app load) and shows a
 * single modal requiring manual dismissal -- deliberately not a persistent
 * inbox/bell icon, matching the spec's "keep this lightweight" guidance.
 */
export function NewBooksNotificationModal() {
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [open, setOpen] = useState(false);
  const [dismissing, setDismissing] = useState(false);

  useEffect(() => {
    let cancelled = false;

    async function loadUnseen() {
      try {
        const response = await fetchApiWithFallback("/notifications/unseen", { cache: "no-store" });
        const data = await response.json();
        if (cancelled || !Array.isArray(data) || data.length === 0) return;
        setItems(data);
        setOpen(true);
      } catch {
        // Silent by design -- a failed notification fetch shouldn't block
        // or interrupt the rest of the app from loading.
      }
    }

    void loadUnseen();
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleDismiss() {
    setDismissing(true);
    try {
      await fetchApiWithFallback("/notifications/dismiss", { method: "POST" });
    } catch {
      // Even if the dismiss call fails, close the modal -- it'll just
      // reappear next load, which is preferable to trapping the user.
    } finally {
      setDismissing(false);
      setOpen(false);
    }
  }

  if (items.length === 0) return null;

  return (
    <Dialog open={open} onOpenChange={(next) => !next && handleDismiss()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>New Books Added to Your Library</DialogTitle>
          <DialogDescription>
            Auto Discovery found {items.length === 1 ? "a new book" : `${items.length} new books`} for you.
          </DialogDescription>
        </DialogHeader>
        <ul className="flex max-h-64 flex-col gap-2 overflow-y-auto text-sm">
          {items.map((item) => (
            <li key={item.id} className="rounded-md border px-3 py-2">
              <p className="font-medium">{item.book_title || "Untitled book"}</p>
              {item.series_name ? (
                item.series_id ? (
                  <Link href={`/series/${item.series_id}`} className="text-xs text-muted-foreground underline underline-offset-2">
                    {item.series_name}
                  </Link>
                ) : (
                  <p className="text-xs text-muted-foreground">{item.series_name}</p>
                )
              ) : null}
            </li>
          ))}
        </ul>
        <DialogFooter>
          <Button onClick={handleDismiss} disabled={dismissing}>
            {dismissing ? "Dismissing..." : "Got it"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
