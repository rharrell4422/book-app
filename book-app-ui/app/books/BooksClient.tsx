"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { AlertTriangleIcon, XIcon } from "lucide-react";
import { useToast } from "@/components/ui/use-toast";
import { Button } from "@/components/ui/button";
import { BookActionIcon } from "@/components/books/book-action-icon";
import { publishBookStatusUpdate, subscribeBookStatusUpdates } from "@/lib/book-status-sync";
import { fetchApiWithFallback } from "@/lib/api-client";
import { ValueFilterMenu } from "@/components/value-filter-menu";
import { useAuth } from "@/lib/auth-context";
import {
  type BookStatus,
  formatDate,
  getCheckOnlineUrl,
  getUnifiedBookStatus,
  hasUnconfirmedReleaseDate,
  getStatusChipClass as getStatusChipClassShared,
  normalizeText,
  parseFlexibleDate,
  toDateValue,
} from "@/lib/book-format";
import { AddBookDialog } from "@/components/books/add-book-dialog";
import { EditBookDialog } from "@/components/books/edit-book-dialog";
import { BookSummaryDialog } from "@/components/series/book-summary-dialog";
import { MoreByAuthorDialog } from "@/components/books/more-by-author-dialog";
import { MobileBookList } from "@/components/books/mobile-book-list";
import { useDeviceClass } from "@/hooks/use-device-class";
import { useIsMobile } from "@/hooks/use-mobile";
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
  availability_status?: string | null;
  is_missing?: boolean | null;
  is_upcoming_auto?: boolean | null;
  is_upcoming_final?: boolean | null;
  release_date?: string | null;
  publication_date?: string | null;
  read_date?: string | null;
  series_name?: string | null;
  series_id?: number | null;
  book_number?: number | null;
  source_url?: string | null;
  auto_summary?: string | null;
  notes?: string | null;
  [key: string]: unknown;
};

function getBookStatus(book: BookRow): BookStatus {
  // Delegates to the shared is_read + availability_status derivation (see
  // book-format.ts's getUnifiedBookStatus docstring) -- Phase 3 of the
  // "Two-Axis Status Architecture" design removed the legacy read_status
  // string plus all its fallback heuristics (release-date blind inference,
  // is_upcoming_auto/is_upcoming_final, is_missing, series_id +
  // book_number) as the source for this badge, since those fields are no
  // longer the authoritative source of truth and aren't guaranteed
  // complete for every row the way is_read/availability_status are.
  return getUnifiedBookStatus(book);
}

function getDisplayDate(book: BookRow) {
  const status = getBookStatus(book);
  // release_date and publication_date are two separate stored fields (e.g.
  // Hardcover-sourced candidates populate publication_date, not
  // release_date) -- getBookStatus already falls back to publication_date
  // when deciding upcoming vs. available, so the displayed date needs the
  // same fallback or a book can be correctly classified "available" from
  // its publication_date while still showing a blank date column.
  const releaseOrPublicationDate = book.release_date || book.publication_date;
  return status === "upcoming"
    ? releaseOrPublicationDate || book.read_date
    : book.read_date || releaseOrPublicationDate;
}

function getStatusChipClass(status: string) {
  return getStatusChipClassShared(status, "compact");
}

type BookSortKey = "id" | "title" | "author" | "status" | "date" | "series" | "bookNumber";
type SortDirection = "asc" | "desc";
type ResizableColumnKey = "title" | "author" | "status" | "date" | "series" | "bookNumber" | "actions";

const DEFAULT_COLUMN_WIDTHS: Record<ResizableColumnKey, number> = {
  title: 30,
  author: 21,
  status: 7,
  date: 8,
  series: 16,
  bookNumber: 5,
  actions: 13,
};

const MIN_COLUMN_WIDTH: Record<ResizableColumnKey, number> = {
  title: 14,
  author: 10,
  status: 8,
  date: 8,
  series: 10,
  bookNumber: 5,
  actions: 5,
};

const RESIZE_NEIGHBOR: Record<ResizableColumnKey, ResizableColumnKey | null> = {
  title: "author",
  author: "status",
  status: "date",
  date: "series",
  series: "bookNumber",
  bookNumber: "actions",
  actions: null,
};

const COLUMN_WIDTHS_STORAGE_KEY = "booksTableColumnWidthsV1";

