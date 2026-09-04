"use client";

import Link from "next/link";

import { Button } from "@/components/ui/button";
import { type DiscoveryHealth } from "@/components/series/discovery-health-badge";
import { NewBooksPill } from "@/components/series/new-books-pill";

export type MobileSeriesRow = {
  id: number;
  name: string;
  author?: string | null;
  next_unread_book_number?: number | null;
  next_upcoming_book_number?: number | null;
  total_books?: number | null;
  last_checked?: string | null;
  discovery_health?: DiscoveryHealth | null;
  is_finished?: boolean;
};

export type MobileSeriesItem = {
  series: MobileSeriesRow;
  hasNewBooks: boolean;
  missingBooksLabel: string | null;
  lastCheckedDisplay: string;
  // Two-Timestamp UI Adjustments spec (locked 2026-09-04).
  lastVerifiedDisplay: string;
  lastSyncedDisplay: string;
};

/**
 * Mobile-only vertical card list for the /series list page, standing in for
 * its desktop <Table> (id/name/author/next-unread/next-upcoming/total/
 * last-checked/actions columns) when useIsMobile() is true -- that many
 * columns squeezed into a phone width is what caused the header-row
 * overlap this was written to fix.
 */
export function MobileSeriesList({
  items,
  viewMode,
}: {
  items: MobileSeriesItem[];
  viewMode: "ongoing" | "finished";
}) {
  if (items.length === 0) {
    return <p className="px-3 py-6 text-center text-sm text-muted-foreground">No series match the current filters.</p>;
  }

  return (
    <ul className="flex flex-col gap-2 px-2">
      {items.map(({ series, hasNewBooks, missingBooksLabel, lastCheckedDisplay, lastVerifiedDisplay, lastSyncedDisplay }) => (
        <li key={series.id} className="rounded-lg border bg-card/80 p-3">
          <div className="flex items-center gap-2">
            {hasNewBooks ? <NewBooksPill /> : null}
            <p className="min-w-0 flex-1 truncate text-sm font-semibold leading-tight">{series.name}</p>
          </div>
          {series.author ? <p className="truncate text-xs text-muted-foreground">{series.author}</p> : null}
          {missingBooksLabel ? <p className="mt-0.5 truncate text-[11px] text-rose-700">{missingBooksLabel}</p> : null}

          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
            <span>Next unread <span className="font-medium text-foreground">{series.next_unread_book_number ?? "—"}</span></span>
            <span>Next upcoming <span className="font-medium text-foreground">{series.next_upcoming_book_number ?? "—"}</span></span>
            <span>Total <span className="font-medium text-foreground">{series.total_books ?? "—"}</span></span>
            <span>Checked {lastCheckedDisplay}</span>
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
            <span>Last Verified {lastVerifiedDisplay}</span>
            <span>Last Synced {lastSyncedDisplay}</span>
          </div>

          <div className="mt-2 flex items-center gap-2 border-t pt-2">
            <Link href={`/series/${series.id}?fromView=${viewMode}`}>
              <Button variant="ghost" size="sm">View books</Button>
            </Link>
          </div>
        </li>
      ))}
    </ul>
  );
}
