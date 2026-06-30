"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { CircleHelpIcon } from "lucide-react";
import { useToast } from "@/components/ui/use-toast";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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
        className="h-7 w-full rounded border bg-background px-2 text-left text-xs"
      >
        {label} {selectedValues.length > 0 ? `(${selectedValues.length})` : ""}
      </button>
      {open ? (
        <div className="absolute z-20 mt-1 w-60 rounded-md border bg-background p-2 shadow-lg">
        <input
          value={searchValue}
          onChange={(event) => onSearchChange(event.target.value)}
          placeholder="Search values"
          className="mb-2 h-7 w-full rounded border bg-background px-2 text-xs"
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

type SeriesOption = {
  id: number;
  name: string;
  author?: string | null;
};

type AddBookFormState = {
  title: string;
  author: string;
  seriesName: string;
  bookNumber: string;
  status: "unread" | "upcoming" | "read";
  releaseDate: string;
  publicationDate: string;
  readDate: string;
  autoSummary: string;
};

type LookupResultState = {
  found: boolean;
  summary: string | null;
  source_url: string | null;
  matched_title: string | null;
  matched_author: string | null;
};

const API_BASE_CANDIDATES = [
  process.env.NEXT_PUBLIC_API_BASE_URL,
  "http://localhost:8000",
  "http://127.0.0.1:8000",
].filter(Boolean) as string[];

function normalizeBaseUrl(value: string) {
  return value.replace(/\/+$/, "");
}

async function fetchApiWithFallback(path: string, init?: RequestInit) {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const candidates = API_BASE_CANDIDATES.map((base) => `${normalizeBaseUrl(base)}${normalizedPath}`);

  // If route includes a trailing slash, also try without it to avoid router mismatches.
  if (normalizedPath.endsWith("/")) {
    const trimmedPath = normalizedPath.slice(0, -1);
    candidates.push(...API_BASE_CANDIDATES.map((base) => `${normalizeBaseUrl(base)}${trimmedPath}`));
  }

  let lastError: Error | null = null;
  for (const url of candidates) {
    try {
      const response = await fetch(url, init);
      if (response.ok) {
        return response;
      }
      lastError = new Error(`Failed to load ${normalizedPath} (${response.status})`);
    } catch (error) {
      lastError = error instanceof Error ? error : new Error("Network error");
    }
  }

  throw lastError ?? new Error(`Failed to load ${normalizedPath}`);
}

const EMPTY_ADD_BOOK_FORM: AddBookFormState = {
  title: "",
  author: "",
  seriesName: "",
  bookNumber: "",
  status: "unread",
  releaseDate: "",
  publicationDate: "",
  readDate: "",
  autoSummary: "",
};

function normalizeLookupMatchedTitle(value: string | null | undefined) {
  const raw = String(value || "").trim();
  if (!raw) return "";

  return raw
    .replace(/\s+ebook\s*$/i, "")
    .replace(/\s+kindle\s+edition\s*$/i, "")
    .trim();
}

export default function BooksClient() {
  const { toast } = useToast();
  const [books, setBooks] = useState<any[]>([]);
  const [seriesList, setSeriesList] = useState<SeriesOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [addDialogOpen, setAddDialogOpen] = useState(false);
  const [savingBook, setSavingBook] = useState(false);
  const [lookingUpBook, setLookingUpBook] = useState(false);
  const [showLookupSummary, setShowLookupSummary] = useState(false);
  const [addBookForm, setAddBookForm] = useState<AddBookFormState>(EMPTY_ADD_BOOK_FORM);
  const [lookupResult, setLookupResult] = useState<LookupResultState | null>(null);
  const [filters, setFilters] = useState({
    id: "",
    title: "",
    author: "",
    status: "all",
    series: "",
    bookNumber: "",
  });
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
  const [sortConfig, setSortConfig] = useState<{ key: BookSortKey | null; direction: SortDirection }>({
    key: null,
    direction: "asc",
  });
  const searchParams = useSearchParams();
  const router = useRouter();
  const seriesId = searchParams.get("series_id");

  useEffect(() => {
    if (seriesId) {
      router.replace(`/series/${seriesId}`);
    }
  }, [router, seriesId]);

  const totalBooks = books.length;
  const readBooks = books.filter((book) => book.is_read).length;
  const unreadBooks = books.filter((book) => !book.is_read).length;
  const upcomingBooks = books.filter((book) => getBookStatus(book) === "upcoming").length;

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
      if (valueFilters.title.length > 0 && !valueFilters.title.includes(String(book.title || "").trim())) return false;
      if (valueFilters.author.length > 0 && !valueFilters.author.includes(String(book.author || "").trim())) return false;
      if (valueFilters.series.length > 0 && !valueFilters.series.includes(String(book.series_name || "").trim())) return false;
      if (valueFilters.status.length > 0 && !valueFilters.status.includes(String(getBookStatus(book)).trim())) return false;

      return true;
    });
  }, [books, filters, valueFilters]);

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

  function toggleValueFilter(kind: "title" | "author" | "series" | "status", value: string) {
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
    setValueFilters({ title: [], author: [], series: [], status: [] });
    setValueFilterSearch({ title: "", author: "", series: "", status: "" });
  }

  function updateAddBookForm<K extends keyof AddBookFormState>(key: K, value: AddBookFormState[K]) {
    setAddBookForm((prev) => ({ ...prev, [key]: value }));
  }

  function resetAddBookForm() {
    setAddBookForm(EMPTY_ADD_BOOK_FORM);
    setLookupResult(null);
    setShowLookupSummary(false);
  }

  async function handleFindDetails() {
    const title = addBookForm.title.trim();
    const author = addBookForm.author.trim();

    if (!title) {
      toast({
        title: "Need a title",
        description: "Enter at least the book title before using Find details.",
      });
      return;
    }

    setLookingUpBook(true);

    try {
      const params = new URLSearchParams();
      params.set("title", title);
      if (author) {
        params.set("author", author);
      }

      const response = await fetch(`http://localhost:8000/books/lookup?${params.toString()}`);
      if (!response.ok) {
        throw new Error(`Lookup failed (${response.status})`);
      }

      const data: LookupResultState = await response.json();
      setLookupResult(data);

      if (!data.found) {
        toast({
          title: "No details found",
          description: "No match was found. You can still add the book manually.",
        });
        return;
      }

      setAddBookForm((prev) => ({
        ...prev,
        title: normalizeLookupMatchedTitle(data.matched_title) || prev.title,
        author: data.matched_author?.trim() || prev.author,
        autoSummary: data.summary || prev.autoSummary,
      }));
      setShowLookupSummary(false);

      toast({
        title: "Details found",
        description: "Matched title and author were applied to the form.",
      });
    } catch (error) {
      console.error("Error looking up book:", error);
      toast({
        title: "Lookup error",
        description: error instanceof Error ? error.message : "Unable to look up book details.",
      });
    } finally {
      setLookingUpBook(false);
    }
  }

  async function fetchBooks() {
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
  }

  async function fetchSeriesList() {
    try {
      const response = await fetchApiWithFallback("/series/", { cache: "no-store" });
      const data = await response.json();
      setSeriesList(Array.isArray(data) ? data : []);
    } catch (error) {
      console.error("Error fetching series:", error);
    }
  }

  useEffect(() => {
    if (seriesId) return;
    fetchBooks();
    fetchSeriesList();
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

  async function handleAddBook() {
    const title = addBookForm.title.trim();
    const author = addBookForm.author.trim();
    const seriesName = addBookForm.seriesName.trim();
    const bookNumberText = addBookForm.bookNumber.trim();

    if (!title || !author) {
      toast({
        title: "Missing info",
        description: "Title and author are required.",
      });
      return;
    }

    const parsedBookNumber = bookNumberText ? Number(bookNumberText) : null;
    if (bookNumberText && !Number.isFinite(parsedBookNumber)) {
      toast({
        title: "Invalid book number",
        description: "Book number must be numeric when provided.",
      });
      return;
    }

    setSavingBook(true);

    try {
      let resolvedSeriesId: number | null = null;

      if (seriesName) {
        const normalizedSeriesName = normalizeText(seriesName);
        const normalizedAuthor = normalizeText(author);
        const matchedSeries = seriesList.find((series) => {
          if (normalizeText(series.name) !== normalizedSeriesName) return false;

          const existingAuthor = normalizeText(series.author);
          return !existingAuthor || !normalizedAuthor || existingAuthor === normalizedAuthor;
        });

        if (matchedSeries) {
          resolvedSeriesId = Number(matchedSeries.id);
        } else {
          const createSeriesResponse = await fetch("http://localhost:8000/series/", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              name: seriesName,
              author,
            }),
          });

          if (!createSeriesResponse.ok) {
            throw new Error(`Failed to create series (${createSeriesResponse.status})`);
          }

          const createdSeries = await createSeriesResponse.json();
          resolvedSeriesId = Number(createdSeries.id);
        }
      }

      const readStatus = addBookForm.status;
      const isRead = readStatus === "read";
      const readDate = readStatus === "read"
        ? (addBookForm.readDate || new Date().toISOString().split("T")[0])
        : null;
      const releaseDate = readStatus !== "read" ? addBookForm.releaseDate.trim() : "";

      const createBookResponse = await fetch("http://localhost:8000/books/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title,
          author,
          series_id: resolvedSeriesId,
          series_order: parsedBookNumber,
          book_number: parsedBookNumber,
          release_date: releaseDate || undefined,
          publication_date: addBookForm.publicationDate || undefined,
          read_date: readDate || undefined,
          read_status: readStatus,
          is_read: isRead,
          auto_summary: addBookForm.autoSummary || undefined,
        }),
      });

      if (!createBookResponse.ok) {
        throw new Error(`Failed to create book (${createBookResponse.status})`);
      }

      const createdBook = await createBookResponse.json();
      await Promise.all([fetchBooks(), fetchSeriesList()]);
      setAddDialogOpen(false);
      resetAddBookForm();
      toast({
        title: "Book added",
        description: resolvedSeriesId
          ? `Added ${createdBook.title} and attached it to a series.`
          : `Added ${createdBook.title} to your library.`,
      });
    } catch (error) {
      console.error("Error adding book:", error);
      toast({
        title: "Error",
        description: error instanceof Error ? error.message : "Failed to add book.",
      });
    } finally {
      setSavingBook(false);
    }
  }

  if (seriesId) {
    return <div className="p-4 text-sm text-muted-foreground">Redirecting to series detail...</div>;
  }

  return (
    <div className="p-4 space-y-3">
      <div className="grid gap-2 md:grid-cols-[1fr_auto_auto] md:items-start">
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground">
            Library
          </p>
          <h1 className="text-2xl font-bold">
            {seriesId ? `Series ${seriesId} books` : "All books"}
          </h1>
          <p className="max-w-2xl text-xs leading-5 text-muted-foreground md:hidden">
            Browse the collection with read status, release dates, and series links.
          </p>
        </div>

        <div className="flex justify-start md:justify-self-center">
          <table className="border border-border bg-card/70 text-xs">
            <tbody>
              <tr>
                <td className="min-w-[150px] border border-border px-2 py-1">Unread: <span className="font-semibold">{unreadBooks}</span></td>
                <td className="min-w-[150px] border border-border px-2 py-1">Read: <span className="font-semibold">{readBooks}</span></td>
              </tr>
              <tr>
                <td className="border border-border px-2 py-1">Total: <span className="font-semibold">{totalBooks}</span></td>
                <td className="border border-border px-2 py-1">Upcoming: <span className="font-semibold">{upcomingBooks}</span></td>
              </tr>
            </tbody>
          </table>
        </div>

        <div className="flex flex-wrap gap-2 md:justify-self-end">
          <Button type="button" onClick={() => setAddDialogOpen(true)}>Add Book</Button>
          <Link href="/books">
            <Button type="button" variant="outline">All Books</Button>
          </Link>
          <Link href="/series">
            <Button type="button" variant="secondary">Series</Button>
          </Link>
        </div>
      </div>

      <div className="overflow-x-auto rounded-lg border bg-card/80">
        <Table className="text-xs [&_th]:h-8 [&_th]:py-1 [&_td]:py-1">
          <TableHeader>
            <TableRow>
              <TableHead>
                <span className="text-muted-foreground">—</span>
              </TableHead>
              <TableHead>
                <ValueFilterMenu
                  label="Filter"
                  options={titleOptions}
                  selectedValues={valueFilters.title}
                  onToggleValue={(value) => toggleValueFilter("title", value)}
                  onClear={() => {
                    setValueFilters((prev) => ({ ...prev, title: [] }));
                    setValueFilterSearch((prev) => ({ ...prev, title: "" }));
                    setFilters((prev) => ({ ...prev, title: "" }));
                  }}
                  searchValue={valueFilterSearch.title}
                  onSearchChange={(value) => setValueFilterSearch((prev) => ({ ...prev, title: value }))}
                />
              </TableHead>
              <TableHead>
                <ValueFilterMenu
                  label="Filter"
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
              </TableHead>
              <TableHead>
                <div className="space-y-1">
                  <ValueFilterMenu
                    label="Filter"
                    options={statusOptions}
                    selectedValues={valueFilters.status}
                    onToggleValue={(value) => toggleValueFilter("status", value)}
                    onClear={() => {
                      setValueFilters((prev) => ({ ...prev, status: [] }));
                      setValueFilterSearch((prev) => ({ ...prev, status: "" }));
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
                    className="block h-7 w-full rounded border bg-background px-2 text-xs"
                  >
                    <option value="none">Sort date</option>
                    <option value="asc">A to Z (oldest)</option>
                    <option value="desc">Z to A (newest)</option>
                  </select>
                </div>
              </TableHead>
              <TableHead>
                <ValueFilterMenu
                  label="Filter"
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
              </TableHead>
              <TableHead>
                <select
                  value={sortConfig.key === "bookNumber" ? sortConfig.direction : "none"}
                  onChange={(event) =>
                    setExplicitSort("bookNumber", event.target.value as "none" | "asc" | "desc")
                  }
                  className="h-7 w-full rounded border bg-background px-2 text-xs"
                >
                  <option value="none">Sort</option>
                  <option value="asc">A to Z</option>
                  <option value="desc">Z to A</option>
                </select>
              </TableHead>
              <TableHead>
                <Button type="button" variant="ghost" size="sm" onClick={clearFilters}>
                  Clear
                </Button>
              </TableHead>
            </TableRow>
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
                    <TableCell className="whitespace-nowrap">
                      <div className="flex items-center gap-1">
                      {b.series_id ? (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-7 px-2 text-xs"
                        onClick={() => router.push(`/series/${b.series_id}`)}
                      >
                        View books
                      </Button>
                    ) : null}
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className={
                        b.is_read
                          ? "h-7 border-rose-300 px-2 text-xs text-rose-700 hover:bg-rose-50"
                          : "h-7 border-emerald-300 px-2 text-xs text-emerald-700 hover:bg-emerald-50"
                      }
                      onClick={() => toggleRead(b)}
                    >
                      {b.is_read ? "Mark unread" : "Mark read"}
                    </Button>
                    <Button
                      type="button"
                      variant="destructive"
                      size="sm"
                      className="h-7 px-2 text-xs"
                      onClick={() => deleteBook(b.id)}
                    >
                      Delete
                    </Button>
                      </div>
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

      <Dialog open={addDialogOpen} onOpenChange={setAddDialogOpen}>
        <DialogContent className="sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>Add Book</DialogTitle>
            <DialogDescription>
              Add a standalone book or start a new series by entering the first book you already own.
            </DialogDescription>
          </DialogHeader>

          <div className="rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
            <div className="flex items-start gap-2">
              <CircleHelpIcon className="mt-0.5 h-4 w-4 shrink-0" />
              <div>
                <p className="font-medium text-foreground">Find details helper</p>
                <p>Minimum for search: book title. Best results: book title plus author.</p>
              </div>
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1 sm:col-span-2">
              <Label htmlFor="add-book-title">Title</Label>
              <Input
                id="add-book-title"
                value={addBookForm.title}
                onChange={(event) => updateAddBookForm("title", event.target.value)}
                placeholder="Book title"
              />
            </div>

            <div className="space-y-1 sm:col-span-2">
              <Label htmlFor="add-book-author">Author</Label>
              <Input
                id="add-book-author"
                value={addBookForm.author}
                onChange={(event) => updateAddBookForm("author", event.target.value)}
                placeholder="Author name"
              />
            </div>

            <div className="sm:col-span-2 flex flex-wrap items-center gap-2">
              <Button type="button" variant="secondary" onClick={handleFindDetails} disabled={lookingUpBook}>
                {lookingUpBook ? "Finding..." : "Find details"}
              </Button>
              {lookupResult?.found ? (
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <span>
                    Matched {normalizeLookupMatchedTitle(lookupResult.matched_title) || "title"}
                    {lookupResult.matched_author ? ` by ${lookupResult.matched_author}` : ""}.
                  </span>
                  {lookupResult.summary ? (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="h-6 px-2 text-[11px]"
                      onClick={() => setShowLookupSummary((prev) => !prev)}
                    >
                      {showLookupSummary ? "Hide summary" : "Show summary"}
                    </Button>
                  ) : null}
                  {lookupResult.source_url ? (
                    <a
                      href={lookupResult.source_url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-[11px] text-blue-600 underline"
                    >
                      Source
                    </a>
                  ) : null}
                </div>
              ) : lookupResult ? (
                <span className="text-xs text-muted-foreground">No external match found. Manual add still works.</span>
              ) : null}
            </div>

            <div className="space-y-1">
              <Label htmlFor="add-book-series">Series name</Label>
              <Input
                id="add-book-series"
                list="series-options"
                value={addBookForm.seriesName}
                onChange={(event) => updateAddBookForm("seriesName", event.target.value)}
                placeholder="Optional series"
              />
              <datalist id="series-options">
                {seriesList.map((series) => (
                  <option key={series.id} value={series.name} />
                ))}
              </datalist>
            </div>

            <div className="space-y-1">
              <Label htmlFor="add-book-number">Book number</Label>
              <Input
                id="add-book-number"
                value={addBookForm.bookNumber}
                onChange={(event) => updateAddBookForm("bookNumber", event.target.value)}
                placeholder="Optional number"
              />
            </div>

            <div className="space-y-1">
              <Label htmlFor="add-book-status">Status</Label>
              <select
                id="add-book-status"
                value={addBookForm.status}
                onChange={(event) => {
                  const nextStatus = event.target.value as AddBookFormState["status"];
                  setAddBookForm((prev) => ({
                    ...prev,
                    status: nextStatus,
                    readDate: nextStatus === "read" ? prev.readDate : "",
                    releaseDate: nextStatus === "upcoming" ? prev.releaseDate : "",
                  }));
                }}
                className="h-9 w-full rounded-md border bg-background px-2 text-sm"
              >
                <option value="unread">Unread</option>
                <option value="upcoming">Upcoming</option>
                <option value="read">Read</option>
              </select>
            </div>

            <div className="space-y-1">
              <Label htmlFor="add-book-release-date">Date (planned/release)</Label>
              <Input
                id="add-book-release-date"
                type="date"
                value={addBookForm.releaseDate}
                onChange={(event) => updateAddBookForm("releaseDate", event.target.value)}
                disabled={addBookForm.status === "read"}
              />
            </div>

            <div className="space-y-1">
              <Label htmlFor="add-book-publication-date">Publication date</Label>
              <Input
                id="add-book-publication-date"
                type="date"
                value={addBookForm.publicationDate}
                onChange={(event) => updateAddBookForm("publicationDate", event.target.value)}
              />
            </div>

            <div className="space-y-1">
              <Label htmlFor="add-book-read-date">Read date</Label>
              <Input
                id="add-book-read-date"
                type="date"
                value={addBookForm.readDate}
                onChange={(event) => updateAddBookForm("readDate", event.target.value)}
                disabled={addBookForm.status !== "read"}
              />
            </div>

            {lookupResult?.summary && showLookupSummary ? (
              <div className="space-y-1 sm:col-span-2">
                <Label htmlFor="add-book-summary">Found summary</Label>
                <textarea
                  id="add-book-summary"
                  value={addBookForm.autoSummary}
                  onChange={(event) => updateAddBookForm("autoSummary", event.target.value)}
                  className="min-h-16 w-full rounded-lg border border-input bg-transparent px-2.5 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
                />
              </div>
            ) : null}
          </div>

          <DialogFooter showCloseButton>
            <Button type="button" onClick={handleAddBook} disabled={savingBook}>
              {savingBook ? "Saving..." : "Save book"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