function sanitizeSavedColumnWidths(value: unknown): Record<ResizableColumnKey, number> | null {
  if (!value || typeof value !== "object") return null;
  const candidate = value as Partial<Record<ResizableColumnKey, unknown>>;

  const keys: ResizableColumnKey[] = ["title", "author", "status", "date", "series", "bookNumber", "actions"];
  const next: Partial<Record<ResizableColumnKey, number>> = {};
  let hasAtLeastOneSavedKey = false;

  for (const key of keys) {
    const raw = candidate[key];
    if (typeof raw === "number" && Number.isFinite(raw)) {
      const minimum = MIN_COLUMN_WIDTH[key];
      next[key] = Math.max(minimum, Number(raw));
      hasAtLeastOneSavedKey = true;
    } else {
      next[key] = DEFAULT_COLUMN_WIDTHS[key];
    }
  }

  if (!hasAtLeastOneSavedKey) return null;

  const total = keys.reduce((sum, key) => sum + (next[key] ?? 0), 0);
  if (total <= 0) return null;

  const normalized: Record<ResizableColumnKey, number> = {
    title: Number((((next.title ?? DEFAULT_COLUMN_WIDTHS.title) / total) * 100).toFixed(2)),
    author: Number((((next.author ?? DEFAULT_COLUMN_WIDTHS.author) / total) * 100).toFixed(2)),
    status: Number((((next.status ?? DEFAULT_COLUMN_WIDTHS.status) / total) * 100).toFixed(2)),
    date: Number((((next.date ?? DEFAULT_COLUMN_WIDTHS.date) / total) * 100).toFixed(2)),
    series: Number((((next.series ?? DEFAULT_COLUMN_WIDTHS.series) / total) * 100).toFixed(2)),
    bookNumber: Number((((next.bookNumber ?? DEFAULT_COLUMN_WIDTHS.bookNumber) / total) * 100).toFixed(2)),
    actions: Number((((next.actions ?? DEFAULT_COLUMN_WIDTHS.actions) / total) * 100).toFixed(2)),
  };

  return normalized;
}

