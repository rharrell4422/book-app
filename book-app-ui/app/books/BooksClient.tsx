"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { useToast } from "@/components/ui/use-toast";
import { Button } from "@/components/ui/button";
import { publishBookStatusUpdate, subscribeBookStatusUpdates } from "@/lib/book-status-sync";

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

function getBookStatus(book: any) {
  if (book.read_status) {
    return String(book.read_status);
  }
  if (book.is_read) {
    return "read";
  }

  const releaseDate = book.release_date || book.publication_date;
  if (releaseDate) {
    const parsedDate = new Date(releaseDate);
    if (!Number.isNaN(parsedDate.valueOf())) {
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      parsedDate.setHours(0, 0, 0, 0);
      if (parsedDate > today) {
        return "upcoming";
      }
    }
  }

  return "unread";
}

function getDisplayDate(book: any) {
  const status = getBookStatus(book);
  return status === "upcoming"
    ? book.release_date || book.read_date
    : book.read_date || book.release_date;
}

function formatDate(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleDateString();
}

function getStatusChipClass(status: string) {
  if (status === "read") {
    return "inline-flex rounded-full border border-emerald-300 bg-emerald-100 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-emerald-800";
  }
  return "inline-flex rounded-full border border-rose-300 bg-rose-100 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-rose-800";
}

function normalizeText(value: unknown) {
  return String(value ?? "").trim().toLowerCase();
}

function parseFlexibleDate(value?: string | null): Date | null {
  if (!value) return null;

  const raw = String(value).trim();
  if (!raw) return null;

  const nativeParsed = new Date(raw);
  if (!Number.isNaN(nativeParsed.valueOf())) {
    return nativeParsed;
  }

  const mdyMatch = raw.match(/^(\d{1,2})[-\/](\d{1,2})[-\/](\d{2,4})$/);
  if (mdyMatch) {
    const month = Number(mdyMatch[1]);
    const day = Number(mdyMatch[2]);
    const yearRaw = Number(mdyMatch[3]);
    const year = yearRaw < 100 ? 2000 + yearRaw : yearRaw;
    const date = new Date(year, month - 1, day);
    if (!Number.isNaN(date.valueOf())) {
      return date;
    }
  }

  const ymdMatch = raw.match(/^(\d{4})[-\/](\d{1,2})[-\/](\d{1,2})$/);
  if (ymdMatch) {
    const year = Number(ymdMatch[1]);
    const month = Number(ymdMatch[2]);
    const day = Number(ymdMatch[3]);
    const date = new Date(year, month - 1, day);
    if (!Number.isNaN(date.valueOf())) {
      return date;
    }
  }

  return null;
}

type ValueFilterMenuProps = {
  label: string;
  options: string[];
  selectedValues: string[];
  onToggleValue: (value: string) => void;
  onClear: () => void;
  searchValue: string;
  onSearchChange: (value: string) => void;
};

function ValueFilterMenu({
  label,
  options,
  selectedValues,
  onToggleValue,
  onClear,
  searchValue,
  onSearchChange,
}: ValueFilterMenuProps) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const normalizedSearch = normalizeText(searchValue);
  const visibleOptions = options.filter((option) => normalizeText(option).includes(normalizedSearch));

  useEffect(() => {
    if (!open) return;

    const handleDocumentMouseDown = (event: MouseEvent) => {
      if (!menuRef.current) return;
      if (!menuRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };

    const handleDocumentKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
      }
    };

    document.addEventListener("mousedown", handleDocumentMouseDown);
    document.addEventListener("keydown", handleDocumentKeyDown);
    return () => {
      document.removeEventListener("mousedown", handleDocumentMouseDown);
      document.removeEventListener("keydown", handleDocumentKeyDown);
    };
  }, [open]);

  return (
    <div className="relative" ref={menuRef}>
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        className="h-8 w-full rounded border bg-background px-2 text-left text-xs"
      >
        {label} {selectedValues.length > 0 ? `(${selectedValues.length})` : ""}
      </button>
      {open ? (
        <div className="absolute z-20 mt-1 w-60 rounded-md border bg-background p-2 shadow-lg">
        <input
          value={searchValue}
          onChange={(event) => onSearchChange(event.target.value)}
          placeholder="Search values"
          className="mb-2 h-8 w-full rounded border bg-background px-2 text-xs"
        />
        <div className="max-h-40 space-y-1 overflow-auto pr-1">
          {visibleOptions.map((option) => {
            const checked = selectedValues.includes(option);
            return (
              <label key={option} className="flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => {
                    onToggleValue(option);
                    setOpen(false);
                  }}
                />
                <span className="truncate">{option || "(blank)"}</span>
              </label>
            );
          })}
          {visibleOptions.length === 0 ? (
            <p className="text-xs text-muted-foreground">No matching values.</p>
          ) : null}
        </div>
        <div className="mt-2 flex justify-end">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              onClear();
              setOpen(false);
            }}
          >
            Clear
          </Button>
        </div>
      </div>
      ) : null}
    </div>
  );
}

