"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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

type BookRow = {
  id: number;
  title?: string | null;
  author?: string | null;
  read_status?: string | null;
  is_read?: boolean | null;
  is_missing?: boolean | null;
  is_upcoming_auto?: boolean | null;
  is_upcoming_final?: boolean | null;
  release_date?: string | null;
  publication_date?: string | null;
  read_date?: string | null;
  series_name?: string | null;
  series_id?: number | null;
  book_number?: number | null;
  [key: string]: unknown;
};

type BookStatus = "unread" | "available" | "upcoming" | "read";

function getBookStatus(book: BookRow): BookStatus {
  if (book.is_read) {
    return "read";
  }

  if (book.is_missing || book.is_upcoming_auto || book.is_upcoming_final) {
    return "upcoming";
  }

  if (book.read_status) {
    const explicitStatus = String(book.read_status).trim().toLowerCase();
    if (explicitStatus === "upcoming") return "upcoming";
    if (explicitStatus === "available") return "available";
    if (explicitStatus === "read") return "read";
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
      return "available";
    }
  }

  if (book.series_id && book.book_number !== null && book.book_number !== undefined) {
    return "available";
  }

  return "unread";
}

function getDisplayDate(book: BookRow) {
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
    return "inline-flex rounded-full border border-emerald-300 bg-emerald-100 px-1.5 py-0 text-[10px] font-semibold uppercase tracking-wide text-emerald-800";
  }
  if (status === "available") {
    return "inline-flex rounded-full border border-sky-300 bg-sky-100 px-1.5 py-0 text-[10px] font-semibold uppercase tracking-wide text-sky-800";
  }
  if (status === "unread") {
    return "inline-flex rounded-full border border-slate-300 bg-slate-100 px-1.5 py-0 text-[10px] font-semibold uppercase tracking-wide text-slate-800";
  }
  return "inline-flex rounded-full border border-rose-300 bg-rose-100 px-1.5 py-0 text-[10px] font-semibold uppercase tracking-wide text-rose-800";
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

