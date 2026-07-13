"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useToast } from "@/components/ui/use-toast";
import { fetchApiWithFallback } from "@/lib/api-client";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

type SeriesRow = {
  id: number;
  name: string;
  author?: string | null;
  is_finished?: boolean;
  series_status?: string | null;
  next_unread_book_number?: number | null;
  next_upcoming_book_number?: number | null;
  total_books?: number | null;
  books_tracked?: number;
  last_checked?: string | null;
  updated_at?: string | null;
  has_new_books?: boolean;
  has_unread_books?: boolean;
  has_upcoming_books?: boolean;
  is_caught_up?: boolean;
  missing_books?: string[];
  inferred_missing_numbers?: number[];
  series_state?: SeriesState | null;
};

type SeriesState = {
  has_new_books: boolean;
  has_unread_books: boolean;
  has_upcoming_books: boolean;
  is_caught_up: boolean;
};

type SeriesApiRow = {
  id: number;
  name: string;
  author?: string | null;
  is_finished?: boolean;
  series_status?: string | null;
  next_unread_book_number?: number | null;
  next_upcoming_book_number?: number | null;
  total_books?: number | null;
  updated_at?: string | null;
  has_new_books?: boolean;
  has_unread_books?: boolean;
  has_upcoming_books?: boolean;
  is_caught_up?: boolean;
  missing_books?: string[];
  series_state?: SeriesState | null;
};

type SeriesDetailBook = {
  title?: string | null;
  book_number?: number | null;
  series_order?: number | null;
};