type BookSortKey = "id" | "title" | "author" | "status" | "date" | "series" | "bookNumber";
type SortDirection = "asc" | "desc";

export default function BooksClient() {
  const { toast } = useToast();
  const [books, setBooks] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [filters, setFilters] = useState({
    id: "",
    title: "",
    author: "",
    status: "all",
    series: "",
    bookNumber: "",
  });
  const [valueFilters, setValueFilters] = useState({
    author: [] as string[],
    series: [] as string[],
    status: [] as string[],
  });
  const [valueFilterSearch, setValueFilterSearch] = useState({
    author: "",
    series: "",
    status: "",
  });
  const [sortConfig, setSortConfig] = useState<{ key: BookSortKey | null; direction: SortDirection }>({
    key: null,
    direction: "asc",
  });
  const [dateFilterMode, setDateFilterMode] = useState<"any" | "before" | "after" | "between">("any");
  const [dateFilterFrom, setDateFilterFrom] = useState("");
  const [dateFilterTo, setDateFilterTo] = useState("");
  const searchParams = useSearchParams();
  const router = useRouter();
  const seriesId = searchParams.get("series_id");

  const totalBooks = books.length;
  const readBooks = books.filter((book) => book.is_read).length;
  const unreadBooks = books.filter((book) => !book.is_read).length;
  const upcomingBooks = books.filter((book) => getBookStatus(book) === "upcoming").length;

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

  function passesDateFilter(book: any): boolean {
    if (dateFilterMode === "any") return true;
    const dateValue = parseFlexibleDate(getDisplayDate(book));
    if (!dateValue) return false;

    const fromDate = parseFlexibleDate(dateFilterFrom);
    const toDate = parseFlexibleDate(dateFilterTo);

    if (dateFilterMode === "before") {
      return toDate ? dateValue <= toDate : true;
    }
    if (dateFilterMode === "after") {
      return fromDate ? dateValue >= fromDate : true;
    }
    if (dateFilterMode === "between") {
      const afterFrom = fromDate ? dateValue >= fromDate : true;
      const beforeTo = toDate ? dateValue <= toDate : true;
      return afterFrom && beforeTo;
    }

    return true;
  }

  const filteredBooks = useMemo(() => {
    const idFilter = normalizeText(filters.id);
    const titleFilter = normalizeText(filters.title);
    const authorFilter = normalizeText(filters.author);
    const statusFilter = normalizeText(filters.status);
    const seriesFilter = normalizeText(filters.series);
    const bookNumberFilter = normalizeText(filters.bookNumber);

    return books.filter((book) => {
      const status = normalizeText(getBookStatus(book));
      const idText = normalizeText(book.id);
      const titleText = normalizeText(book.title);
      const authorText = normalizeText(book.author);
      const seriesText = normalizeText(book.series_name);
      const bookNumberText = normalizeText(book.book_number);

      if (idFilter && !idText.includes(idFilter)) return false;
      if (titleFilter && !titleText.includes(titleFilter)) return false;
      if (authorFilter && !authorText.includes(authorFilter)) return false;
      if (statusFilter !== "all" && status !== statusFilter) return false;
      if (seriesFilter && !seriesText.includes(seriesFilter)) return false;
      if (bookNumberFilter && !bookNumberText.includes(bookNumberFilter)) return false;
      if (valueFilters.author.length > 0 && !valueFilters.author.includes(String(book.author || "").trim())) return false;
      if (valueFilters.series.length > 0 && !valueFilters.series.includes(String(book.series_name || "").trim())) return false;
      if (valueFilters.status.length > 0 && !valueFilters.status.includes(String(getBookStatus(book)).trim())) return false;
      if (!passesDateFilter(book)) return false;

      return true;
    });
  }, [books, filters, valueFilters, dateFilterMode, dateFilterFrom, dateFilterTo]);

  const sortedBooks = useMemo(() => {
    if (!sortConfig.key) return filteredBooks;

    const sorted = [...filteredBooks].sort((a, b) => {
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

      if (typeof aValue === "number" && typeof bValue === "number") {
        return aValue - bValue;
      }

      return String(aValue).localeCompare(String(bValue), undefined, { sensitivity: "base" });
    });

    return sortConfig.direction === "asc" ? sorted : sorted.reverse();
  }, [filteredBooks, sortConfig]);

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

  function setExplicitSort(key: BookSortKey, mode: "none" | "asc" | "desc") {
    if (mode === "none") {
      setSortConfig({ key: null, direction: "asc" });
      return;
    }
    setSortConfig({ key, direction: mode });
  }

  function toggleValueFilter(kind: "author" | "series" | "status", value: string) {
    setValueFilters((prev) => {
      const exists = prev[kind].includes(value);
      return {
        ...prev,
        [kind]: exists ? prev[kind].filter((item) => item !== value) : [...prev[kind], value],
      };
    });
  }

  function clearFilters() {
    setFilters({
      id: "",
      title: "",
      author: "",
      status: "all",
      series: "",
      bookNumber: "",
    });
    setValueFilters({ author: [], series: [], status: [] });
    setValueFilterSearch({ author: "", series: "", status: "" });
    setDateFilterMode("any");
    setDateFilterFrom("");
    setDateFilterTo("");
  }

  async function fetchBooks() {
    setLoading(true);
    try {
      const url = seriesId
        ? `http://localhost:8000/books/by_series/${seriesId}`
        : "http://localhost:8000/books/";

      const response = await fetch(url, { cache: "no-store" });
      const data = await response.json();
      setBooks(data);
    } catch (error) {
      console.error("Error fetching books:", error);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchBooks();
  }, [seriesId]);

  useEffect(() => {
    const unsubscribe = subscribeBookStatusUpdates((payload) => {
      setBooks((prev) =>
        prev.map((book) =>
          book.id === payload.id
            ? {
                ...book,
                is_read: payload.is_read,
                read_status: payload.read_status,
                read_date: payload.read_date,
                release_date: payload.release_date,
                publication_date: payload.publication_date,
              }
            : book,
        ),
      );
    });

    return unsubscribe;
  }, []);

  async function toggleRead(book: any) {
    const nextIsRead = !book.is_read;
    const releaseDate = book.release_date || book.publication_date;
    let nextStatus = nextIsRead ? "read" : "unread";
    if (!nextIsRead && releaseDate) {
      const parsedDate = new Date(releaseDate);
      if (!Number.isNaN(parsedDate.valueOf())) {
        const today = new Date();
        today.setHours(0, 0, 0, 0);
        parsedDate.setHours(0, 0, 0, 0);
        if (parsedDate > today) {
          nextStatus = "upcoming";
        }
      }
    }

    try {
      const response = await fetch(`http://localhost:8000/books/${book.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          is_read: nextIsRead,
          read_status: nextStatus,
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
      const response = await fetch(`http://localhost:8000/books/${bookId}`, {
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

  return (
    <div className="p-4 space-y-3">
      <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground">
            Library
          </p>
          <h1 className="text-2xl font-bold">
            {seriesId ? `Series ${seriesId} books` : "All books"}
          </h1>
          <p className="max-w-2xl text-xs leading-5 text-muted-foreground">
            Browse the collection with read status, release dates, and series links.
          </p>
        </div>

        <div className="flex flex-wrap gap-2">
          <Link href="/books">
            <Button type="button" variant="outline">All Books</Button>
          </Link>
          <Link href="/series">
            <Button type="button" variant="secondary">Series</Button>
          </Link>
        </div>
      </div>

      <div className="rounded-md border bg-card/70 px-3 py-2">
        <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
          <div className="rounded bg-background/70 px-2 py-1">
            <span className="text-muted-foreground">Total</span>
            <span className="ml-2 font-semibold">{totalBooks}</span>
          </div>
          <div className="rounded bg-background/70 px-2 py-1">
            <span className="text-muted-foreground">Read</span>
            <span className="ml-2 font-semibold">{readBooks}</span>
          </div>
          <div className="rounded bg-background/70 px-2 py-1">
            <span className="text-muted-foreground">Unread</span>
            <span className="ml-2 font-semibold">{unreadBooks}</span>
          </div>
          <div className="rounded bg-background/70 px-2 py-1">
            <span className="text-muted-foreground">Upcoming</span>
            <span className="ml-2 font-semibold">{upcomingBooks}</span>
          </div>
        </div>
      </div>

      <div className="overflow-x-auto rounded-lg border bg-card/80">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>
                <button type="button" className="text-left" onClick={() => toggleSort("id")}>ID{sortLabel("id")}</button>
              </TableHead>
              <TableHead>
                <button type="button" className="text-left" onClick={() => toggleSort("title")}>Title{sortLabel("title")}</button>
              </TableHead>
              <TableHead>
                <button type="button" className="text-left" onClick={() => toggleSort("author")}>Author{sortLabel("author")}</button>
              </TableHead>
              <TableHead>
                <button type="button" className="text-left" onClick={() => toggleSort("status")}>Status{sortLabel("status")}</button>
              </TableHead>
              <TableHead className="min-w-[220px]">
                <button type="button" className="text-left" onClick={() => toggleSort("date")}>Date{sortLabel("date")}</button>
              </TableHead>
              <TableHead>
                <button type="button" className="text-left" onClick={() => toggleSort("series")}>Series{sortLabel("series")}</button>
              </TableHead>
              <TableHead>
                <button type="button" className="text-left" onClick={() => toggleSort("bookNumber")}>Book #{sortLabel("bookNumber")}</button>
              </TableHead>
              <TableHead>Actions</TableHead>
            </TableRow>
            <TableRow>
              <TableHead>
                <input
                  value={filters.id}
                  onChange={(event) => setFilters((prev) => ({ ...prev, id: event.target.value }))}
                  placeholder="Filter"
                  className="h-8 w-full rounded border bg-background px-2 text-xs"
                />
              </TableHead>
              <TableHead>
                <input
                  value={filters.title}
                  onChange={(event) => setFilters((prev) => ({ ...prev, title: event.target.value }))}
                  placeholder="Filter"
                  className="h-8 w-full rounded border bg-background px-2 text-xs"
                />
              </TableHead>
              <TableHead>
                <input
                  value={filters.author}
                  onChange={(event) => setFilters((prev) => ({ ...prev, author: event.target.value }))}
                  placeholder="Filter"
                  className="h-8 w-full rounded border bg-background px-2 text-xs"
                />
                <div className="mt-1">
                  <ValueFilterMenu
                    label="Values"
                    options={authorOptions}
                    selectedValues={valueFilters.author}
                    onToggleValue={(value) => toggleValueFilter("author", value)}
                    onClear={() => {
                      setValueFilters((prev) => ({ ...prev, author: [] }));
                      setValueFilterSearch((prev) => ({ ...prev, author: "" }));
                      setFilters((prev) => ({ ...prev, author: "" }));
                    }}
                    searchValue={valueFilterSearch.author}
                    onSearchChange={(value) => setValueFilterSearch((prev) => ({ ...prev, author: value }))}
                  />
                </div>
              </TableHead>
              <TableHead>
                <div className="space-y-1">
                  <select
                    value={filters.status}
                    onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
                    className="h-8 w-full rounded border bg-background px-2 text-xs"
                  >
                    <option value="all">All</option>
                    <option value="read">Read</option>
                    <option value="unread">Unread</option>
                    <option value="upcoming">Upcoming</option>
                  </select>
                  <ValueFilterMenu
                    label="Values"
                    options={statusOptions}
                    selectedValues={valueFilters.status}
                    onToggleValue={(value) => toggleValueFilter("status", value)}
                    onClear={() => {
                      setValueFilters((prev) => ({ ...prev, status: [] }));
                      setValueFilterSearch((prev) => ({ ...prev, status: "" }));
                      setFilters((prev) => ({ ...prev, status: "all" }));
                    }}
                    searchValue={valueFilterSearch.status}
                    onSearchChange={(value) => setValueFilterSearch((prev) => ({ ...prev, status: value }))}
                  />
                </div>
              </TableHead>
              <TableHead className="min-w-[220px] align-top">
                <div className="space-y-1">
                  <select
                    value={sortConfig.key === "date" ? sortConfig.direction : "none"}
                    onChange={(event) =>
                      setExplicitSort("date", event.target.value as "none" | "asc" | "desc")
                    }
                    className="block h-8 w-full rounded border bg-background px-2 text-xs"
                  >
                    <option value="none">Sort date</option>
                    <option value="asc">A to Z (oldest)</option>
                    <option value="desc">Z to A (newest)</option>
                  </select>
                  <select
                    value={dateFilterMode}
                    onChange={(event) => setDateFilterMode(event.target.value as "any" | "before" | "after" | "between")}
                    className="block h-8 w-full rounded border bg-background px-2 text-xs"
                  >
                    <option value="any">Any date</option>
                    <option value="after">After</option>
                    <option value="before">Before</option>
                    <option value="between">Between</option>
                  </select>
                  {dateFilterMode === "after" || dateFilterMode === "between" ? (
                    <input
                      type="date"
                      value={dateFilterFrom}
                      onChange={(event) => setDateFilterFrom(event.target.value)}
                      className="block h-8 w-full rounded border bg-background px-2 text-xs"
                    />
                  ) : null}
                  {dateFilterMode === "before" || dateFilterMode === "between" ? (
                    <input
                      type="date"
                      value={dateFilterTo}
                      onChange={(event) => setDateFilterTo(event.target.value)}
                      className="block h-8 w-full rounded border bg-background px-2 text-xs"
                    />
                  ) : null}
                </div>
              </TableHead>
              <TableHead>
                <input
                  value={filters.series}
                  onChange={(event) => setFilters((prev) => ({ ...prev, series: event.target.value }))}
                  placeholder="Filter"
                  className="h-8 w-full rounded border bg-background px-2 text-xs"
                />
                <div className="mt-1">
                  <ValueFilterMenu
                    label="Values"
                    options={seriesOptions}
                    selectedValues={valueFilters.series}
                    onToggleValue={(value) => toggleValueFilter("series", value)}
                    onClear={() => {
                      setValueFilters((prev) => ({ ...prev, series: [] }));
                      setValueFilterSearch((prev) => ({ ...prev, series: "" }));
                      setFilters((prev) => ({ ...prev, series: "" }));
                    }}
                    searchValue={valueFilterSearch.series}
                    onSearchChange={(value) => setValueFilterSearch((prev) => ({ ...prev, series: value }))}
                  />
                </div>
              </TableHead>
              <TableHead>
                <input
                  value={filters.bookNumber}
                  onChange={(event) => setFilters((prev) => ({ ...prev, bookNumber: event.target.value }))}
                  placeholder="Filter"
                  className="h-8 w-full rounded border bg-background px-2 text-xs"
                />
              </TableHead>
              <TableHead>
                <Button type="button" variant="ghost" size="sm" onClick={clearFilters}>
                  Clear
                </Button>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {Array.isArray(sortedBooks) &&
              sortedBooks.map((b) => {
                const status = getBookStatus(b);
                return (
                  <TableRow key={b.id}>
                    <TableCell>{b.id}</TableCell>
                    <TableCell>{b.title}</TableCell>
                    <TableCell>{b.author || "—"}</TableCell>
                    <TableCell>
                      <span className={getStatusChipClass(status)}>{status}</span>
                    </TableCell>
                    <TableCell>{formatDate(getDisplayDate(b))}</TableCell>
                    <TableCell>{b.series_name || "—"}</TableCell>
                    <TableCell>{b.book_number ?? "—"}</TableCell>
                    <TableCell className="flex flex-wrap gap-2">
                      {b.series_id ? (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => router.push(`/series/${b.series_id}`)}
                      >
                        Open series
                      </Button>
                    ) : null}
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className={
                        b.is_read
                          ? "border-rose-300 text-rose-700 hover:bg-rose-50"
                          : "border-emerald-300 text-emerald-700 hover:bg-emerald-50"
                      }
                      onClick={() => toggleRead(b)}
                    >
                      {b.is_read ? "Book: mark unread" : "Book: mark read"}
                    </Button>
                    <Button type="button" variant="destructive" size="sm" onClick={() => deleteBook(b.id)}>
                      Delete
                    </Button>
                    </TableCell>
                  </TableRow>
                );
              })}
          </TableBody>
        </Table>
      </div>
      <p className="text-xs text-muted-foreground">
        Showing {sortedBooks.length} of {books.length} books.
      </p>
      {loading && <p className="text-sm text-muted-foreground">Loading books…</p>}
    </div>
  );
}
