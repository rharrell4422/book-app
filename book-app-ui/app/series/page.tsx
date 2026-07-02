"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useToast } from "@/components/ui/use-toast";
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
};

type SeriesDetailApiRow = SeriesApiRow & {
  books?: Array<unknown>;
};

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
  const candidates = baseCandidates.map((base) => `${normalizeBaseUrl(base)}${normalizedPath}`);

  if (normalizedPath.endsWith("/")) {
    const trimmedPath = normalizedPath.slice(0, -1);
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
    if (!sortConfig.key) return filteredSeries;

    const parsedTime = (value?: string | null) => parseFlexibleDate(value)?.valueOf() ?? 0;

    const sorted = [...filteredSeries].sort((a, b) => {
      const key = sortConfig.key;

      const aValue =
        key === "id"
          ? Number(a.id ?? 0)
          : key === "name"
            ? String(a.name || "")
            : key === "author"
              ? String(a.author || "")
              : key === "nextUnread"
                ? Number(a.next_unread_book_number ?? 0)
                : key === "nextUpcoming"
                  ? Number(a.next_upcoming_book_number ?? 0)
                  : key === "total"
                    ? Number(a.total_books ?? 0)
                    : parsedTime(a.last_checked);

      const bValue =
        key === "id"
          ? Number(b.id ?? 0)
          : key === "name"
            ? String(b.name || "")
            : key === "author"
              ? String(b.author || "")
              : key === "nextUnread"
                ? Number(b.next_unread_book_number ?? 0)
                : key === "nextUpcoming"
                  ? Number(b.next_upcoming_book_number ?? 0)
                  : key === "total"
                    ? Number(b.total_books ?? 0)
                    : parsedTime(b.last_checked);

      if (typeof aValue === "number" && typeof bValue === "number") {
        return aValue - bValue;
      }

      return String(aValue).localeCompare(String(bValue), undefined, { sensitivity: "base" });
    });

    return sortConfig.direction === "asc" ? sorted : sorted.reverse();
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

      const detailResults = await Promise.allSettled(
        (baseSeries as SeriesApiRow[]).map(async (item) => {
          const detailResponse = await fetchApiWithFallback(`/series/${item.id}`, {
            cache: "no-store",
          });
          return detailResponse.json();
        })
      );

      const hydrated = (baseSeries as SeriesApiRow[]).map((item, index: number) => {
        const detail = detailResults[index];
        const detailData: SeriesDetailApiRow | null = detail?.status === "fulfilled" ? (detail.value as SeriesDetailApiRow) : null;
        const booksTracked = Array.isArray(detailData?.books)
          ? detailData.books.length
          : 0;

        return {
          id: item.id,
          name: item.name,
          author: detailData?.author ?? item.author ?? null,
          is_finished: Boolean(detailData?.is_finished ?? item.is_finished ?? false),
          series_status: detailData?.series_status ?? item.series_status ?? null,
          next_unread_book_number:
            detailData?.next_unread_book_number ?? item.next_unread_book_number ?? null,
          next_upcoming_book_number:
            detailData?.next_upcoming_book_number ?? item.next_upcoming_book_number ?? null,
          total_books: detailData?.total_books ?? item.total_books ?? null,
          books_tracked: booksTracked,
          last_checked: detailData?.updated_at ?? item.updated_at ?? null,
          updated_at: detailData?.updated_at ?? item.updated_at ?? null,
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
    setLoadingId(seriesId);
    setMessage("");

    try {
      const response = await fetchApiWithFallback(`/series/${seriesId}/check`, { method: "POST" });
      const data = await response.json();

      setMessage(
        `Series ${seriesId} refreshed. Next upcoming: ${
          data.next_upcoming_book_number ?? "None"
        }.`
      );

      toast({
        title: "Series refreshed",
        description: `Series ${seriesId} has been updated.`,
      });

      fetchSeries();
    } catch (error) {
      console.error("Error checking series:", error);
      setMessage("Error checking series.");
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
          <h1 className="text-2xl font-bold">{viewMode === "ongoing" ? "Ongoing Series" : "Finished Series"}</h1>
          <p className="max-w-2xl text-xs leading-5 text-muted-foreground md:hidden">
            Browse your tracked series and refresh status for each series.
          </p>
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
            Ongoing Series ({ongoingCount})
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

      {message && (
        <div className="rounded-lg border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-900">
          {message}
        </div>
      )}

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
                <TableCell className="truncate" title={s.name}>{s.name}</TableCell>
                <TableCell className="truncate" title={s.author || "—"}>{s.author || "—"}</TableCell>
                <TableCell>{s.next_unread_book_number ?? "—"}</TableCell>
                <TableCell>{s.next_upcoming_book_number ?? "—"}</TableCell>
                <TableCell>{s.total_books ?? "—"}</TableCell>
                <TableCell>{formatDate(s.last_checked)}</TableCell>
                <TableCell className="space-x-2 whitespace-nowrap">
                  <Link href={`/series/${s.id}?fromView=${viewMode}`}>
                    <Button variant="ghost" size="sm">
                      View books
                    </Button>
                  </Link>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => handleCheckNow(s.id)}
                    disabled={loadingId === s.id}
                  >
                    {loadingId === s.id ? "Checking…" : "Refresh"}
                  </Button>
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
