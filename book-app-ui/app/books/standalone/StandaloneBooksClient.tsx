"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useToast } from "@/components/ui/use-toast";
import { Button } from "@/components/ui/button";
import { BookActionIcon } from "@/components/books/book-action-icon";
import { MobileBookList } from "@/components/books/mobile-book-list";
import { publishBookStatusUpdate, subscribeBookStatusUpdates } from "@/lib/book-status-sync";
import { fetchApiWithFallback } from "@/lib/api-client";
import { ValueFilterMenu } from "@/components/value-filter-menu";
import { useAuth } from "@/lib/auth-context";
import {
  type BookStatus,
  formatDate,
  getCheckOnlineUrl,
  getStatusChipClass,
  hasUnconfirmedReleaseDate,
  isPastOrTodayDate,
  parseFlexibleDate,
} from "@/lib/book-format";
import { EditBookDialog } from "@/components/books/edit-book-dialog";
import { BookSummaryDialog } from "@/components/series/book-summary-dialog";
import { MoreByAuthorDialog } from "@/components/books/more-by-author-dialog";
import { useDeviceClass } from "@/hooks/use-device-class";
import { getDeviceClass } from "@/lib/device-class";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

type BookRow = {
  id: number;
  title?: string | null;
  author?: string | null;
  read_status?: string | null;
  is_read?: boolean | null;
  release_date?: string | null;
  publication_date?: string | null;
  read_date?: string | null;
  series_id?: number | null;
  source_url?: string | null;
  auto_summary?: string | null;
  notes?: string | null;
  [key: string]: unknown;
};

type SortKey = "title" | "author" | "status" | "date";
type SortDirection = "asc" | "desc";

// Deliberately duplicated (rather than imported) from BooksClient.tsx -- this
// view is intentionally kept small and standalone, with none of the
// series-specific logic (next-book lookahead, KU/upcoming discovery, etc.)
// that the series pages carry. It only ever needs the plain fields already
// present on a book record.
function getBookStatus(book: BookRow): BookStatus {
  const explicitStatus = String(book.read_status || "").trim().toLowerCase();

  if (book.is_read || explicitStatus === "read") {
    return "read";
  }

  const releaseDate = book.release_date || book.publication_date;

  if (explicitStatus === "upcoming") {
    if (releaseDate && isPastOrTodayDate(releaseDate)) return "available";
    return "upcoming";
  }
  if (explicitStatus === "available") return "available";

  if (releaseDate) {
    const parsedDate = parseFlexibleDate(releaseDate);
    if (parsedDate) {
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      parsedDate.setHours(0, 0, 0, 0);
      return parsedDate > today ? "upcoming" : "available";
    }
  }

  return "unread";
}

function getDisplayDate(book: BookRow): string | null {
  const status = getBookStatus(book);
  const releaseOrPublicationDate = book.release_date || book.publication_date || null;
  return status === "upcoming" ? releaseOrPublicationDate || book.read_date || null : book.read_date || releaseOrPublicationDate;
}

