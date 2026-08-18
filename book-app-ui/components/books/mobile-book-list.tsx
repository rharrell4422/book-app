"use client";

import { AlertTriangleIcon } from "lucide-react";

import { BookActionIcon } from "@/components/books/book-action-icon";

export type MobileBookCardBook = {
  id: number;
  title?: string | null;
  author?: string | null;
  series_name?: string | null;
  series_id?: number | null;
  book_number?: number | null;
  is_read?: boolean | null;
  auto_summary?: string | null;
  notes?: string | null;
  source_url?: string | null;
  [key: string]: unknown;
};

export type MobileBookCardItem = {
  book: MobileBookCardBook;
  status: string;
  statusChipClass: string;
  unconfirmedDate: boolean;
  displayDate: string;
};

/**
 * Mobile-only vertical card list, standing in for the desktop <Table> in
 * BooksClient.tsx when useIsMobile() is true. All status/date computation
 * stays centralized in BooksClient (single source of truth); this component
 * is purely presentational and forwards taps straight to the same handlers
 * the desktop table rows already use.
 */
export function MobileBookList({
  items,
  canEdit,
  onToggleRead,
  onEdit,
  onDelete,
  onOpenSummary,
  onMoreByAuthor,
  onViewSeries,
  onCheckOnline,
}: {
  items: MobileBookCardItem[];
  canEdit: boolean;
  onToggleRead: (book: MobileBookCardBook) => void;
  onEdit: (book: MobileBookCardBook) => void;
  onDelete: (bookId: number) => void;
  onOpenSummary: (book: MobileBookCardBook) => void;
  onMoreByAuthor: (author: string) => void;
  onViewSeries: (seriesId: number) => void;
  onCheckOnline: (book: MobileBookCardBook) => void;
}) {
  if (items.length === 0) {
    return <p className="px-3 py-6 text-center text-sm text-muted-foreground">No books match the current filters.</p>;
  }

  return (
    <ul className="flex flex-col gap-2 px-2">
      {items.map(({ book, status, statusChipClass, unconfirmedDate, displayDate }) => (
        <li key={book.id} className="rounded-lg border bg-card/80 p-3">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold leading-tight">{book.title || "—"}</p>
              <p className="truncate text-xs text-muted-foreground">{book.author || "—"}</p>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <span className={statusChipClass}>{status}</span>
              {unconfirmedDate ? (
                <AlertTriangleIcon className="h-3.5 w-3.5 shrink-0 text-amber-600" aria-label="Date unconfirmed" />
              ) : null}
            </div>
          </div>

          <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
            {book.series_name ? (
              <span className="truncate">
                {book.series_name}
                {book.book_number !== null && book.book_number !== undefined ? ` #${book.book_number}` : ""}
              </span>
            ) : null}
            {displayDate ? <span>{displayDate}</span> : null}
          </div>

          <div className="mt-2 flex items-center gap-1 border-t pt-2">
            {book.series_id ? (
              <BookActionIcon state="series" onClick={() => onViewSeries(Number(book.series_id))} />
            ) : (
              <BookActionIcon
                state={book.auto_summary || book.notes ? "summaryStandaloneHasContent" : "summaryStandaloneEmpty"}
                onClick={() => onOpenSummary(book)}
              />
            )}
            <BookActionIcon
              state={unconfirmedDate ? "unconfirmedDate" : book.source_url ? "hasSourceUrl" : "missingSourceUrl"}
              onClick={() => onCheckOnline(book)}
            />
            <BookActionIcon state="moreByAuthor" onClick={() => onMoreByAuthor(String(book.author || ""))} />
            {canEdit ? (
              <div className="ml-auto flex items-center gap-1">
                <BookActionIcon state={book.is_read ? "read" : "unread"} onClick={() => onToggleRead(book)} />
                <BookActionIcon state="edit" onClick={() => onEdit(book)} />
                <BookActionIcon state="delete" onClick={() => onDelete(Number(book.id))} />
              </div>
            ) : null}
          </div>
        </li>
      ))}
    </ul>
  );
}