export default function BooksClient() {
  const { toast } = useToast();
  const { role } = useAuth();
  const canEdit = role === "owner";
  const isMobile = useIsMobile();
  const deviceClass = useDeviceClass();
  const [books, setBooks] = useState<BookRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [addDialogOpen, setAddDialogOpen] = useState(false);
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [editBookId, setEditBookId] = useState<number | null>(null);
  const [pinnedBookId, setPinnedBookId] = useState<number | null>(null);
  const [summaryEditorBook, setSummaryEditorBook] = useState<BookRow | null>(null);
  const [summaryDraft, setSummaryDraft] = useState("");
  const [notesDraft, setNotesDraft] = useState("");
  const [summaryFetching, setSummaryFetching] = useState(false);
  const [summarySaving, setSummarySaving] = useState(false);
  const [moreByAuthorTarget, setMoreByAuthorTarget] = useState<string | null>(null);
  const [valueFilters, setValueFilters] = useState({
    title: [] as string[],
    author: [] as string[],
    series: [] as string[],
    status: [] as string[],
  });
  const [valueFilterSearch, setValueFilterSearch] = useState({
    title: "",
    author: "",
    series: "",
    status: "",
  });
  const [needsVerificationOnly, setNeedsVerificationOnly] = useState(false);
  const [sortConfig, setSortConfig] = useState<{ key: BookSortKey | null; direction: SortDirection }>({
    key: null,
    direction: "asc",
  });
  const [columnWidths, setColumnWidths] = useState<Record<ResizableColumnKey, number>>(DEFAULT_COLUMN_WIDTHS);
  const tableWrapRef = useRef<HTMLDivElement | null>(null);
  const resizeStateRef = useRef<{
    key: ResizableColumnKey;
    neighborKey: ResizableColumnKey;
    startX: number;
    startWidth: number;
    startNeighborWidth: number;
    containerWidth: number;
  } | null>(null);
  const searchParams = useSearchParams();
  const router = useRouter();
  const seriesId = searchParams.get("series_id");
  const returnTo = searchParams.get("returnTo");
  const pinParam = searchParams.get("pin");

  useEffect(() => {
    const rafId = window.requestAnimationFrame(() => {
      try {
        const saved = window.localStorage.getItem(COLUMN_WIDTHS_STORAGE_KEY);
        if (!saved) return;
        const parsed = JSON.parse(saved);
        const restored = sanitizeSavedColumnWidths(parsed);
        if (restored) {
          setColumnWidths(restored);
        }
      } catch {
        // Ignore storage parse/read errors and keep defaults.
      }
    });

    return () => window.cancelAnimationFrame(rafId);
  }, []);

  useEffect(() => {
    try {
      window.localStorage.setItem(COLUMN_WIDTHS_STORAGE_KEY, JSON.stringify(columnWidths));
    } catch {
      // Ignore storage write errors.
    }
  }, [columnWidths]);

  useEffect(() => {
    if (seriesId) {
      const safeReturnTo = typeof returnTo === "string" && returnTo.startsWith("/")
        ? returnTo
        : null;
      router.replace(safeReturnTo || `/series/${seriesId}`, { scroll: false });
    }
  }, [router, returnTo, seriesId]);

  useEffect(() => {
    if (!pinParam) return;
    const parsed = Number(pinParam);
    if (Number.isFinite(parsed) && parsed > 0) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- URL pin from /add-book
      setPinnedBookId(parsed);
    }
  }, [pinParam]);

  const totalBooks = books.length;
  const statusSummary = books.reduce(
    (acc, book) => {
      const status = getBookStatus(book);
      acc[status] += 1;
      return acc;
    },
    { read: 0, unread: 0, available: 0, upcoming: 0 } as Record<BookStatus, number>,
  );
  const readBooks = statusSummary.read;
  const availableBooks = statusSummary.available;
  const upcomingBooks = statusSummary.upcoming;
  const unreadBooks = statusSummary.unread + statusSummary.available;
  const needsVerificationBooks = books.filter((book) => hasUnconfirmedReleaseDate(getBookStatus(book), book)).length;

  const titleOptions = useMemo(
    () => Array.from(new Set(books.map((book) => String(book.title || "").trim()))).sort((a, b) => a.localeCompare(b)),
    [books],
  );
  const authorOptions = useMemo(
    () => Array.from(new Set(books.map((book) => String(book.author || "").trim()))).sort((a, b) => a.localeCompare(b)),
    [books],
  );
  const seriesOptions = useMemo(
    () => Array.from(new Set(books.map((book) => String(book.series_name || "").trim()))).sort((a, b) => a.localeCompare(b)),
    [books],
  );
  const statusOptions = useMemo(
    () => Array.from(new Set(books.map((book) => String(getBookStatus(book)).trim()))).sort((a, b) => a.localeCompare(b)),
    [books],
  );

  const activeValueFilters = useMemo(() => {
    const titleSet = new Set(titleOptions);
    const authorSet = new Set(authorOptions);
    const seriesSet = new Set(seriesOptions);
    const statusSet = new Set(statusOptions);

    return {
      title: valueFilters.title.filter((value) => titleSet.has(value)),
      author: valueFilters.author.filter((value) => authorSet.has(value)),
      series: valueFilters.series.filter((value) => seriesSet.has(value)),
      status: valueFilters.status.filter((value) => statusSet.has(value)),
    };
  }, [valueFilters, titleOptions, authorOptions, seriesOptions, statusOptions]);

  const filteredBooks = useMemo(() => {
    return books.filter((book) => {
      if (activeValueFilters.title.length > 0 && !activeValueFilters.title.includes(String(book.title || "").trim())) return false;
      if (activeValueFilters.author.length > 0 && !activeValueFilters.author.includes(String(book.author || "").trim())) return false;
      if (activeValueFilters.series.length > 0 && !activeValueFilters.series.includes(String(book.series_name || "").trim())) return false;
      if (activeValueFilters.status.length > 0 && !activeValueFilters.status.includes(String(getBookStatus(book)).trim())) return false;
      if (needsVerificationOnly && !hasUnconfirmedReleaseDate(getBookStatus(book), book)) return false;

      return true;
    });
  }, [books, activeValueFilters, needsVerificationOnly]);

  const sortedBooks = useMemo(() => {
    const withPriorityOrder = [...filteredBooks].sort((a, b) => {
      const statusA = normalizeText(getBookStatus(a));
      const statusB = normalizeText(getBookStatus(b));

      const priority = (status: string) => {
        if (status === "unread") return 0;
        if (status === "available") return 1;
        if (status === "upcoming") return 2;
        if (status === "read") return 3;
        return 4;
      };

      const priorityDelta = priority(statusA) - priority(statusB);
      if (priorityDelta !== 0) {
        return priorityDelta;
      }

      if (statusA === "upcoming") {
        const aRelease = toDateValue(a.release_date || a.publication_date || getDisplayDate(a));
        const bRelease = toDateValue(b.release_date || b.publication_date || getDisplayDate(b));
        if (aRelease !== bRelease) {
          return bRelease - aRelease;
        }
      }

      if (statusA === "read") {
        const aRead = toDateValue(a.read_date || getDisplayDate(a));
        const bRead = toDateValue(b.read_date || getDisplayDate(b));
        if (aRead !== bRead) {
          return bRead - aRead;
        }
      }

      const aId = Number(a.id ?? 0);
      const bId = Number(b.id ?? 0);
      if (aId !== bId) {
        return bId - aId;
      }

      return String(a.title || "").localeCompare(String(b.title || ""), undefined, { sensitivity: "base" });
    });

    const base = !sortConfig.key
      ? withPriorityOrder
      : [...withPriorityOrder].sort((a, b) => {
          const statusA = normalizeText(getBookStatus(a));
          const statusB = normalizeText(getBookStatus(b));
          const priority = (status: string) => {
            if (status === "unread") return 0;
            if (status === "available") return 1;
            if (status === "upcoming") return 2;
            if (status === "read") return 3;
            return 4;
          };

          const priorityDelta = priority(statusA) - priority(statusB);
          if (priorityDelta !== 0) {
            return priorityDelta;
          }

          const key = sortConfig.key;

          const aValue =
            key === "id"
              ? Number(a.id ?? 0)
              : key === "title"
                ? String(a.title || "")
                : key === "author"
                  ? String(a.author || "")
                  : key === "status"
                    ? String(getBookStatus(a) || "")
                    : key === "date"
                      ? parseFlexibleDate(getDisplayDate(a))?.valueOf() ?? 0
                      : key === "series"
                        ? String(a.series_name || "")
                        : Number(a.book_number ?? 0);

          const bValue =
            key === "id"
              ? Number(b.id ?? 0)
              : key === "title"
                ? String(b.title || "")
                : key === "author"
                  ? String(b.author || "")
                  : key === "status"
                    ? String(getBookStatus(b) || "")
                    : key === "date"
                      ? parseFlexibleDate(getDisplayDate(b))?.valueOf() ?? 0
                      : key === "series"
                        ? String(b.series_name || "")
                        : Number(b.book_number ?? 0);

          const keyResult =
            typeof aValue === "number" && typeof bValue === "number"
              ? aValue - bValue
              : String(aValue).localeCompare(String(bValue), undefined, { sensitivity: "base" });

          if (keyResult !== 0) {
            return sortConfig.direction === "asc" ? keyResult : -keyResult;
          }

          if (statusA === "upcoming") {
            const aRelease = toDateValue(a.release_date || a.publication_date || getDisplayDate(a));
            const bRelease = toDateValue(b.release_date || b.publication_date || getDisplayDate(b));
            if (aRelease !== bRelease) {
              return bRelease - aRelease;
            }
          }

          if (statusA === "read") {
            const aRead = toDateValue(a.read_date || getDisplayDate(a));
            const bRead = toDateValue(b.read_date || getDisplayDate(b));
            if (aRead !== bRead) {
              return bRead - aRead;
            }
          }

          return Number(b.id ?? 0) - Number(a.id ?? 0);
        });

    const sorted = base;

    if (pinnedBookId === null) {
      return sorted;
    }

    const pinnedIndex = sorted.findIndex((book) => Number(book?.id) === pinnedBookId);
    if (pinnedIndex <= 0) {
      return sorted;
    }

    const next = [...sorted];
    const [pinned] = next.splice(pinnedIndex, 1);
    next.unshift(pinned);
    return next;
  }, [filteredBooks, sortConfig, pinnedBookId]);

  function toggleSort(key: BookSortKey) {
    setSortConfig((prev) => {
      if (prev.key !== key) {
        return { key, direction: "asc" };
      }
      if (prev.direction === "asc") {
        return { key, direction: "desc" };
      }
      return { key: null, direction: "asc" };
    });
  }

  function sortLabel(key: BookSortKey) {
    if (sortConfig.key !== key) return "";
    return sortConfig.direction === "asc" ? " ▲" : " ▼";
  }

  function setValueFilter(kind: "title" | "author" | "series" | "status", values: string[]) {
    setValueFilters((prev) => ({
      ...prev,
      [kind]: values,
    }));
  }

  function clearFilters() {
    setValueFilters({ title: [], author: [], series: [], status: [] });
    setValueFilterSearch({ title: "", author: "", series: "", status: "" });
    setSortConfig({ key: null, direction: "asc" });
    setNeedsVerificationOnly(false);
  }

  useEffect(() => {
    const handleMouseMove = (event: MouseEvent) => {
      const active = resizeStateRef.current;
      if (!active) return;

      const deltaX = event.clientX - active.startX;
      const deltaPercent = (deltaX / active.containerWidth) * 100;
      const minCurrent = MIN_COLUMN_WIDTH[active.key];
      const minNeighbor = MIN_COLUMN_WIDTH[active.neighborKey];
      const maxCurrent = active.startWidth + active.startNeighborWidth - minNeighbor;
      const nextCurrentWidth = Math.min(maxCurrent, Math.max(minCurrent, active.startWidth + deltaPercent));
      const nextNeighborWidth = active.startNeighborWidth - (nextCurrentWidth - active.startWidth);

      setColumnWidths((prev) => ({
        ...prev,
        [active.key]: Number(nextCurrentWidth.toFixed(2)),
        [active.neighborKey]: Number(nextNeighborWidth.toFixed(2)),
      }));
    };

    const handleMouseUp = () => {
      resizeStateRef.current = null;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };

    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp);

    return () => {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
    };
  }, []);

  function startColumnResize(key: ResizableColumnKey, event: React.MouseEvent<HTMLButtonElement>) {
    const neighborKey = RESIZE_NEIGHBOR[key];
    const containerWidth = tableWrapRef.current?.getBoundingClientRect().width ?? 0;
    if (!neighborKey || containerWidth <= 0) return;

    event.preventDefault();
    event.stopPropagation();

    resizeStateRef.current = {
      key,
      neighborKey,
      startX: event.clientX,
      startWidth: columnWidths[key],
      startNeighborWidth: columnWidths[neighborKey],
      containerWidth,
    };

    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }

  const fetchBooks = useCallback(async () => {
    setLoading(true);
    try {
      const path = seriesId ? `/books/by_series/${seriesId}` : "/books/";
      const response = await fetchApiWithFallback(path, { cache: "no-store" });
      const data = await response.json();
      setBooks(data);
    } catch (error) {
      console.error("Error fetching books:", error);
    } finally {
      setLoading(false);
    }
  }, [seriesId]);

  useEffect(() => {
    if (seriesId) return;
    // fetchBooks sets loading state synchronously before its first await --
    // standard "fetch on mount" pattern, not derived state that could be
    // computed during render.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    fetchBooks();
  }, [seriesId, fetchBooks]);

  useEffect(() => {
    const unsubscribe = subscribeBookStatusUpdates((payload) => {
      setBooks((prev) => {
        if (String(payload.record_status || "").toLowerCase() === "deleted") {
          return prev.filter((book) => book.id !== payload.id);
        }
        return prev.map((book) =>
          book.id === payload.id
            ? {
                ...book,
                ...payload,
              }
            : book,
        );
      });
    });

    return unsubscribe;
  }, []);

  async function toggleRead(book: BookRow) {
    const nextIsRead = !book.is_read;

    try {
      const response = await fetchApiWithFallback(`/books/${book.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          // availability_status is deliberately omitted -- this quick
          // action only ever touches the reading axis (is_read/read_date).
          // The backend re-derives read_status from is_read plus whatever
          // this book's availability_status already is (see crud.books._
          // apply_availability_bridge_for_update), so unmarking a book as
          // read can never silently reclassify its availability the way a
          // date/series-number heuristic here used to.
          is_read: nextIsRead,
          read_date: nextIsRead ? new Date().toISOString().split("T")[0] : null,
        }),
      });

      if (response.ok) {
        const updatedBook = await response.json();
        setBooks((prev) =>
          prev.map((item) => (item.id === updatedBook.id ? { ...item, ...updatedBook } : item)),
        );
        publishBookStatusUpdate(updatedBook);
        toast({
          title: "Updated",
          description: `Marked book ${book.id} as ${nextIsRead ? "read" : "unread"}.`,
        });
      } else {
        toast({
          title: "Error",
          description: "Failed to update book.",
        });
      }
    } catch (error) {
      console.error("Error updating book:", error);
    }
  }

  async function deleteBook(bookId: number) {
    if (!confirm("Delete this book?")) return;

    try {
      const response = await fetchApiWithFallback(`/books/${bookId}`, {
        method: "DELETE",
      });

      if (response.ok) {
        toast({
          title: "Deleted",
          description: `Book ${bookId} removed.`,
        });
        fetchBooks();
      } else {
        toast({
          title: "Error",
          description: "Failed to delete book.",
        });
      }
    } catch (error) {
      console.error("Error deleting book:", error);
    }
  }

  // Standalone (non-series) books have no "Series Recap" equivalent to lean
  // on, so this is the one place in the app they can still get an AI
  // summary -- series books rely on the series page's Series Recap instead.
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
      setBooks((prev) => prev.map((item) => (item.id === updatedBook.id ? { ...item, ...updatedBook } : item)));
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
      setBooks((prev) => prev.map((item) => (item.id === updatedBook.id ? { ...item, ...updatedBook } : item)));
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

  function startEditBook(book: BookRow) {
    if (getDeviceClass() === "desktop") {
      setEditBookId(Number(book.id));
      setEditDialogOpen(true);
    } else {
      router.push(`/edit-book/${book.id}`);
    }
  }

  if (seriesId) {
    return <div className="p-4 text-sm text-muted-foreground">Redirecting to series detail...</div>;
  }

  return (
    <div className="p-2 space-y-1.5">
      <div className="space-y-1.5 rounded-lg border bg-card/60 px-3 py-2">
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
            <h1 className="text-xl font-bold leading-tight">
              {seriesId ? `Series ${seriesId} books` : "All books"}
            </h1>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-muted-foreground">
              <span className="inline-flex items-center gap-1">
                <span className="inline-flex rounded-full border border-sky-300 bg-sky-100 px-1.5 py-0 text-[10px] font-semibold uppercase tracking-wide text-sky-800">
                  available
                </span>
                <span>released and unread</span>
              </span>
              <span className="inline-flex items-center gap-1">
                <span className="inline-flex rounded-full border border-rose-300 bg-rose-100 px-1.5 py-0 text-[10px] font-semibold uppercase tracking-wide text-rose-800">
                  upcoming
                </span>
                <span>planned for a future release</span>
              </span>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
            <span>Unread <span className="font-semibold text-foreground">{unreadBooks}</span></span>
            <span>Read <span className="font-semibold text-foreground">{readBooks}</span></span>
            <span>Available <span className="font-semibold text-foreground">{availableBooks}</span></span>
            <span>Total <span className="font-semibold text-foreground">{totalBooks}</span></span>
            <span>Upcoming <span className="font-semibold text-foreground">{upcomingBooks}</span></span>
            {needsVerificationBooks > 0 ? (
              <button
                type="button"
                title="Books flagged as upcoming/available with no confirmed release date -- click to filter to just these"
                onClick={() => setNeedsVerificationOnly((prev) => !prev)}
                className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-semibold transition-colors ${
                  needsVerificationOnly
                    ? "border-amber-400 bg-amber-200 text-amber-900"
                    : "border-amber-300 bg-amber-100 text-amber-800 hover:bg-amber-200"
                }`}
              >
                <AlertTriangleIcon className="h-3 w-3" />
                Needs date verification {needsVerificationBooks}
              </button>
            ) : null}
          </div>
        </div>

        <div className="flex flex-wrap gap-2">
          {canEdit ? (
            <Button
              type="button"
              onClick={() => {
                // Read matchMedia at click time so a phone/tablet tap during
                // the desktop server snapshot cannot open the Radix dialog.
                if (getDeviceClass() === "desktop") {
                  setAddDialogOpen(true);
                } else {
                  router.push("/add-book");
                }
              }}
            >
              Add Book
            </Button>
          ) : null}
          <Link href="/books">
            <Button type="button" variant="outline">All Books</Button>
          </Link>
          <Link href="/series">
            <Button type="button" variant="secondary">Series</Button>
          </Link>
          <Link href="/books/standalone">
            <Button type="button" variant="secondary">Standalone Books</Button>
          </Link>
        </div>
      </div>

      {isMobile ? (
        <MobileBookList
          items={sortedBooks.map((b) => {
            const status = getBookStatus(b);
            return {
              book: b,
              status,
              statusChipClass: getStatusChipClass(status),
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
          onViewSeries={(seriesId) => router.push(`/series/${seriesId}`)}
          onCheckOnline={(book) => window.open(getCheckOnlineUrl(book), "_blank", "noopener,noreferrer")}
        />
      ) : (
      <div ref={tableWrapRef} className="overflow-x-auto rounded-lg border bg-card/80">
        <Table className="w-full min-w-[880px] table-fixed text-sm [&_th]:h-9 [&_th]:py-1 [&_td]:py-1">
          <TableHeader>
            <TableRow>
              <TableHead className="relative" style={{ width: `${columnWidths.title}%` }}>
                <div className="flex items-center justify-between gap-1 pr-2">
                  <button type="button" className="truncate text-left hover:underline" onClick={() => toggleSort("title")}>
                    Title{sortLabel("title")}
                  </button>
                  <ValueFilterMenu
                    label="Title"
                    options={titleOptions}
                    selectedValues={activeValueFilters.title}
                    onApplyValues={(values) => setValueFilter("title", values)}
                    onClear={() => {
                      setValueFilters((prev) => ({ ...prev, title: [] }));
                      setValueFilterSearch((prev) => ({ ...prev, title: "" }));
                    }}
                    searchValue={valueFilterSearch.title}
                    onSearchChange={(value) => setValueFilterSearch((prev) => ({ ...prev, title: value }))}
                  />
                </div>
                <button
                  type="button"
                  aria-label="Resize Title column"
                  onMouseDown={(event) => startColumnResize("title", event)}
                  className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
                />
              </TableHead>
              <TableHead className="relative" style={{ width: `${columnWidths.author}%` }}>
                <div className="flex items-center justify-between gap-1 pr-2">
                  <button type="button" className="truncate text-left hover:underline" onClick={() => toggleSort("author")}>
                    Author{sortLabel("author")}
                  </button>
                  <ValueFilterMenu
                    label="Author"
                    options={authorOptions}
                    selectedValues={activeValueFilters.author}
                    onApplyValues={(values) => setValueFilter("author", values)}
                    onClear={() => {
                      setValueFilters((prev) => ({ ...prev, author: [] }));
                      setValueFilterSearch((prev) => ({ ...prev, author: "" }));
                    }}
                    searchValue={valueFilterSearch.author}
                    onSearchChange={(value) => setValueFilterSearch((prev) => ({ ...prev, author: value }))}
                  />
                </div>
                <button
                  type="button"
                  aria-label="Resize Author column"
                  onMouseDown={(event) => startColumnResize("author", event)}
                  className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
                />
              </TableHead>
              <TableHead className="relative" style={{ width: `${columnWidths.status}%` }}>
                <div className="flex items-center justify-between gap-1 pr-2">
                  <button type="button" className="truncate text-left hover:underline" onClick={() => toggleSort("status")}>
                    Status{sortLabel("status")}
                  </button>
                  <ValueFilterMenu
                    label="Status"
                    options={statusOptions}
                    selectedValues={activeValueFilters.status}
                    onApplyValues={(values) => setValueFilter("status", values)}
                    onClear={() => {
                      setValueFilters((prev) => ({ ...prev, status: [] }));
                      setValueFilterSearch((prev) => ({ ...prev, status: "" }));
                    }}
                    searchValue={valueFilterSearch.status}
                    onSearchChange={(value) => setValueFilterSearch((prev) => ({ ...prev, status: value }))}
                  />
                </div>
                <button
                  type="button"
                  aria-label="Resize Status column"
                  onMouseDown={(event) => startColumnResize("status", event)}
                  className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
                />
              </TableHead>
              <TableHead className="relative" style={{ width: `${columnWidths.date}%` }}>
                <button type="button" className="text-left hover:underline" onClick={() => toggleSort("date")}>Date{sortLabel("date")}</button>
                <button
                  type="button"
                  aria-label="Resize Date column"
                  onMouseDown={(event) => startColumnResize("date", event)}
                  className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
                />
              </TableHead>
              <TableHead className="relative" style={{ width: `${columnWidths.series}%` }}>
                <div className="flex items-center justify-between gap-1 pr-2">
                  <button type="button" className="truncate text-left hover:underline" onClick={() => toggleSort("series")}>
                    Series{sortLabel("series")}
                  </button>
                  <ValueFilterMenu
                    label="Series"
                    options={seriesOptions}
                    selectedValues={activeValueFilters.series}
                    onApplyValues={(values) => setValueFilter("series", values)}
                    onClear={() => {
                      setValueFilters((prev) => ({ ...prev, series: [] }));
                      setValueFilterSearch((prev) => ({ ...prev, series: "" }));
                    }}
                    searchValue={valueFilterSearch.series}
                    onSearchChange={(value) => setValueFilterSearch((prev) => ({ ...prev, series: value }))}
                  />
                </div>
                <button
                  type="button"
                  aria-label="Resize Series column"
                  onMouseDown={(event) => startColumnResize("series", event)}
                  className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
                />
              </TableHead>
              <TableHead className="relative" style={{ width: `${columnWidths.bookNumber}%` }}>
                <button type="button" className="text-left hover:underline" onClick={() => toggleSort("bookNumber")}>Book #{sortLabel("bookNumber")}</button>
                <button
                  type="button"
                  aria-label="Resize Book number column"
                  onMouseDown={(event) => startColumnResize("bookNumber", event)}
                  className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
                />
              </TableHead>
              <TableHead className="relative" style={{ width: `${columnWidths.actions}%` }}>
                <div className="flex items-center justify-between gap-1 pr-2">
                  Actions
                  <Button type="button" variant="ghost" size="icon-xs" title="Clear all filters and sorting" onClick={clearFilters}>
                    <XIcon />
                  </Button>
                </div>
                <button
                  type="button"
                  aria-label="Resize Actions column"
                  onMouseDown={(event) => startColumnResize("actions", event)}
                  className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
                />
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {Array.isArray(sortedBooks) &&
              sortedBooks.map((b) => {
                const status = getBookStatus(b);
                const unconfirmedDate = hasUnconfirmedReleaseDate(status, b);
                return (
                  <TableRow key={b.id}>
                    <TableCell className="truncate" title={b.title ?? undefined}>{b.title || "—"}</TableCell>
                    <TableCell className="truncate" title={b.author || "—"}>{b.author || "—"}</TableCell>
                    <TableCell>
                      <div className="flex items-center gap-1">
                        <span className={getStatusChipClass(status)}>{status}</span>
                        {unconfirmedDate ? (
                          <span title="No confirmed release date yet -- click the check-online icon to verify with the retailer">
                            <AlertTriangleIcon className="h-3.5 w-3.5 shrink-0 text-amber-600" aria-label="Date unconfirmed" />
                          </span>
                        ) : null}
                      </div>
                    </TableCell>
                    <TableCell>{formatDate(getDisplayDate(b))}</TableCell>
                    <TableCell className="truncate" title={b.series_name || "—"}>{b.series_name || "—"}</TableCell>
                    <TableCell>{b.book_number ?? "—"}</TableCell>
                    <TableCell className="whitespace-nowrap">
                      <div className="flex items-center gap-0.5">
                      {b.series_id ? (
                        <BookActionIcon state="series" onClick={() => router.push(`/series/${b.series_id}`)} />
                      ) : (
                        // Standalone books have no series page/Series Recap to lean
                        // on, so this is their one AI-summary entry point -- series
                        // books intentionally don't get this button here.
                        <BookActionIcon
                          state={b.auto_summary || b.notes ? "summaryStandaloneHasContent" : "summaryStandaloneEmpty"}
                          onClick={() => openSummaryEditor(b)}
                        />
                      )}
                      <BookActionIcon
                        state={unconfirmedDate ? "unconfirmedDate" : b.source_url ? "hasSourceUrl" : "missingSourceUrl"}
                        onClick={() => window.open(getCheckOnlineUrl(b), "_blank", "noopener,noreferrer")}
                      />
                      <BookActionIcon state="moreByAuthor" onClick={() => setMoreByAuthorTarget(String(b.author || ""))} />
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
      )}
      <p className="text-xs text-muted-foreground">
        Showing {sortedBooks.length} of {books.length} books.
      </p>
      {loading && <p className="text-sm text-muted-foreground">Loading books…</p>}

      {deviceClass === "desktop" ? (
        <AddBookDialog
          open={addDialogOpen}
          onOpenChange={setAddDialogOpen}
          onSuccess={async (createdBook) => {
            const id = Number(createdBook?.id);
            if (Number.isFinite(id) && id > 0) {
              setPinnedBookId(id);
            }
            await fetchBooks();
          }}
        />
      ) : null}

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