function toIsoDateString(value?: string | null): string | null {
  const parsed = parseFlexibleDate(value);
  if (!parsed) return null;
  const year = parsed.getFullYear();
  const month = String(parsed.getMonth() + 1).padStart(2, "0");
  const day = String(parsed.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function toDateValue(value?: string | null): number {
  return parseFlexibleDate(value)?.valueOf() ?? Number.NEGATIVE_INFINITY;
}

type ValueFilterMenuProps = {
  label: string;
  options: string[];
  selectedValues: string[];
  onApplyValues: (values: string[]) => void;
  onClear: () => void;
  searchValue: string;
  onSearchChange: (value: string) => void;
};

function ValueFilterMenu({
  label,
  options,
  selectedValues,
  onApplyValues,
  onClear,
  searchValue,
  onSearchChange,
}: ValueFilterMenuProps) {
  const [open, setOpen] = useState(false);
  const [draftValues, setDraftValues] = useState<string[]>(selectedValues);
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
        onClick={() => {
          setOpen((prev) => {
            const nextOpen = !prev;
            if (nextOpen) {
              setDraftValues(selectedValues);
            }
            return nextOpen;
          });
        }}
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
            const checked = draftValues.includes(option);
            return (
              <label key={option} className="flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => {
                    setDraftValues((prev) =>
                      prev.includes(option)
                        ? prev.filter((item) => item !== option)
                        : [...prev, option]
                    );
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
        <div className="mt-2 flex justify-end gap-1">
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={() => {
              onApplyValues(draftValues);
              setOpen(false);
            }}
          >
            Apply
          </Button>
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
  status: BookStatus;
  releaseDate: string;
  publicationDate: string;
  readDate: string;
  autoSummary: string;
};

type EditBookFormState = {
  id: number | null;
  title: string;
  author: string;
  seriesName: string;
  bookNumber: string;
  status: BookStatus;
  date: string;
};

type AgentRunFormState = {
  title: string;
  author: string;
};

type AgentRunResponse = {
  found: boolean;
  metadata: Record<string, unknown>;
};

type LookupResultState = {
  found: boolean;
  summary: string | null;
  source_url: string | null;
  matched_title: string | null;
  matched_author: string | null;
};

const STATIC_API_BASE_CANDIDATES = [
  process.env.NEXT_PUBLIC_API_BASE_URL,
  "http://localhost:8000",
  "http://127.0.0.1:8000",
].filter(Boolean) as string[];

function normalizeBaseUrl(value: string) {
  return value.replace(/\/+$/, "");
}

function getApiBaseCandidates() {
  const dynamicCandidates: string[] = [];
  if (typeof window !== "undefined") {
    dynamicCandidates.push(`${window.location.protocol}//${window.location.hostname}:8000`);
  }

  return Array.from(new Set([...STATIC_API_BASE_CANDIDATES, ...dynamicCandidates]));
}

async function fetchApiWithFallback(path: string, init?: RequestInit) {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const baseCandidates = getApiBaseCandidates();
  const candidates = [
    `/api${normalizedPath}`,
    ...baseCandidates.map((base) => `${normalizeBaseUrl(base)}${normalizedPath}`),
  ];

  // If route includes a trailing slash, also try without it to avoid router mismatches.
  if (normalizedPath.endsWith("/")) {
    const trimmedPath = normalizedPath.slice(0, -1);
    candidates.push(`/api${trimmedPath}`);
    candidates.push(...baseCandidates.map((base) => `${normalizeBaseUrl(base)}${trimmedPath}`));
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

const EMPTY_EDIT_BOOK_FORM: EditBookFormState = {
  id: null,
  title: "",
  author: "",
  seriesName: "",
  bookNumber: "",
  status: "unread",
  date: "",
};

const EMPTY_AGENT_RUN_FORM: AgentRunFormState = {
  title: "",
  author: "",
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
  const [books, setBooks] = useState<BookRow[]>([]);
  const [seriesList, setSeriesList] = useState<SeriesOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [addDialogOpen, setAddDialogOpen] = useState(false);
  const [savingBook, setSavingBook] = useState(false);
  const [lookingUpBook, setLookingUpBook] = useState(false);
  const [showLookupSummary, setShowLookupSummary] = useState(false);
  const [addBookForm, setAddBookForm] = useState<AddBookFormState>(EMPTY_ADD_BOOK_FORM);
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [savingEditBook, setSavingEditBook] = useState(false);
  const [editBookForm, setEditBookForm] = useState<EditBookFormState>(EMPTY_EDIT_BOOK_FORM);
  const [agentDialogOpen, setAgentDialogOpen] = useState(false);
  const [agentRunning, setAgentRunning] = useState(false);
  const [agentApproving, setAgentApproving] = useState(false);
  const [agentRunForm, setAgentRunForm] = useState<AgentRunFormState>(EMPTY_AGENT_RUN_FORM);
  const [agentFound, setAgentFound] = useState<boolean | null>(null);
  const [agentMetadata, setAgentMetadata] = useState<Record<string, unknown> | null>(null);
  const [pinnedBookId, setPinnedBookId] = useState<number | null>(null);
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
      if (activeValueFilters.title.length > 0 && !activeValueFilters.title.includes(String(book.title || "").trim())) return false;
      if (activeValueFilters.author.length > 0 && !activeValueFilters.author.includes(String(book.author || "").trim())) return false;
      if (activeValueFilters.series.length > 0 && !activeValueFilters.series.includes(String(book.series_name || "").trim())) return false;
      if (activeValueFilters.status.length > 0 && !activeValueFilters.status.includes(String(getBookStatus(book)).trim())) return false;

      return true;
    });
  }, [books, filters, activeValueFilters]);

  const sortedBooks = useMemo(() => {
    const withPriorityOrder = [...filteredBooks].sort((a, b) => {
      const statusA = normalizeText(getBookStatus(a));
      const statusB = normalizeText(getBookStatus(b));

      const priority = (status: string) => {
        if (status === "upcoming") return 0;
        if (status === "read") return 1;
        return 2;
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
            if (status === "upcoming") return 0;
            if (status === "read") return 1;
            return 2;
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

  function setExplicitSort(key: BookSortKey, mode: "none" | "asc" | "desc") {
    if (mode === "none") {
      setSortConfig({ key: null, direction: "asc" });
      return;
    }
    setSortConfig({ key, direction: mode });
  }

  function setValueFilter(kind: "title" | "author" | "series" | "status", values: string[]) {
    setValueFilters((prev) => ({
      ...prev,
      [kind]: values,
    }));
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

  function updateAddBookForm<K extends keyof AddBookFormState>(key: K, value: AddBookFormState[K]) {
    setAddBookForm((prev) => ({ ...prev, [key]: value }));
  }

  function resetAddBookForm() {
    setAddBookForm(EMPTY_ADD_BOOK_FORM);
    setLookupResult(null);
    setShowLookupSummary(false);
  }

  function resetAgentWorkflow() {
    setAgentRunForm(EMPTY_AGENT_RUN_FORM);
    setAgentFound(null);
    setAgentMetadata(null);
    setAgentRunning(false);
    setAgentApproving(false);
  }

  async function handleRunAgent() {
    const title = agentRunForm.title.trim();
    const author = agentRunForm.author.trim();

    if (!title) {
      toast({
        title: "Need a title",
        description: "Enter a title before running the agent.",
      });
      return;
    }

    setAgentRunning(true);
    setAgentFound(null);
    setAgentMetadata(null);

    try {
      const response = await fetchApiWithFallback("/agent/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title,
          author: author || undefined,
        }),
      });

      if (!response.ok) {
        throw new Error(`Agent run failed (${response.status})`);
      }

      const data = await response.json();
      if (!data || typeof data !== "object" || Array.isArray(data)) {
        throw new Error("Agent returned invalid metadata format.");
      }

      const typedResponse = data as Partial<AgentRunResponse>;
      const responseMetadata =
        typedResponse.metadata && typeof typedResponse.metadata === "object" && !Array.isArray(typedResponse.metadata)
          ? typedResponse.metadata
          : (data as Record<string, unknown>);
      const responseFound =
        typeof typedResponse.found === "boolean"
          ? typedResponse.found
          : Boolean((responseMetadata as Record<string, unknown>).found);

      setAgentFound(responseFound);
      setAgentMetadata(responseMetadata);
      toast({
        title: "Metadata ready",
        description: "Review metadata and approve to create the book.",
      });
    } catch (error) {
      console.error("Error running book agent:", error);
      toast({
        title: "Agent error",
        description: error instanceof Error ? error.message : "Failed to run the book agent.",
      });
    } finally {
      setAgentRunning(false);
    }
  }

  async function handleApproveAgentMetadata() {
    if (!agentMetadata) {
      toast({
        title: "No metadata",
        description: "Run the agent and review metadata before approving.",
      });
      return;
    }

    setAgentApproving(true);

    try {
      const response = await fetchApiWithFallback("/agent/approve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ metadata: agentMetadata, found: agentFound }),
      });

      if (!response.ok) {
        throw new Error(`Approval failed (${response.status})`);
      }

      const createdBook = await response.json();
      await Promise.all([fetchBooks(), fetchSeriesList()]);
      setPinnedBookId(Number(createdBook?.id ?? null));

      toast({
        title: "Book created",
        description: `${createdBook?.title || "Book"} was created from approved metadata.`,
      });

      setAgentDialogOpen(false);
      resetAgentWorkflow();
    } catch (error) {
      console.error("Error approving metadata:", error);
      toast({
        title: "Approval error",
        description: error instanceof Error ? error.message : "Failed to create book from metadata.",
      });
    } finally {
      setAgentApproving(false);
    }
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

      const response = await fetchApiWithFallback(`/books/lookup?${params.toString()}`);
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

  const fetchSeriesList = useCallback(async () => {
    try {
      const response = await fetchApiWithFallback("/series/", { cache: "no-store" });
      const data = await response.json();
      setSeriesList(Array.isArray(data) ? data : []);
    } catch (error) {
      console.error("Error fetching series:", error);
    }
  }, []);

  useEffect(() => {
    if (seriesId) return;
    fetchBooks();
    fetchSeriesList();
  }, [seriesId, fetchBooks, fetchSeriesList]);

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
    const releaseDate = book.release_date || book.publication_date;
    const shouldStayUpcoming = Boolean(book.is_missing || book.is_upcoming_auto || book.is_upcoming_final);
    let nextStatus = nextIsRead ? "read" : "unread";
    if (!nextIsRead && releaseDate) {
      const parsedDate = new Date(releaseDate);
      if (!Number.isNaN(parsedDate.valueOf())) {
        const today = new Date();
        today.setHours(0, 0, 0, 0);
        parsedDate.setHours(0, 0, 0, 0);
        if (parsedDate > today) {
          nextStatus = "upcoming";
        } else {
          nextStatus = "available";
        }
      }
    }
    if (!nextIsRead && shouldStayUpcoming) {
      nextStatus = "upcoming";
    }
    if (!nextIsRead && !shouldStayUpcoming && book.series_id && book.book_number !== null && book.book_number !== undefined) {
      nextStatus = "available";
    }

    try {
      const response = await fetchApiWithFallback(`/books/${book.id}`, {
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
          const createSeriesResponse = await fetchApiWithFallback("/series/", {
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

      const createBookResponse = await fetchApiWithFallback("/books/", {
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
      setPinnedBookId(Number(createdBook?.id ?? null));
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

  function openEditBookDialog(book: BookRow) {
    setEditBookForm({
      id: Number(book.id),
      title: String(book.title || ""),
      author: String(book.author || ""),
      seriesName: String(book.series_name || ""),
      bookNumber: book.book_number !== null && book.book_number !== undefined ? String(book.book_number) : "",
      status: getBookStatus(book),
      date: toIsoDateString(getDisplayDate(book)) || "",
    });
    setEditDialogOpen(true);
  }

  async function handleSaveBookEdit() {
    const bookId = Number(editBookForm.id);
    if (!Number.isFinite(bookId) || bookId <= 0) return;

    const title = editBookForm.title.trim();
    const author = editBookForm.author.trim();
    if (!title || !author) {
      toast({ title: "Missing info", description: "Title and author are required." });
      return;
    }

    const numberRaw = editBookForm.bookNumber.trim();
    const parsedBookNumber = numberRaw ? Number(numberRaw) : null;
    if (numberRaw && !Number.isFinite(parsedBookNumber)) {
      toast({ title: "Invalid book number", description: "Book number must be numeric when provided." });
      return;
    }

    setSavingEditBook(true);
    try {
      let resolvedSeriesId: number | null = null;
      const seriesName = editBookForm.seriesName.trim();

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
          const createSeriesResponse = await fetchApiWithFallback("/series/", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: seriesName, author }),
          });
          if (!createSeriesResponse.ok) {
            throw new Error(`Failed to create series (${createSeriesResponse.status})`);
          }
          const createdSeries = await createSeriesResponse.json();
          resolvedSeriesId = Number(createdSeries.id);
        }
      }

      const status = editBookForm.status;
      const rawDate = editBookForm.date.trim();
      const normalizedDate = rawDate ? toIsoDateString(rawDate) : null;
      if (rawDate && !normalizedDate) {
        toast({
          title: "Invalid date",
          description: "Use a valid date format, such as YYYY-MM-DD.",
        });
        return;
      }
      const payload: Record<string, unknown> = {
        title,
        author,
        series_id: resolvedSeriesId,
        series_order: parsedBookNumber,
        book_number: parsedBookNumber,
        read_status: status,
        is_read: status === "read",
      };

      if (status === "read") {
        payload.read_date = normalizedDate || new Date().toISOString().split("T")[0];
        payload.release_date = null;
      } else {
        payload.read_date = null;
        payload.release_date = normalizedDate || null;
      }

      const response = await fetchApiWithFallback(`/books/${bookId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        throw new Error(`Failed to update book (${response.status})`);
      }

      const updatedBook = await response.json();
      setBooks((prev) => prev.map((item) => (item.id === updatedBook.id ? { ...item, ...updatedBook } : item)));
      publishBookStatusUpdate(updatedBook);
      setEditDialogOpen(false);
      setEditBookForm(EMPTY_EDIT_BOOK_FORM);
      toast({ title: "Book updated", description: `Saved changes for ${updatedBook.title}.` });
      await fetchSeriesList();
    } catch (error) {
      console.error("Error updating book:", error);
      toast({ title: "Error", description: error instanceof Error ? error.message : "Failed to update book." });
    } finally {
      setSavingEditBook(false);
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
          <p className="max-w-2xl text-xs leading-5 text-muted-foreground">
            Browse the collection with read status, release dates, and series links.
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
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

        <div className="flex justify-start md:justify-self-center">
          <table className="border border-border bg-card/70 text-xs">
            <tbody>
              <tr>
                <td className="min-w-[150px] border border-border px-2 py-1">Unread: <span className="font-semibold">{unreadBooks}</span></td>
                <td className="min-w-[150px] border border-border px-2 py-1">Read: <span className="font-semibold">{readBooks}</span></td>
                <td className="min-w-[150px] border border-border px-2 py-1">Available: <span className="font-semibold">{availableBooks}</span></td>
              </tr>
              <tr>
                <td className="border border-border px-2 py-1">Total: <span className="font-semibold">{totalBooks}</span></td>
                <td className="border border-border px-2 py-1">Upcoming: <span className="font-semibold">{upcomingBooks}</span></td>
                <td className="border border-border px-2 py-1">&nbsp;</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div className="flex flex-wrap gap-2 md:justify-self-end">
          <Button type="button" onClick={() => setAddDialogOpen(true)}>Add Book</Button>
          <Button type="button" variant="secondary" onClick={() => setAgentDialogOpen(true)}>
            Agent Draft
          </Button>
          <Link href="/books">
            <Button type="button" variant="outline">All Books</Button>
          </Link>
          <Link href="/series">
            <Button type="button" variant="secondary">Series</Button>
          </Link>
        </div>
      </div>

      <div ref={tableWrapRef} className="rounded-lg border bg-card/80">
        <Table className="w-full table-fixed text-[11px] [&_th]:h-7 [&_th]:py-0.5 [&_td]:py-0.5">
          <TableHeader>
            <TableRow>
              <TableHead style={{ width: `${columnWidths.title}%` }}>
                <ValueFilterMenu
                  label="Filter"
                  options={titleOptions}
                  selectedValues={activeValueFilters.title}
                  onApplyValues={(values) => setValueFilter("title", values)}
                  onClear={() => {
                    setValueFilters((prev) => ({ ...prev, title: [] }));
                    setValueFilterSearch((prev) => ({ ...prev, title: "" }));
                    setFilters((prev) => ({ ...prev, title: "" }));
                  }}
                  searchValue={valueFilterSearch.title}
                  onSearchChange={(value) => setValueFilterSearch((prev) => ({ ...prev, title: value }))}
                />
              </TableHead>
              <TableHead style={{ width: `${columnWidths.author}%` }}>
                <ValueFilterMenu
                  label="Filter"
                  options={authorOptions}
                  selectedValues={activeValueFilters.author}
                  onApplyValues={(values) => setValueFilter("author", values)}
                  onClear={() => {
                    setValueFilters((prev) => ({ ...prev, author: [] }));
                    setValueFilterSearch((prev) => ({ ...prev, author: "" }));
                    setFilters((prev) => ({ ...prev, author: "" }));
                  }}
                  searchValue={valueFilterSearch.author}
                  onSearchChange={(value) => setValueFilterSearch((prev) => ({ ...prev, author: value }))}
                />
              </TableHead>
              <TableHead style={{ width: `${columnWidths.status}%` }}>
                <div className="space-y-1">
                  <ValueFilterMenu
                    label="Filter"
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
              </TableHead>
              <TableHead style={{ width: `${columnWidths.date}%` }} className="align-top">
                <div className="space-y-1">
                  <select
                    value={sortConfig.key === "date" ? sortConfig.direction : "none"}
                    onChange={(event) =>
                      setExplicitSort("date", event.target.value as "none" | "asc" | "desc")
                    }
                    className="block h-6 w-full rounded border bg-background px-1.5 text-[11px]"
                  >
                    <option value="none">Sort</option>
                    <option value="asc">A to Z (oldest)</option>
                    <option value="desc">Z to A (newest)</option>
                  </select>
                </div>
              </TableHead>
              <TableHead style={{ width: `${columnWidths.series}%` }}>
                <ValueFilterMenu
                  label="Filter"
                  options={seriesOptions}
                  selectedValues={activeValueFilters.series}
                  onApplyValues={(values) => setValueFilter("series", values)}
                  onClear={() => {
                    setValueFilters((prev) => ({ ...prev, series: [] }));
                    setValueFilterSearch((prev) => ({ ...prev, series: "" }));
                    setFilters((prev) => ({ ...prev, series: "" }));
                  }}
                  searchValue={valueFilterSearch.series}
                  onSearchChange={(value) => setValueFilterSearch((prev) => ({ ...prev, series: value }))}
                />
              </TableHead>
              <TableHead style={{ width: `${columnWidths.bookNumber}%` }}>
                <select
                  value={sortConfig.key === "bookNumber" ? sortConfig.direction : "none"}
                  onChange={(event) =>
                    setExplicitSort("bookNumber", event.target.value as "none" | "asc" | "desc")
                  }
                  className="h-6 w-full rounded border bg-background px-1.5 text-[11px]"
                >
                  <option value="none">Sort</option>
                  <option value="asc">A to Z</option>
                  <option value="desc">Z to A</option>
                </select>
              </TableHead>
              <TableHead style={{ width: `${columnWidths.actions}%` }}>
                <Button type="button" variant="ghost" size="sm" onClick={clearFilters}>
                  Clear
                </Button>
              </TableHead>
            </TableRow>
            <TableRow>
              <TableHead className="relative" style={{ width: `${columnWidths.title}%` }}>
                <button type="button" className="text-left" onClick={() => toggleSort("title")}>Title{sortLabel("title")}</button>
                <button
                  type="button"
                  aria-label="Resize Title column"
                  onMouseDown={(event) => startColumnResize("title", event)}
                  className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
                />
              </TableHead>
              <TableHead className="relative" style={{ width: `${columnWidths.author}%` }}>
                <button type="button" className="text-left" onClick={() => toggleSort("author")}>Author{sortLabel("author")}</button>
                <button
                  type="button"
                  aria-label="Resize Author column"
                  onMouseDown={(event) => startColumnResize("author", event)}
                  className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
                />
              </TableHead>
              <TableHead className="relative" style={{ width: `${columnWidths.status}%` }}>
                <button type="button" className="text-left" onClick={() => toggleSort("status")}>Status{sortLabel("status")}</button>
                <button
                  type="button"
                  aria-label="Resize Status column"
                  onMouseDown={(event) => startColumnResize("status", event)}
                  className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
                />
              </TableHead>
              <TableHead className="relative" style={{ width: `${columnWidths.date}%` }}>
                <button type="button" className="text-left" onClick={() => toggleSort("date")}>Date{sortLabel("date")}</button>
                <button
                  type="button"
                  aria-label="Resize Date column"
                  onMouseDown={(event) => startColumnResize("date", event)}
                  className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
                />
              </TableHead>
              <TableHead className="relative" style={{ width: `${columnWidths.series}%` }}>
                <button type="button" className="text-left" onClick={() => toggleSort("series")}>Series{sortLabel("series")}</button>
                <button
                  type="button"
                  aria-label="Resize Series column"
                  onMouseDown={(event) => startColumnResize("series", event)}
                  className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
                />
              </TableHead>
              <TableHead className="relative" style={{ width: `${columnWidths.bookNumber}%` }}>
                <button type="button" className="text-left" onClick={() => toggleSort("bookNumber")}>Book #{sortLabel("bookNumber")}</button>
                <button
                  type="button"
                  aria-label="Resize Book number column"
                  onMouseDown={(event) => startColumnResize("bookNumber", event)}
                  className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
                />
              </TableHead>
              <TableHead className="relative" style={{ width: `${columnWidths.actions}%` }}>
                Actions
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
                return (
                  <TableRow key={b.id}>
                    <TableCell className="truncate" title={b.title ?? undefined}>{b.title || "—"}</TableCell>
                    <TableCell className="truncate" title={b.author || "—"}>{b.author || "—"}</TableCell>
                    <TableCell>
                      <span className={getStatusChipClass(status)}>{status}</span>
                    </TableCell>
                    <TableCell>{formatDate(getDisplayDate(b))}</TableCell>
                    <TableCell className="truncate" title={b.series_name || "—"}>{b.series_name || "—"}</TableCell>
                    <TableCell>{b.book_number ?? "—"}</TableCell>
                    <TableCell className="whitespace-nowrap">
                      <div className="flex items-center gap-0.5">
                      {b.series_id ? (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-6 px-1.5 text-[10px]"
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
                          ? "h-6 border-rose-300 px-1.5 text-[10px] text-rose-700 hover:bg-rose-50"
                          : "h-6 border-emerald-300 px-1.5 text-[10px] text-emerald-700 hover:bg-emerald-50"
                      }
                      onClick={() => toggleRead(b)}
                    >
                      {b.is_read ? "Mark unread" : "Mark read"}
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="h-6 px-1.5 text-[10px]"
                      onClick={() => openEditBookDialog(b)}
                    >
                      Edit
                    </Button>
                    <Button
                      type="button"
                      variant="destructive"
                      size="sm"
                      className="h-6 px-1.5 text-[10px]"
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
                <option value="available">Available</option>
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

      <Dialog
        open={editDialogOpen}
        onOpenChange={(open) => {
          setEditDialogOpen(open);
          if (!open) {
            setEditBookForm(EMPTY_EDIT_BOOK_FORM);
          }
        }}
      >
        <DialogContent className="sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>Edit Book</DialogTitle>
            <DialogDescription>
              Update core book metadata from the library without leaving this page.
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1 sm:col-span-2">
              <Label htmlFor="edit-book-title">Title</Label>
              <Input
                id="edit-book-title"
                value={editBookForm.title}
                onChange={(event) => setEditBookForm((prev) => ({ ...prev, title: event.target.value }))}
              />
            </div>

            <div className="space-y-1 sm:col-span-2">
              <Label htmlFor="edit-book-author">Author</Label>
              <Input
                id="edit-book-author"
                value={editBookForm.author}
                onChange={(event) => setEditBookForm((prev) => ({ ...prev, author: event.target.value }))}
              />
            </div>

            <div className="space-y-1 sm:col-span-2">
              <Label htmlFor="edit-book-series">Series (optional)</Label>
              <Input
                id="edit-book-series"
                value={editBookForm.seriesName}
                onChange={(event) => setEditBookForm((prev) => ({ ...prev, seriesName: event.target.value }))}
                placeholder="Series name"
              />
            </div>

            <div className="space-y-1">
              <Label htmlFor="edit-book-number">Book #</Label>
              <Input
                id="edit-book-number"
                value={editBookForm.bookNumber}
                onChange={(event) => setEditBookForm((prev) => ({ ...prev, bookNumber: event.target.value }))}
                placeholder="e.g. 24"
              />
            </div>

            <div className="space-y-1">
              <Label htmlFor="edit-book-status">Status</Label>
              <select
                id="edit-book-status"
                value={editBookForm.status}
                onChange={(event) =>
                  setEditBookForm((prev) => ({
                    ...prev,
                    status: event.target.value as BookStatus,
                  }))
                }
                className="h-9 w-full rounded border bg-background px-2 text-sm"
              >
                <option value="unread">unread</option>
                <option value="available">available</option>
                <option value="upcoming">upcoming</option>
                <option value="read">read</option>
              </select>
            </div>

            <div className="space-y-1 sm:col-span-2">
              <Label htmlFor="edit-book-date">Date</Label>
              <Input
                id="edit-book-date"
                value={editBookForm.date}
                onChange={(event) => setEditBookForm((prev) => ({ ...prev, date: event.target.value }))}
                placeholder={editBookForm.status === "read" ? "Read date" : "Release date"}
              />
            </div>
          </div>

          <DialogFooter showCloseButton>
            <Button type="button" onClick={handleSaveBookEdit} disabled={savingEditBook}>
              {savingEditBook ? "Saving..." : "Save changes"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={agentDialogOpen}
        onOpenChange={(open) => {
          setAgentDialogOpen(open);
          if (!open) {
            resetAgentWorkflow();
          }
        }}
      >
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Agent-Assisted Book Draft</DialogTitle>
            <DialogDescription>
              Run the agent to generate metadata, review the result, then approve to create the book.
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1 sm:col-span-2">
              <Label htmlFor="agent-book-title">Title</Label>
              <Input
                id="agent-book-title"
                value={agentRunForm.title}
                onChange={(event) => setAgentRunForm((prev) => ({ ...prev, title: event.target.value }))}
                placeholder="Book title"
              />
            </div>

            <div className="space-y-1 sm:col-span-2">
              <Label htmlFor="agent-book-author">Author (optional)</Label>
              <Input
                id="agent-book-author"
                value={agentRunForm.author}
                onChange={(event) => setAgentRunForm((prev) => ({ ...prev, author: event.target.value }))}
                placeholder="Author name"
              />
            </div>

            <div className="sm:col-span-2">
              <Button type="button" variant="outline" onClick={handleRunAgent} disabled={agentRunning}>
                {agentRunning ? "Running agent..." : "Run /agent/run"}
              </Button>
            </div>

            <div className="space-y-1 sm:col-span-2">
              <Label htmlFor="agent-metadata-preview">Metadata preview</Label>
              <textarea
                id="agent-metadata-preview"
                readOnly
                value={agentMetadata ? JSON.stringify(agentMetadata, null, 2) : "Run the agent to generate metadata."}
                className="min-h-56 w-full rounded-lg border border-input bg-background px-2.5 py-2 font-mono text-xs outline-none"
              />
            </div>
          </div>

          {agentFound === false ? (
            <div className="rounded-md border border-red-500 bg-red-200 px-3 py-2 text-sm font-semibold text-red-900">
              ⚠️ No known book found for this title. You can still approve manually.
            </div>
          ) : null}

          <DialogFooter showCloseButton>
            <Button
              type="button"
              onClick={handleApproveAgentMetadata}
              disabled={!agentMetadata || agentApproving || agentRunning}
            >
              {agentApproving ? "Approving..." : "Approve and Create Book"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
