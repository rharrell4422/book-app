"use client";

import { AlertTriangleIcon, ExternalLinkIcon, FileTextIcon, PencilIcon, SearchIcon } from "lucide-react";

import { Button } from "@/components/ui/button";

export type MobileSeriesBook = {
  id: number;
  title?: string | null;
  author?: string | null;
  book_number?: number | null;
  [key: string]: unknown;
};

export type MobileSeriesBookItem = {
  book: MobileSeriesBook;
  status: string;
  statusChipClass: string;
  unconfirmedDate: boolean;
  displayDate: string;
};

/**
 * Mobile-only vertical card list for the series detail page, standing in
 * for its desktop <Table> when useIsMobile() is true. The desktop table's
 * row of 3-4 text buttons ("Edit book" / "Notes" / "Check online" /
 * more-by-author) doesn't fit a narrow column width under table-layout:
 * fixed and visually overlaps neighboring cells -- this avoids that by
 * using a stacked card with icon-only actions instead, matching the
 * pattern already used for the /books mobile list.
 */
export function MobileSeriesBookList({
  items,
  canEdit,
  highlightedBookId,
  onEdit,
  onOpenSummary,
  onMoreByAuthor,
  onCheckOnline,
}: {
  items: MobileSeriesBookItem[];
  canEdit: boolean;
  /** Book the page just scrolled to after an add/edit, highlighted briefly. */
  highlightedBookId?: number | null;
  onEdit: (book: MobileSeriesBook) => void;
  onOpenSummary: (book: MobileSeriesBook) => void;
  onMoreByAuthor: (author: string) => void;
  onCheckOnline: (book: MobileSeriesBook) => void;
}) {
  if (items.length === 0) {
    return <p className="px-3 py-6 text-center text-sm text-muted-foreground">No books match the current filters.</p>;
  }

  return (
    <ul className="flex flex-col gap-2 px-2">
      {items.map(({ book, status, statusChipClass, unconfirmedDate, displayDate }) => (
        <li
          key={book.id}
          data-book-id={book.id}
          className={`rounded-lg border bg-card/80 p-3 transition-colors duration-500${
            Number(book.id) === highlightedBookId ? " border-emerald-400 bg-emerald-50/80" : ""
          }`}
        >
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold leading-tight">
                {book.book_number !== null && book.book_number !== undefined ? `#${book.book_number} · ` : ""}
                {book.title || "—"}
              </p>
              {book.author ? <p className="truncate text-xs text-muted-foreground">{book.author}</p> : null}
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <span className={statusChipClass}>{status}</span>
              {unconfirmedDate ? (
                <AlertTriangleIcon className="h-3.5 w-3.5 shrink-0 text-amber-600" aria-label="Date unconfirmed" />
              ) : null}
            </div>
          </div>

          {displayDate ? <p className="mt-1.5 text-xs text-muted-foreground">{displayDate}</p> : null}

          <div className="mt-2 flex items-center gap-1 border-t pt-2">
            <Button type="button" variant="ghost" size="icon-xs" title="View/edit summary and notes" aria-label="View/edit summary and notes" onClick={() => onOpenSummary(book)}>
              <FileTextIcon />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              title={unconfirmedDate ? "No confirmed date -- verify with retailer" : "Check source listing"}
              aria-label="Check online"
              className={unconfirmedDate ? "text-amber-600 hover:text-amber-700" : undefined}
              onClick={() => onCheckOnline(book)}
            >
              <ExternalLinkIcon />
            </Button>
            <Button type="button" variant="ghost" size="icon-xs" title="More by this author" aria-label="More by this author" onClick={() => onMoreByAuthor(String(book.author || ""))}>
              <SearchIcon />
            </Button>
            {canEdit ? (
              <Button type="button" variant="outline" size="icon-xs" title="Edit book" aria-label="Edit book" className="ml-auto" onClick={() => onEdit(book)}>
                <PencilIcon />
              </Button>
            ) : null}
          </div>
        </li>
      ))}
    </ul>
  );
}