export default function StandaloneBooksClient() {
  const { toast } = useToast();
  const { role } = useAuth();
  const router = useRouter();
  const canEdit = role === "owner";
  const deviceClass = useDeviceClass();

  const [allBooks, setAllBooks] = useState<BookRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [editBookId, setEditBookId] = useState<number | null>(null);
  const [summaryEditorBook, setSummaryEditorBook] = useState<BookRow | null>(null);
  const [summaryDraft, setSummaryDraft] = useState("");
  const [notesDraft, setNotesDraft] = useState("");
  const [summaryFetching, setSummaryFetching] = useState(false);
  const [summarySaving, setSummarySaving] = useState(false);
  const [moreByAuthorTarget, setMoreByAuthorTarget] = useState<string | null>(null);
  const [sortConfig, setSortConfig] = useState<{ key: SortKey | null; direction: SortDirection }>({
    key: "title",
    direction: "asc",
  });
  const [valueFilters, setValueFilters] = useState({
    title: [] as string[],
    author: [] as string[],
    status: [] as string[],
  });
  const [valueFilterSearch, setValueFilterSearch] = useState({ title: "", author: "", status: "" });

  const fetchBooks = useCallback(async () => {
    setLoading(true);
    try {
      const response = await fetchApiWithFallback("/books/", { cache: "no-store" });
      const data = await response.json();
      setAllBooks(Array.isArray(data) ? data : []);
    } catch (error) {
      console.error("Error fetching books:", error);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // fetchBooks sets loading state synchronously before its first await --
    // standard "fetch on mount" pattern, not derived state that could be
    // computed during render.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    fetchBooks();
  }, [fetchBooks]);

  useEffect(() => {
    const unsubscribe = subscribeBookStatusUpdates((payload) => {
      setAllBooks((prev) => {
        if (String(payload.record_status || "").toLowerCase() === "deleted") {
          return prev.filter((book) => book.id !== payload.id);
        }
        if (!prev.some((book) => book.id === payload.id)) return prev;
        return prev.map((book) => (book.id === payload.id ? { ...book, ...payload } : book));
      });
    });
    return unsubscribe;
  }, []);

  // The entire scope of this view: books with no series attached. No series
  // lookups, no upcoming/KU discovery logic -- just this one filter.
  const standaloneBooks = useMemo(() => allBooks.filter((book) => !book.series_id), [allBooks]);

  const titleOptions = useMemo(
    () => Array.from(new Set(standaloneBooks.map((book) => String(book.title || "").trim()))).sort((a, b) => a.localeCompare(b)),
    [standaloneBooks],
  );
  const authorOptions = useMemo(
    () => Array.from(new Set(standaloneBooks.map((book) => String(book.author || "").trim()))).sort((a, b) => a.localeCompare(b)),
    [standaloneBooks],
  );
  const statusOptions = useMemo(
    () => Array.from(new Set(standaloneBooks.map((book) => String(getBookStatus(book)).trim()))).sort((a, b) => a.localeCompare(b)),
    [standaloneBooks],
  );

  const activeValueFilters = useMemo(() => {
    const titleSet = new Set(titleOptions);
    const authorSet = new Set(authorOptions);
    const statusSet = new Set(statusOptions);
    return {
      title: valueFilters.title.filter((value) => titleSet.has(value)),
      author: valueFilters.author.filter((value) => authorSet.has(value)),
      status: valueFilters.status.filter((value) => statusSet.has(value)),
    };
  }, [valueFilters, titleOptions, authorOptions, statusOptions]);

  const filteredBooks = useMemo(() => {
    return standaloneBooks.filter((book) => {
      if (activeValueFilters.title.length > 0 && !activeValueFilters.title.includes(String(book.title || "").trim())) return false;
      if (activeValueFilters.author.length > 0 && !activeValueFilters.author.includes(String(book.author || "").trim())) return false;
      if (activeValueFilters.status.length > 0 && !activeValueFilters.status.includes(String(getBookStatus(book)).trim())) return false;
      return true;
    });
  }, [standaloneBooks, activeValueFilters]);

  const sortedBooks = useMemo(() => {
    if (!sortConfig.key) return filteredBooks;
    const key = sortConfig.key;
    return [...filteredBooks].sort((a, b) => {
      const aValue =
        key === "title"
          ? String(a.title || "")
          : key === "author"
            ? String(a.author || "")
            : key === "status"
              ? String(getBookStatus(a))
              : parseFlexibleDate(getDisplayDate(a))?.valueOf() ?? 0;
      const bValue =
        key === "title"
          ? String(b.title || "")
          : key === "author"
            ? String(b.author || "")
            : key === "status"
              ? String(getBookStatus(b))
              : parseFlexibleDate(getDisplayDate(b))?.valueOf() ?? 0;

      const result =
        typeof aValue === "number" && typeof bValue === "number"
          ? aValue - bValue
          : String(aValue).localeCompare(String(bValue), undefined, { sensitivity: "base" });
      return sortConfig.direction === "asc" ? result : -result;
    });
  }, [filteredBooks, sortConfig]);

  function toggleSort(key: SortKey) {
    setSortConfig((prev) => {
      if (prev.key !== key) return { key, direction: "asc" };
      if (prev.direction === "asc") return { key, direction: "desc" };
      return { key: null, direction: "asc" };
    });
  }

  function sortLabel(key: SortKey) {
    if (sortConfig.key !== key) return "";
    return sortConfig.direction === "asc" ? " ▲" : " ▼";
  }

  function setValueFilter(kind: "title" | "author" | "status", values: string[]) {
    setValueFilters((prev) => ({ ...prev, [kind]: values }));
  }

  async function toggleRead(book: BookRow) {
    const nextIsRead = !book.is_read;
    try {
      const response = await fetchApiWithFallback(`/books/${book.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          is_read: nextIsRead,
          read_status: nextIsRead ? "read" : "unread",
          read_date: nextIsRead ? new Date().toISOString().split("T")[0] : null,
        }),
      });
      if (response.ok) {
        const updatedBook = await response.json();
        setAllBooks((prev) => prev.map((item) => (item.id === updatedBook.id ? { ...item, ...updatedBook } : item)));
        publishBookStatusUpdate(updatedBook);
      } else {
        toast({ title: "Error", description: "Failed to update book." });
      }
    } catch (error) {
      console.error("Error updating book:", error);
    }
  }

  async function deleteBook(bookId: number) {
    if (!confirm("Delete this book?")) return;
    try {
      const response = await fetchApiWithFallback(`/books/${bookId}`, { method: "DELETE" });
      if (response.ok) {
        toast({ title: "Deleted", description: `Book ${bookId} removed.` });
        setAllBooks((prev) => prev.filter((book) => book.id !== bookId));
      } else {
        toast({ title: "Error", description: "Failed to delete book." });
      }
    } catch (error) {
      console.error("Error deleting book:", error);
    }
  }

  function startEditBook(book: BookRow) {
    if (getDeviceClass() === "desktop") {
      setEditBookId(Number(book.id));
      setEditDialogOpen(true);
    } else {
      router.push(`/edit-book/${book.id}?returnTo=/books/standalone`);
    }
  }

  function openSummaryEditor(book: BookRow) {
    setSummaryEditorBook(book);
    setSummaryDraft(String(book?.auto_summary || ""));
    setNotesDraft(String(book?.notes || ""));
  }

  async function handleFetchSummary() {
    if (!summaryEditorBook) return;
    setSummaryFetching(true);
    try {
      const response = await fetchApiWithFallback(`/books/${summaryEditorBook.id}/summary`, {
        method: "POST",
      });
      if (!response.ok) {
        throw new Error(`Failed to fetch summary (${response.status})`);
      }

      const data = await response.json();
      const updatedBook = data.book;
      setAllBooks((prev) => prev.map((item) => (item.id === updatedBook.id ? { ...item, ...updatedBook } : item)));
      setSummaryEditorBook(updatedBook);
      setSummaryDraft(String(updatedBook.auto_summary || ""));
      setNotesDraft(String(updatedBook.notes || ""));
    } catch (error) {
      console.error(error);
      toast({ title: "Summary lookup failed", description: "Unable to fetch a summary for this book right now." });
    } finally {
      setSummaryFetching(false);
    }
  }

  async function handleSaveSummaryEditor() {
    if (!summaryEditorBook) return;
    setSummarySaving(true);
    try {
      const response = await fetchApiWithFallback(`/books/${summaryEditorBook.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          auto_summary: summaryDraft.trim() || null,
          notes: notesDraft.trim() || null,
        }),
      });
      if (!response.ok) {
        throw new Error(`Failed to save summary (${response.status})`);
      }

      const updatedBook = await response.json();
      setAllBooks((prev) => prev.map((item) => (item.id === updatedBook.id ? { ...item, ...updatedBook } : item)));
      setSummaryEditorBook(updatedBook);
      setSummaryDraft(String(updatedBook.auto_summary || ""));
      setNotesDraft(String(updatedBook.notes || ""));
    } catch (error) {
      console.error(error);
      toast({ title: "Save failed", description: "Unable to save summary or notes right now." });
    } finally {
      setSummarySaving(false);
    }
  }

  const totalBooks = standaloneBooks.length;
  const readBooks = standaloneBooks.filter((book) => getBookStatus(book) === "read").length;
  const unreadBooks = totalBooks - readBooks;

  return (
    <div className="p-2 space-y-1.5">
      <div className="space-y-1.5 rounded-lg border bg-card/60 px-3 py-2">
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
            <h1 className="text-xl font-bold leading-tight">Standalone Books</h1>
            <p className="text-xs text-muted-foreground">Books not attached to any series.</p>
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
            <span>Unread <span className="font-semibold text-foreground">{unreadBooks}</span></span>
            <span>Read <span className="font-semibold text-foreground">{readBooks}</span></span>
            <span>Total <span className="font-semibold text-foreground">{totalBooks}</span></span>
          </div>
        </div>

        <div className="flex flex-wrap gap-2">
          <Link href="/books">
            <Button type="button" variant="outline">All Books</Button>
          </Link>
          <Link href="/series">
            <Button type="button" variant="outline">Series</Button>
          </Link>
        </div>
      </div>

      {deviceClass === "desktop" ? (
      <div className="overflow-x-auto rounded-lg border bg-card/80">
        <Table className="w-full min-w-[720px] text-sm [&_th]:h-9 [&_th]:py-1 [&_td]:py-1">
          <TableHeader>
            <TableRow>
              <TableHead>
                <div className="flex items-center gap-1">
                  <button type="button" onClick={() => toggleSort("title")} className="hover:underline">
                    Title{sortLabel("title")}
                  </button>
                  <ValueFilterMenu
                    label="Title"
                    options={titleOptions}
                    selectedValues={valueFilters.title}
                    onApplyValues={(values) => setValueFilter("title", values)}
                    onClear={() => setValueFilter("title", [])}
                    searchValue={valueFilterSearch.title}
                    onSearchChange={(value) => setValueFilterSearch((prev) => ({ ...prev, title: value }))}
                  />
                </div>
              </TableHead>
              <TableHead>
                <div className="flex items-center gap-1">
                  <button type="button" onClick={() => toggleSort("author")} className="hover:underline">
                    Author{sortLabel("author")}
                  </button>
                  <ValueFilterMenu
                    label="Author"
                    options={authorOptions}
                    selectedValues={valueFilters.author}
                    onApplyValues={(values) => setValueFilter("author", values)}
                    onClear={() => setValueFilter("author", [])}
                    searchValue={valueFilterSearch.author}
                    onSearchChange={(value) => setValueFilterSearch((prev) => ({ ...prev, author: value }))}
                  />
                </div>
              </TableHead>
              <TableHead>
                <div className="flex items-center gap-1">
                  <button type="button" onClick={() => toggleSort("status")} className="hover:underline">
                    Status{sortLabel("status")}
                  </button>
                  <ValueFilterMenu
                    label="Status"
                    options={statusOptions}
                    selectedValues={valueFilters.status}
                    onApplyValues={(values) => setValueFilter("status", values)}
                    onClear={() => setValueFilter("status", [])}
                    searchValue={valueFilterSearch.status}
                    onSearchChange={(value) => setValueFilterSearch((prev) => ({ ...prev, status: value }))}
                  />
                </div>
              </TableHead>
              <TableHead>
                <button type="button" onClick={() => toggleSort("date")} className="hover:underline">
                  Date{sortLabel("date")}
                </button>
              </TableHead>
              <TableHead>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {sortedBooks.map((b) => {
              const status = getBookStatus(b);
              const unconfirmedDate = hasUnconfirmedReleaseDate(status, b);
              return (
                <TableRow key={b.id}>
                  <TableCell className="truncate" title={b.title ?? undefined}>{b.title || "—"}</TableCell>
                  <TableCell className="truncate" title={b.author ?? undefined}>{b.author || "—"}</TableCell>
                  <TableCell>
                    <span className={getStatusChipClass(status, "compact")}>{status}</span>
                  </TableCell>
                  <TableCell>{formatDate(getDisplayDate(b))}</TableCell>
                  <TableCell className="whitespace-nowrap">
                    <div className="flex items-center gap-0.5">
                      <BookActionIcon
                        state={unconfirmedDate ? "unconfirmedDate" : b.source_url ? "hasSourceUrl" : "missingSourceUrl"}
                        onClick={() => window.open(getCheckOnlineUrl(b), "_blank", "noopener,noreferrer")}
                      />
                      <BookActionIcon state="moreByAuthor" onClick={() => setMoreByAuthorTarget(String(b.author || ""))} />
                      <BookActionIcon
                        state={b.auto_summary || b.notes ? "summaryStandaloneHasContent" : "summaryStandaloneEmpty"}
                        onClick={() => openSummaryEditor(b)}
                      />
                      {canEdit ? (
                        <>
                          <BookActionIcon state={b.is_read ? "read" : "unread"} onClick={() => toggleRead(b)} />
                          <BookActionIcon state="edit" onClick={() => startEditBook(b)} />
                          <BookActionIcon state="delete" onClick={() => deleteBook(b.id)} />
                        </>
                      ) : null}
                    </div>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
      ) : (
        <MobileBookList
          items={sortedBooks.map((b) => {
            const status = getBookStatus(b);
            return {
              book: b,
              status,
              statusChipClass: getStatusChipClass(status, "compact"),
              unconfirmedDate: hasUnconfirmedReleaseDate(status, b),
              displayDate: formatDate(getDisplayDate(b)),
            };
          })}
          canEdit={canEdit}
          onToggleRead={toggleRead}
          onEdit={startEditBook}
          onDelete={deleteBook}
          onOpenSummary={openSummaryEditor}
          onMoreByAuthor={(author) => setMoreByAuthorTarget(author)}
          // Standalone books never have a series_id, so this is never invoked.
          onViewSeries={() => {}}
          onCheckOnline={(book) => window.open(getCheckOnlineUrl(book), "_blank", "noopener,noreferrer")}
        />
      )}
      <p className="text-xs text-muted-foreground">
        Showing {sortedBooks.length} of {totalBooks} standalone books.
      </p>
      {loading && <p className="text-sm text-muted-foreground">Loading books…</p>}

      {deviceClass === "desktop" ? (
        <EditBookDialog
          bookId={editBookId}
          open={editDialogOpen}
          onOpenChange={(open) => {
            setEditDialogOpen(open);
            if (!open) setEditBookId(null);
          }}
          onSuccess={async () => {
            await fetchBooks();
          }}
        />
      ) : null}

      <BookSummaryDialog
        open={Boolean(summaryEditorBook)}
        bookTitle={summaryEditorBook?.title}
        onOpenChange={(open) => {
          if (!open) setSummaryEditorBook(null);
        }}
        summaryDraft={summaryDraft}
        onSummaryDraftChange={setSummaryDraft}
        notesDraft={notesDraft}
        onNotesDraftChange={setNotesDraft}
        canEdit={canEdit}
        onSave={handleSaveSummaryEditor}
        saving={summarySaving}
        onRefresh={handleFetchSummary}
        refreshing={summaryFetching}
      />

      <MoreByAuthorDialog
        open={Boolean(moreByAuthorTarget)}
        onOpenChange={(open) => {
          if (!open) setMoreByAuthorTarget(null);
        }}
        author={moreByAuthorTarget}
        canEdit={canEdit}
        onBookAdded={() => {
          fetchBooks();
        }}
      />
    </div>
  );
}
