"use client";

import Link from "next/link";
import { BookOpenIcon, Clock3Icon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { type DiscoveryHealth } from "@/components/series/discovery-health-badge";

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

export type MobileSeriesCheckState = {
  tone: "success" | "info" | "error" | string;
  title: string;
  message: string;
  detail?: string | null;
  actionHref?: string | null;
  actionLabel?: string | null;
};

export type MobileSeriesItem = {
  series: MobileSeriesRow;
  hasNewAvailableBooks: boolean;
  hasNewUpcomingBooks: boolean;
  hasUnreadBooks: boolean;
  missingBooksLabel: string | null;
  lastCheckedDisplay: string;
  checkState: MobileSeriesCheckState | null;
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
  checkingSeriesId,
  onCheckNow,
  onDismissCheckState,
  getCheckStateClassName,
}: {
  items: MobileSeriesItem[];
  viewMode: "ongoing" | "finished";
  checkingSeriesId: number | null;
  onCheckNow: (seriesId: number) => void;
  onDismissCheckState: (seriesId: number) => void;
  getCheckStateClassName: (tone: string) => string;
}) {
  if (items.length === 0) {
    return <p className="px-3 py-6 text-center text-sm text-muted-foreground">No series match the current filters.</p>;
  }

  return (
    <ul className="flex flex-col gap-2 px-2">
      {items.map(({ series, hasNewAvailableBooks, hasNewUpcomingBooks, missingBooksLabel, lastCheckedDisplay, checkState }) => (
        <li key={series.id} className="rounded-lg border bg-card/80 p-3">
          <div className="flex items-center gap-1">
            <p className="min-w-0 flex-1 truncate text-sm font-semibold leading-tight">{series.name}</p>
            {hasNewAvailableBooks ? (
              <BookOpenIcon className="h-3.5 w-3.5 shrink-0 text-sky-600" aria-label="New available book(s) found" />
            ) : null}
            {hasNewUpcomingBooks ? (
              <Clock3Icon className="h-3.5 w-3.5 shrink-0 text-rose-600" aria-label="New upcoming book(s) found" />
            ) : null}
          </div>
          {series.author ? <p className="truncate text-xs text-muted-foreground">{series.author}</p> : null}
          {missingBooksLabel ? <p className="mt-0.5 truncate text-[11px] text-rose-700">{missingBooksLabel}</p> : null}

          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
            <span>Next unread <span className="font-medium text-foreground">{series.next_unread_book_number ?? "—"}</span></span>
            <span>Next upcoming <span className="font-medium text-foreground">{series.next_upcoming_book_number ?? "—"}</span></span>
            <span>Total <span className="font-medium text-foreground">{series.total_books ?? "—"}</span></span>
            <span>Checked {lastCheckedDisplay}</span>
          </div>

          <div className="mt-2 flex items-center gap-2 border-t pt-2">
            <Link href={`/series/${series.id}?fromView=${viewMode}`}>
              <Button variant="ghost" size="sm">View books</Button>
            </Link>
            <Button
              variant="outline"
              size="sm"
              onClick={() => onCheckNow(series.id)}
              disabled={checkingSeriesId === series.id}
            >
              {checkingSeriesId === series.id ? "Checking…" : "Check for New"}
            </Button>
          </div>

          {checkState ? (
            <div className={`mt-2 rounded border px-2 py-1 text-[11px] ${getCheckStateClassName(checkState.tone)}`}>
              <span className="font-semibold">{checkState.title}</span>
              <span className="ml-1">{checkState.message}</span>
              {checkState.detail ? <span className="ml-1 opacity-80">{checkState.detail}</span> : null}
              {checkState.actionHref && checkState.actionLabel ? (
                <Link href={checkState.actionHref} className="ml-2 underline underline-offset-2">
                  {checkState.actionLabel}
                </Link>
              ) : null}
              <button type="button" onClick={() => onDismissCheckState(series.id)} className="ml-2 underline underline-offset-2">
                dismiss
              </button>
            </div>
          ) : null}
        </li>
      ))}
    </ul>
  );
}