const OMNIBUS_RANGE_PATTERN = /\bbooks?\s+(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\b/i;

type SeriesDetailApiRow = SeriesApiRow & {
  books?: SeriesDetailBook[];
};

type CheckBannerTone = "success" | "danger" | "error";

type CheckBannerState = {
  seriesId: number;
  seriesTitle: string;
  tone: CheckBannerTone;
  title: string;
  message: string;
  actionHref?: string;
  actionLabel?: string;
  detail?: string;
};

const CHECK_STATUS_POLL_INTERVAL_MS = 1000;
const CHECK_STATUS_MAX_POLLS = 600;
const CHECK_STATUS_STALLED_POLLS = 120;

type SeriesCheckStatusResponse = {
  series_id: number;
  session_id?: string | null;
  status: "idle" | "started" | "running" | "complete";
  updated_at?: string;
  error?: string;
  result?: Record<string, unknown>;
  complete?: boolean;
  no_new_books?: boolean;
  reason?: string;
  missing_books?: Array<number | string>;
  found_books?: Array<Record<string, unknown>>;
  progress?: number;
  current_pass?: string | null;
  progress_total?: number;
  progress_completed?: number;
  current_book_number?: number | null;
};

type CandidateDiagnostic = {
  book_number?: number;
  reason?: string | null;
  message?: string | null;
};

function getSeriesState(row: Pick<SeriesRow, "has_new_books" | "has_unread_books" | "has_upcoming_books" | "is_caught_up" | "series_state">): SeriesState {
  return {
    has_new_books: Boolean(row.series_state?.has_new_books ?? row.has_new_books ?? false),
    has_unread_books: Boolean(row.series_state?.has_unread_books ?? row.has_unread_books ?? false),
    has_upcoming_books: Boolean(row.series_state?.has_upcoming_books ?? row.has_upcoming_books ?? false),
    is_caught_up: Boolean(row.series_state?.is_caught_up ?? row.is_caught_up ?? false),
  };
}

function getSeriesPriority(row: SeriesRow): number {
  const state = getSeriesState(row);
  if (state.has_new_books) return 0;
  if (state.has_unread_books) return 1;
  return 2;
}

function summarizeCandidateDiagnostics(rawDiagnostics: unknown): string | null {
  if (!Array.isArray(rawDiagnostics) || rawDiagnostics.length === 0) {
    return null;
  }

  const diagnostics = rawDiagnostics as CandidateDiagnostic[];
  const firstMeaningful = diagnostics.find((item) => item?.message);
  if (firstMeaningful?.message) {
    return firstMeaningful.message;
  }

  return null;
}

function delay(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function formatDate(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleDateString();
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

function inferMissingNumbersFromBooks(books: SeriesDetailBook[] | undefined): number[] {
  if (!Array.isArray(books) || books.length === 0) {
    return [];
  }

  const ownedWholeNumbers = new Set<number>();
  const omnibusCoveredNumbers = new Set<number>();
  for (const book of books) {
    const omnibusMatch = String(book?.title ?? "").match(OMNIBUS_RANGE_PATTERN);
    if (omnibusMatch) {
      const start = Number(omnibusMatch[1]);
      const end = Number(omnibusMatch[2]);
      if (Number.isInteger(start) && Number.isInteger(end) && start > 0 && end > 0) {
        const lower = Math.min(start, end);
        const upper = Math.max(start, end);
        for (let number = lower; number <= upper; number += 1) {
          omnibusCoveredNumbers.add(number);
        }
      }
    }

    const rawCandidate = book?.book_number ?? book?.series_order;
    const candidate = typeof rawCandidate === "number" && Number.isFinite(rawCandidate)
      ? rawCandidate
      : null;
    if (candidate === null) {
      continue;
    }
    if (!Number.isInteger(candidate) || candidate <= 0) {
      continue;
    }
    ownedWholeNumbers.add(candidate);
  }

  if (ownedWholeNumbers.size < 2) {
    return [];
  }

  const highestOwned = Math.max(...ownedWholeNumbers);
  const missing: number[] = [];
  for (let number = 1; number <= highestOwned; number += 1) {
    if (!ownedWholeNumbers.has(number) && !omnibusCoveredNumbers.has(number)) {
      missing.push(number);
    }
  }

  return missing;
}

function mergeMissingNumbers(knownMissing: string[] | undefined, inferredMissing: number[]): string[] {
  const merged = new Set<string>();
  if (Array.isArray(knownMissing)) {
    for (const value of knownMissing) {
      const normalized = String(value).trim();
      if (normalized) {
        merged.add(normalized);
      }
    }
  }

  for (const number of inferredMissing) {
    merged.add(String(number));
  }

  return Array.from(merged).sort((a, b) => Number(a) - Number(b));
}

function formatMissingBooksLabel(missingBooks: Array<number | string> | undefined): string | null {
  if (!Array.isArray(missingBooks) || missingBooks.length === 0) {
    return null;
  }

  const values = missingBooks
    .map((value) => String(value).trim())
    .filter((value) => value.length > 0);

  if (values.length === 0) {
    return null;
  }

  return `Missing: Book ${values.join(", ")}`;
}

function getCheckBannerClassName(tone: CheckBannerTone) {
  if (tone === "success") {
    return "border-emerald-200 bg-emerald-50 text-emerald-900";
  }

  if (tone === "danger") {
    return "border-rose-200 bg-rose-50 text-rose-900";
  }

  return "border-amber-200 bg-amber-50 text-amber-900";
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

type SeriesSortKey = "id" | "name" | "author" | "nextUnread" | "nextUpcoming" | "total" | "lastChecked";
type SortDirection = "asc" | "desc";
type SeriesColumnKey = "id" | "name" | "author" | "nextUnread" | "nextUpcoming" | "total" | "lastChecked" | "actions";

const DEFAULT_SERIES_COLUMN_WIDTHS: Record<SeriesColumnKey, number> = {
  id: 6,
  name: 22,
  author: 18,
  nextUnread: 10,
  nextUpcoming: 12,
  total: 8,
  lastChecked: 12,
  actions: 12,
};

const MIN_SERIES_COLUMN_WIDTHS: Record<SeriesColumnKey, number> = {
  id: 4,
  name: 12,
  author: 10,
  nextUnread: 8,
  nextUpcoming: 8,
  total: 6,
  lastChecked: 8,
  actions: 8,
};

const SERIES_RESIZE_NEIGHBOR: Record<SeriesColumnKey, SeriesColumnKey | null> = {
  id: "name",
  name: "author",
  author: "nextUnread",
  nextUnread: "nextUpcoming",
  nextUpcoming: "total",
  total: "lastChecked",
  lastChecked: "actions",
  actions: null,
};

const SERIES_TABLE_COLUMN_WIDTHS_STORAGE_KEY = "seriesTableColumnWidthsV1";

function sanitizeSavedSeriesColumnWidths(value: unknown): Record<SeriesColumnKey, number> | null {
  if (!value || typeof value !== "object") return null;
  const candidate = value as Partial<Record<SeriesColumnKey, unknown>>;

  const keys: SeriesColumnKey[] = ["id", "name", "author", "nextUnread", "nextUpcoming", "total", "lastChecked", "actions"];
  const next: Partial<Record<SeriesColumnKey, number>> = {};

  for (const key of keys) {
    const raw = candidate[key];
    if (typeof raw !== "number" || !Number.isFinite(raw)) {
      return null;
    }
    const minimum = MIN_SERIES_COLUMN_WIDTHS[key];
    next[key] = Math.max(minimum, Number(raw));
  }

  const total = keys.reduce((sum, key) => sum + (next[key] ?? 0), 0);
  if (total <= 0) return null;

  return {
    id: Number((((next.id ?? DEFAULT_SERIES_COLUMN_WIDTHS.id) / total) * 100).toFixed(2)),
    name: Number((((next.name ?? DEFAULT_SERIES_COLUMN_WIDTHS.name) / total) * 100).toFixed(2)),
    author: Number((((next.author ?? DEFAULT_SERIES_COLUMN_WIDTHS.author) / total) * 100).toFixed(2)),
    nextUnread: Number((((next.nextUnread ?? DEFAULT_SERIES_COLUMN_WIDTHS.nextUnread) / total) * 100).toFixed(2)),
    nextUpcoming: Number((((next.nextUpcoming ?? DEFAULT_SERIES_COLUMN_WIDTHS.nextUpcoming) / total) * 100).toFixed(2)),
    total: Number((((next.total ?? DEFAULT_SERIES_COLUMN_WIDTHS.total) / total) * 100).toFixed(2)),
    lastChecked: Number((((next.lastChecked ?? DEFAULT_SERIES_COLUMN_WIDTHS.lastChecked) / total) * 100).toFixed(2)),
    actions: Number((((next.actions ?? DEFAULT_SERIES_COLUMN_WIDTHS.actions) / total) * 100).toFixed(2)),
  };
}

export default function SeriesPage() {
  const { toast } = useToast();
  const [series, setSeries] = useState<SeriesRow[]>([]);
  const [viewMode, setViewMode] = useState<"ongoing" | "finished">("ongoing");
  const [quickSearch, setQuickSearch] = useState("");
  const [loadingId, setLoadingId] = useState<number | null>(null);
  const [message, setMessage] = useState("");
  const [checkBanner, setCheckBanner] = useState<CheckBannerState | null>(null);
  const [rowCheckState, setRowCheckState] = useState<Record<number, CheckBannerState>>({});

  function dismissCheckBanner(seriesId?: number) {
    setCheckBanner(null);
    if (seriesId === undefined) {
      return;
    }
    setRowCheckState((prev) => {
      const next = { ...prev };
      delete next[seriesId];
      return next;
    });
  }
  const [valueFilters, setValueFilters] = useState({
    name: [] as string[],
    author: [] as string[],
  });
  const [valueFilterSearch, setValueFilterSearch] = useState({
    name: "",
    author: "",
  });
  const [sortConfig, setSortConfig] = useState<{ key: SeriesSortKey | null; direction: SortDirection }>({
    key: null,
    direction: "asc",
  });
  const [columnWidths, setColumnWidths] = useState<Record<SeriesColumnKey, number>>(DEFAULT_SERIES_COLUMN_WIDTHS);
  const tableWrapRef = useRef<HTMLDivElement | null>(null);
  const resizeStateRef = useRef<{
    key: SeriesColumnKey;
    neighborKey: SeriesColumnKey;
    startX: number;
    startWidth: number;
    startNeighborWidth: number;
    containerWidth: number;
  } | null>(null);
  const firstSeriesId = series.length > 0 ? series[0]?.id : null;
  const detailHref = firstSeriesId ? `/series/${firstSeriesId}?fromView=${viewMode}` : "/series";

  useEffect(() => {
    const rafId = window.requestAnimationFrame(() => {
      const sourceView = new URLSearchParams(window.location.search).get("view");
      if (sourceView === "ongoing" || sourceView === "finished") {
        setViewMode(sourceView);
      }
    });

    return () => window.cancelAnimationFrame(rafId);
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    const current = url.searchParams.get("view");
    if (current === viewMode) return;
    url.searchParams.set("view", viewMode);
    window.history.replaceState({}, "", url.toString());
  }, [viewMode]);

  useEffect(() => {
    const rafId = window.requestAnimationFrame(() => {
      try {
        const saved = window.localStorage.getItem(SERIES_TABLE_COLUMN_WIDTHS_STORAGE_KEY);
        if (!saved) return;
        const parsed = JSON.parse(saved);
        const restored = sanitizeSavedSeriesColumnWidths(parsed);
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
      window.localStorage.setItem(SERIES_TABLE_COLUMN_WIDTHS_STORAGE_KEY, JSON.stringify(columnWidths));
    } catch {
      // Ignore storage write errors.
    }
  }, [columnWidths]);

  const totalBooks = series.reduce(
    (sum, s) => sum + (s.books_tracked ?? 0),
    0
  );
  const ongoingCount = useMemo(() => series.filter((row) => !row.is_finished).length, [series]);
  const finishedCount = useMemo(() => series.filter((row) => row.is_finished).length, [series]);

  const nameOptions = useMemo(
    () => Array.from(new Set(series.map((row) => String(row.name || "").trim()))).sort((a, b) => a.localeCompare(b)),
    [series],
  );
  const authorOptions = useMemo(
    () => Array.from(new Set(series.map((row) => String(row.author || "").trim()))).sort((a, b) => a.localeCompare(b)),
    [series],
  );

  const filteredSeries = useMemo(() => {
    const normalizedQuickSearch = normalizeText(quickSearch);

    return series.filter((row) => {
      if (viewMode === "finished" && !row.is_finished) return false;
      if (viewMode === "ongoing" && row.is_finished) return false;
      if (valueFilters.name.length > 0 && !valueFilters.name.includes(String(row.name || "").trim())) return false;
      if (valueFilters.author.length > 0 && !valueFilters.author.includes(String(row.author || "").trim())) return false;
      if (normalizedQuickSearch) {
        const haystack = `${String(row.name || "")} ${String(row.author || "")}`;
        if (!normalizeText(haystack).includes(normalizedQuickSearch)) return false;
      }
      return true;
    });
  }, [quickSearch, series, valueFilters, viewMode]);

  const sortedSeries = useMemo(() => {
    const prioritizedSeries = filteredSeries
      .map((row, index) => ({ row, index }))
      .sort((a, b) => {
        const priorityDelta = getSeriesPriority(a.row) - getSeriesPriority(b.row);
        if (priorityDelta !== 0) {
          return priorityDelta;
        }

        if (!sortConfig.key) {
          return a.index - b.index;
        }

        const parsedTime = (value?: string | null) => parseFlexibleDate(value)?.valueOf() ?? 0;

        const key = sortConfig.key;

        const aValue =
          key === "id"
            ? Number(a.row.id ?? 0)
            : key === "name"
              ? String(a.row.name || "")
              : key === "author"
                ? String(a.row.author || "")
                : key === "nextUnread"
                  ? Number(a.row.next_unread_book_number ?? 0)
                  : key === "nextUpcoming"
                    ? Number(a.row.next_upcoming_book_number ?? 0)
                    : key === "total"
                      ? Number(a.row.total_books ?? 0)
                      : parsedTime(a.row.last_checked);

        const bValue =
          key === "id"
            ? Number(b.row.id ?? 0)
            : key === "name"
              ? String(b.row.name || "")
              : key === "author"
                ? String(b.row.author || "")
                : key === "nextUnread"
                  ? Number(b.row.next_unread_book_number ?? 0)
                  : key === "nextUpcoming"
                    ? Number(b.row.next_upcoming_book_number ?? 0)
                    : key === "total"
                      ? Number(b.row.total_books ?? 0)
                      : parsedTime(b.row.last_checked);

        if (typeof aValue === "number" && typeof bValue === "number") {
          const valueDelta = aValue - bValue;
          if (valueDelta !== 0) {
            return sortConfig.direction === "asc" ? valueDelta : -valueDelta;
          }
        } else {
          const valueDelta = String(aValue).localeCompare(String(bValue), undefined, { sensitivity: "base" });
          if (valueDelta !== 0) {
            return sortConfig.direction === "asc" ? valueDelta : -valueDelta;
          }
        }

        return a.index - b.index;
      })
      .map((item) => item.row);

    return prioritizedSeries;
  }, [filteredSeries, sortConfig]);

  function toggleSort(key: SeriesSortKey) {
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

  function sortLabel(key: SeriesSortKey) {
    if (sortConfig.key !== key) return "";
    return sortConfig.direction === "asc" ? " ▲" : " ▼";
  }

  function toggleValueFilter(kind: "name" | "author", value: string) {
    setValueFilters((prev) => {
      const exists = prev[kind].includes(value);
      return {
        ...prev,
        [kind]: exists ? prev[kind].filter((item) => item !== value) : [...prev[kind], value],
      };
    });
  }

  function clearFilters() {
    setValueFilters({ name: [], author: [] });
    setValueFilterSearch({ name: "", author: "" });
  }

  useEffect(() => {
    const handleMouseMove = (event: MouseEvent) => {
      const active = resizeStateRef.current;
      if (!active) return;

      const deltaX = event.clientX - active.startX;
      const deltaPercent = (deltaX / active.containerWidth) * 100;
      const minCurrent = MIN_SERIES_COLUMN_WIDTHS[active.key];
      const minNeighbor = MIN_SERIES_COLUMN_WIDTHS[active.neighborKey];
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

  function startColumnResize(key: SeriesColumnKey, event: React.MouseEvent<HTMLButtonElement>) {
    const neighborKey = SERIES_RESIZE_NEIGHBOR[key];
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

  async function fetchSeries() {
    try {
      const response = await fetchApiWithFallback("/series/", {
        cache: "no-store",
      });
      const baseSeries = await response.json();

      if (!Array.isArray(baseSeries)) {
        setSeries([]);
        return;
      }

      // All necessary data (books, intelligence fields) is already present in the
      // SeriesApiRow objects returned by /series/. Making a further per-series
      // request to /series/{id} here was a redundant N+1 pattern that overwhelmed
      // the backend and caused 500s/CORS failures once there were 100+ series.
      const hydrated = (baseSeries as SeriesDetailApiRow[]).map((item) => {
        const books = Array.isArray(item.books) ? item.books : [];
        const booksTracked = books.length;
        const inferredMissingNumbers = inferMissingNumbersFromBooks(books);
        const mergedMissingBooks = mergeMissingNumbers(
          item.missing_books,
          inferredMissingNumbers,
        );
        const seriesState = item.series_state ?? {
          has_new_books: Boolean(item.has_new_books ?? false),
          has_unread_books: Boolean(item.has_unread_books ?? false),
          has_upcoming_books: Boolean(item.has_upcoming_books ?? false),
          is_caught_up: Boolean(item.is_caught_up ?? false),
        };

        return {
          id: item.id,
          name: item.name,
          author: item.author ?? null,
          is_finished: Boolean(item.is_finished ?? false),
          series_status: item.series_status ?? null,
          next_unread_book_number: item.next_unread_book_number ?? null,
          next_upcoming_book_number: item.next_upcoming_book_number ?? null,
          total_books: item.total_books ?? null,
          books_tracked: booksTracked,
          last_checked: item.updated_at ?? null,
          updated_at: item.updated_at ?? null,
          has_new_books: seriesState.has_new_books,
          has_unread_books: seriesState.has_unread_books,
          has_upcoming_books: seriesState.has_upcoming_books,
          is_caught_up: seriesState.is_caught_up,
          missing_books: mergedMissingBooks,
          inferred_missing_numbers: inferredMissingNumbers,
          series_state: seriesState,
        } satisfies SeriesRow;
      });

      setSeries(hydrated);
    } catch (error) {
      console.error("Error fetching series:", error);
      setMessage(error instanceof Error ? `Unable to load series: ${error.message}` : "Unable to load series right now.");
    }
  }

  useEffect(() => {
    const rafId = window.requestAnimationFrame(() => {
      void fetchSeries();
    });
    return () => window.cancelAnimationFrame(rafId);
  }, []);

  async function handleCheckNow(seriesId: number) {
    const targetSeries = series.find((item) => item.id === seriesId);
    const seriesTitle = String(targetSeries?.name || `Series ${seriesId}`);
    const missingLabel = formatMissingBooksLabel(targetSeries?.missing_books);

    setLoadingId(seriesId);
    setMessage("");
    setRowCheckState((prev) => ({
      ...prev,
      [seriesId]: {
        seriesId,
        seriesTitle,
        tone: "error",
        title: `${seriesTitle} Checking`,
        message: missingLabel ? `${missingLabel}. Checking missing books first...` : "Checking for new books...",
      },
    }));

    try {
      const response = await fetchApiWithFallback(`/series/${seriesId}/check`, { method: "POST" });
      const kickoff = (await response.json()) as SeriesCheckStatusResponse;
      const sessionId = kickoff.session_id;

      let statusPayload = kickoff;
      let pollCount = 0;
      let unchangedStatusPolls = 0;
      let lastStatusFingerprint = `${statusPayload.updated_at || ""}|${statusPayload.progress_completed || 0}|${statusPayload.current_book_number || ""}`;
      while (statusPayload.status === "started" || statusPayload.status === "running") {
        if (pollCount >= CHECK_STATUS_MAX_POLLS) {
          throw new Error("Series check timed out. It may still be running in the background; try again in a moment.");
        }

        await delay(CHECK_STATUS_POLL_INTERVAL_MS);
        const statusPath = sessionId
          ? `/series/${seriesId}/check/status?session_id=${encodeURIComponent(sessionId)}`
          : `/series/${seriesId}/check/status`;
        const statusResponse = await fetchApiWithFallback(statusPath, { cache: "no-store" });
        statusPayload = (await statusResponse.json()) as SeriesCheckStatusResponse;
        pollCount += 1;

        const nextStatusFingerprint = `${statusPayload.updated_at || ""}|${statusPayload.progress || 0}|${statusPayload.current_pass || ""}`;
        if (nextStatusFingerprint === lastStatusFingerprint) {
          unchangedStatusPolls += 1;
        } else {
          unchangedStatusPolls = 0;
          lastStatusFingerprint = nextStatusFingerprint;
        }

        if (unchangedStatusPolls >= CHECK_STATUS_STALLED_POLLS) {
          throw new Error("Series check appears stalled. Please try again.");
        }

        const completed = Number(statusPayload.progress_completed || 0);
        const total = Number(statusPayload.progress_total || 0);
        const progress = Number(statusPayload.progress || 0);
        const currentBook = statusPayload.current_book_number;
        const currentPass = statusPayload.current_pass;
        setRowCheckState((prev) => ({
          ...prev,
          [seriesId]: {
            seriesId,
            seriesTitle,
            tone: "error",
            title: `${seriesTitle} Checking`,
            message: total > 0
              ? `Checking ${completed}/${total}${currentBook ? ` (book ${currentBook})` : ""}${currentPass ? ` • ${currentPass}` : ""}...`
              : `Checking ${progress}%${currentPass ? ` • ${currentPass}` : ""}...`,
          },
        }));
      }

      if (statusPayload.error) {
        throw new Error(statusPayload.error || "Error checking series.");
      }

      const data = statusPayload.result ?? {};
      const missingFromStatus = Array.isArray(statusPayload.missing_books) ? statusPayload.missing_books : [];

      let nextBanner: CheckBannerState;
      const foundBooks = Array.isArray(statusPayload.found_books)
        ? statusPayload.found_books
        : Array.isArray(data.added_books)
          ? data.added_books
          : [];

      if (foundBooks.length > 0) {
        nextBanner = {
          seriesId,
          seriesTitle,
          tone: "success",
          title: `${seriesTitle} Checked`,
          message: foundBooks.length === 1
            ? "Book added to series and library."
            : `${foundBooks.length} books added to series and library.`,
          actionHref: `/series/${seriesId}?fromView=${viewMode}`,
          actionLabel: "View series",
        };
        setMessage(nextBanner.message);
        toast({
          title: `${seriesTitle} Checked`,
          description: nextBanner.message,
        });
      } else {
        const diagnosticDetail = summarizeCandidateDiagnostics(
          typeof data === "object" && data !== null ? (data as Record<string, unknown>).candidate_diagnostics : null,
        );
        const missingAfterCheck = missingFromStatus.length > 0
          ? missingFromStatus
          : Array.isArray(targetSeries?.missing_books)
            ? targetSeries.missing_books
            : [];
        const missingDetail = formatMissingBooksLabel(missingAfterCheck);
        nextBanner = {
          seriesId,
          seriesTitle,
          tone: "danger",
          title: `${seriesTitle} Checked`,
          message: "No new books.",
          detail: [missingDetail, diagnosticDetail].filter(Boolean).join(". ") || undefined,
        };
        setMessage("No new books.");
        toast({
          title: `${seriesTitle} Checked`,
          description: [missingDetail, diagnosticDetail].filter(Boolean).length > 0
            ? `No new books. ${[missingDetail, diagnosticDetail].filter(Boolean).join(". ")}`
            : "No new books.",
        });
      }

      setCheckBanner(nextBanner);
      setRowCheckState((prev) => ({
        ...prev,
        [seriesId]: nextBanner,
      }));

      fetchSeries();
    } catch (error) {
      console.error("Error checking series:", error);
      const errorBanner: CheckBannerState = {
        seriesId,
        seriesTitle,
        tone: "error",
        title: `${seriesTitle} Check Failed`,
        message: error instanceof Error ? error.message : "Error checking series.",
      };
      setCheckBanner(errorBanner);
      setRowCheckState((prev) => ({
        ...prev,
        [seriesId]: errorBanner,
      }));
      setMessage(errorBanner.message);
      toast({
        title: errorBanner.title,
        description: errorBanner.message,
      });
    }

    setLoadingId(null);
  }

  return (
    <div className="p-4 space-y-3">
      <div className="grid gap-2 md:grid-cols-[1fr_auto_auto] md:items-start">
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground">
            Series library
          </p>
          <h1 className="text-2xl font-bold">{viewMode === "ongoing" ? "Unfinished Series" : "Finished Series"}</h1>
          <p className="max-w-2xl text-xs leading-5 text-muted-foreground md:hidden">
            Browse your tracked series and refresh status for each series.
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
            <span className="inline-flex items-center gap-1">
              <span aria-hidden="true">⭐</span>
              <span>new books found</span>
            </span>
            <span className="inline-flex items-center gap-1">
              <span aria-hidden="true">📘</span>
              <span>unread books remain</span>
            </span>
            <span className="inline-flex items-center gap-1">
              <span aria-hidden="true">🔮</span>
              <span>upcoming books remain</span>
            </span>
          </div>
        </div>

        <div className="flex justify-start md:justify-self-center">
          <table className="border border-border bg-card/70 text-xs">
            <tbody>
              <tr>
                <td className="min-w-[140px] border border-border px-2 py-1">Series: <span className="font-semibold">{series.length}</span></td>
                <td className="min-w-[140px] border border-border px-2 py-1">Books: <span className="font-semibold">{totalBooks}</span></td>
                <td className="min-w-[140px] border border-border px-2 py-1">Showing: <span className="font-semibold">{sortedSeries.length}</span></td>
              </tr>
            </tbody>
          </table>
        </div>

        <div className="flex flex-wrap gap-2 md:justify-self-end">
          <Button
            variant={viewMode === "ongoing" ? "secondary" : "outline"}
            onClick={() => setViewMode("ongoing")}
          >
            Unfinished Series ({ongoingCount})
          </Button>
          <Button
            variant={viewMode === "finished" ? "secondary" : "outline"}
            onClick={() => setViewMode("finished")}
          >
            Finished Series ({finishedCount})
          </Button>
          <Link href="/books">
            <Button variant="outline">View Library</Button>
          </Link>
          <Link href={detailHref}>
            <Button variant="secondary">Series detail</Button>
          </Link>
        </div>
      </div>

      {checkBanner ? (
        <div className={`rounded-lg border px-4 py-3 text-sm ${getCheckBannerClassName(checkBanner.tone)}`}>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="font-semibold">{checkBanner.title}</p>
              <p>{checkBanner.message}</p>
              {checkBanner.detail ? <p className="text-xs opacity-80">{checkBanner.detail}</p> : null}
            </div>
            <div className="flex items-center gap-2">
              {checkBanner.actionHref && checkBanner.actionLabel ? (
                <Link href={checkBanner.actionHref}>
                  <Button variant={checkBanner.tone === "success" ? "secondary" : "outline"} size="sm">
                    {checkBanner.actionLabel}
                  </Button>
                </Link>
              ) : null}
              <Button type="button" variant="ghost" size="sm" onClick={() => dismissCheckBanner(checkBanner.seriesId)}>
                Dismiss
              </Button>
            </div>
          </div>
        </div>
      ) : message ? (
        <div className="rounded-lg border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-900">
          {message}
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <input
          value={quickSearch}
          onChange={(event) => setQuickSearch(event.target.value)}
          placeholder="Quick search series (e.g. victor)"
          className="h-8 w-full max-w-sm rounded border bg-background px-2 text-xs"
        />
        {quickSearch ? (
          <Button type="button" variant="ghost" size="sm" onClick={() => setQuickSearch("")}>Clear search</Button>
        ) : null}
      </div>

      <div ref={tableWrapRef} className="overflow-x-auto rounded-lg border bg-card/80">
        <Table className="w-full table-fixed text-xs [&_th]:h-8 [&_th]:py-1 [&_td]:py-1">
          <TableHeader>
            <TableRow>
              <TableHead className="relative" style={{ width: `${columnWidths.id}%` }}>
                <button type="button" className="text-left" onClick={() => toggleSort("id")}>ID{sortLabel("id")}</button>
                <button
                  type="button"
                  aria-label="Resize ID column"
                  onMouseDown={(event) => startColumnResize("id", event)}
                  className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
                />
              </TableHead>
              <TableHead className="relative" style={{ width: `${columnWidths.name}%` }}>
                <div className="flex items-center justify-between gap-1">
                  <button type="button" className="text-left" onClick={() => toggleSort("name")}>Name{sortLabel("name")}</button>
                  <ValueFilterMenu
                    label="Filter"
                    options={nameOptions}
                    selectedValues={valueFilters.name}
                    onToggleValue={(value) => toggleValueFilter("name", value)}
                    onClear={() => {
                      setValueFilters((prev) => ({ ...prev, name: [] }));
                      setValueFilterSearch((prev) => ({ ...prev, name: "" }));
                    }}
                    searchValue={valueFilterSearch.name}
                    onSearchChange={(value) => setValueFilterSearch((prev) => ({ ...prev, name: value }))}
                  />
                </div>
                <button
                  type="button"
                  aria-label="Resize Name column"
                  onMouseDown={(event) => startColumnResize("name", event)}
                  className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
                />
              </TableHead>
              <TableHead className="relative" style={{ width: `${columnWidths.author}%` }}>
                <div className="flex items-center justify-between gap-1">
                  <button type="button" className="text-left" onClick={() => toggleSort("author")}>Author{sortLabel("author")}</button>
                  <ValueFilterMenu
                    label="Filter"
                    options={authorOptions}
                    selectedValues={valueFilters.author}
                    onToggleValue={(value) => toggleValueFilter("author", value)}
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
              <TableHead className="relative" style={{ width: `${columnWidths.nextUnread}%` }}>
                <button type="button" className="text-left" onClick={() => toggleSort("nextUnread")}>Next unread{sortLabel("nextUnread")}</button>
                <button
                  type="button"
                  aria-label="Resize Next unread column"
                  onMouseDown={(event) => startColumnResize("nextUnread", event)}
                  className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
                />
              </TableHead>
              <TableHead className="relative" style={{ width: `${columnWidths.nextUpcoming}%` }}>
                <button type="button" className="text-left" onClick={() => toggleSort("nextUpcoming")}>Next upcoming #{sortLabel("nextUpcoming")}</button>
                <button
                  type="button"
                  aria-label="Resize Next upcoming column"
                  onMouseDown={(event) => startColumnResize("nextUpcoming", event)}
                  className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
                />
              </TableHead>
              <TableHead className="relative" style={{ width: `${columnWidths.total}%` }}>
                <button type="button" className="text-left" onClick={() => toggleSort("total")}>Total{sortLabel("total")}</button>
                <button
                  type="button"
                  aria-label="Resize Total column"
                  onMouseDown={(event) => startColumnResize("total", event)}
                  className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
                />
              </TableHead>
              <TableHead className="relative" style={{ width: `${columnWidths.lastChecked}%` }}>
                <button type="button" className="text-left" onClick={() => toggleSort("lastChecked")}>Last checked{sortLabel("lastChecked")}</button>
                <button
                  type="button"
                  aria-label="Resize Last checked column"
                  onMouseDown={(event) => startColumnResize("lastChecked", event)}
                  className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
                />
              </TableHead>
              <TableHead style={{ width: `${columnWidths.actions}%` }}>
                <Button type="button" variant="ghost" size="sm" onClick={clearFilters}>
                  Clear
                </Button>
              </TableHead>
            </TableRow>
          </TableHeader>

          <TableBody>
            {sortedSeries.map((s) => (
              <TableRow key={s.id}>
                <TableCell>{s.id}</TableCell>
                <TableCell className="truncate" title={s.name}>
                  <div className="flex items-center gap-1 truncate">
                    <span className="truncate">{s.name}</span>
                    {getSeriesState(s).has_new_books ? <span className="text-amber-500" aria-label="New books found">⭐</span> : null}
                    {getSeriesState(s).has_unread_books ? <span aria-label="Unread books remain">📘</span> : null}
                    {getSeriesState(s).has_upcoming_books ? <span aria-label="Upcoming books remain">🔮</span> : null}
                    {Array.isArray(s.missing_books) && s.missing_books.length > 0 ? <span aria-label="Missing books detected">🧩</span> : null}
                  </div>
                  {Array.isArray(s.missing_books) && s.missing_books.length > 0 ? (
                    <p className="mt-1 truncate text-[11px] text-rose-700" title={formatMissingBooksLabel(s.missing_books) || undefined}>
                      {formatMissingBooksLabel(s.missing_books)}
                    </p>
                  ) : null}
                </TableCell>
                <TableCell className="truncate" title={s.author || "—"}>{s.author || "—"}</TableCell>
                <TableCell>{s.next_unread_book_number ?? "—"}</TableCell>
                <TableCell>{s.next_upcoming_book_number ?? "—"}</TableCell>
                <TableCell>{s.total_books ?? "—"}</TableCell>
                <TableCell>{formatDate(s.last_checked)}</TableCell>
                <TableCell className="whitespace-nowrap">
                  <div className="flex items-center gap-2 whitespace-nowrap">
                    <Link href={`/series/${s.id}?fromView=${viewMode}`}>
                      <Button variant="ghost" size="sm" className="shrink-0">
                        View books
                      </Button>
                    </Link>
                    <Button
                      variant="outline"
                      size="sm"
                      className="shrink-0"
                      onClick={() => handleCheckNow(s.id)}
                      disabled={loadingId === s.id}
                    >
                      {loadingId === s.id ? "Checking…" : "Check for New"}
                    </Button>
                    {rowCheckState[s.id]?.actionHref && rowCheckState[s.id]?.actionLabel ? (
                      <Link href={rowCheckState[s.id].actionHref!}>
                        <Button variant="secondary" size="sm" className="shrink-0">
                          {rowCheckState[s.id].actionLabel}
                        </Button>
                      </Link>
                    ) : null}
                  </div>
                  {rowCheckState[s.id] ? (
                    <div className={`mt-2 rounded border px-2 py-1 text-[11px] ${getCheckBannerClassName(rowCheckState[s.id].tone)}`}>
                      <span className="font-semibold">{rowCheckState[s.id].title}</span>
                      <span className="ml-1">{rowCheckState[s.id].message}</span>
                      {rowCheckState[s.id].detail ? <span className="ml-1 opacity-80">{rowCheckState[s.id].detail}</span> : null}
                      <button
                        type="button"
                        onClick={() => dismissCheckBanner(s.id)}
                        className="ml-2 underline underline-offset-2"
                      >
                        dismiss
                      </button>
                    </div>
                  ) : null}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      <p className="text-xs text-muted-foreground">
        Showing {sortedSeries.length} of {viewMode === "ongoing" ? ongoingCount : finishedCount} series.
      </p>
      <p className="text-xs text-muted-foreground">
        &quot;Next upcoming #&quot; is the next upcoming book number in that series.
      </p>
    </div>
  );
}
