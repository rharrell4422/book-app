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
  check_url?: string | null;
  next_unread_book_number?: number | null;
  next_upcoming_book_number?: number | null;
  total_books?: number | null;
  books_tracked?: number;
  last_checked?: string | null;
  updated_at?: string | null;
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

type SeriesSortKey = "id" | "name" | "author" | "nextUnread" | "nextUpcoming" | "total" | "lastChecked";
type SortDirection = "asc" | "desc";

export default function SeriesPage() {
  const { toast } = useToast();
  const [series, setSeries] = useState<SeriesRow[]>([]);
  const [loadingId, setLoadingId] = useState<number | null>(null);
  const [message, setMessage] = useState("");
  const [filters, setFilters] = useState({
    id: "",
    name: "",
    author: "",
    nextUnread: "",
    nextUpcoming: "",
    total: "",
  });
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
  const firstSeriesId = series.length > 0 ? series[0]?.id : null;
  const detailHref = firstSeriesId ? `/series/${firstSeriesId}` : "/series";

  const totalBooks = series.reduce(
    (sum, s) => sum + (s.books_tracked ?? 0),
    0
  );

  const nameOptions = useMemo(
    () => Array.from(new Set(series.map((row) => String(row.name || "").trim()))).sort((a, b) => a.localeCompare(b)),
    [series],
  );
  const authorOptions = useMemo(
    () => Array.from(new Set(series.map((row) => String(row.author || "").trim()))).sort((a, b) => a.localeCompare(b)),
    [series],
  );

  const filteredSeries = useMemo(() => {
    const idFilter = normalizeText(filters.id);
    const nameFilter = normalizeText(filters.name);
    const authorFilter = normalizeText(filters.author);
    const nextUnreadFilter = normalizeText(filters.nextUnread);
    const nextUpcomingFilter = normalizeText(filters.nextUpcoming);
    const totalFilter = normalizeText(filters.total);

    return series.filter((row) => {
      if (idFilter && !normalizeText(row.id).includes(idFilter)) return false;
      if (nameFilter && !normalizeText(row.name).includes(nameFilter)) return false;
      if (authorFilter && !normalizeText(row.author).includes(authorFilter)) return false;
      if (nextUnreadFilter && !normalizeText(row.next_unread_book_number).includes(nextUnreadFilter)) return false;
      if (nextUpcomingFilter && !normalizeText(row.next_upcoming_book_number).includes(nextUpcomingFilter)) return false;
      if (totalFilter && !normalizeText(row.total_books).includes(totalFilter)) return false;
      if (valueFilters.name.length > 0 && !valueFilters.name.includes(String(row.name || "").trim())) return false;
      if (valueFilters.author.length > 0 && !valueFilters.author.includes(String(row.author || "").trim())) return false;
      return true;
    });
  }, [filters, series, valueFilters]);

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

  function setExplicitSort(key: SeriesSortKey, mode: "none" | "asc" | "desc") {
    if (mode === "none") {
      setSortConfig({ key: null, direction: "asc" });
      return;
    }
    setSortConfig({ key, direction: mode });
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
    setFilters({
      id: "",
      name: "",
      author: "",
      nextUnread: "",
      nextUpcoming: "",
      total: "",
    });
    setValueFilters({ name: [], author: [] });
    setValueFilterSearch({ name: "", author: "" });
  }

  async function fetchSeries() {
    try {
      const response = await fetch("http://localhost:8000/series/", {
        cache: "no-store",
      });
      const baseSeries = await response.json();

      if (!Array.isArray(baseSeries)) {
        setSeries([]);
        return;
      }

      const detailResults = await Promise.allSettled(
        baseSeries.map(async (item: any) => {
          const detailResponse = await fetch(`http://localhost:8000/series/${item.id}`, {
            cache: "no-store",
          });
          if (!detailResponse.ok) {
            throw new Error(`Failed to load details for series ${item.id}`);
          }
          return detailResponse.json();
        })
      );

      const hydrated = baseSeries.map((item: any, index: number) => {
        const detail = detailResults[index];
        const detailData = detail?.status === "fulfilled" ? detail.value : null;
        const booksTracked = Array.isArray(detailData?.books)
          ? detailData.books.length
          : 0;

        return {
          id: item.id,
          name: item.name,
          author: detailData?.author ?? item.author ?? null,
          check_url: item.check_url ?? null,
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
      setMessage("Unable to load series right now.");
    }
  }

  useEffect(() => {
    fetchSeries();
  }, []);

  async function handleCheckNow(seriesId: number) {
    setLoadingId(seriesId);
    setMessage("");

    try {
      const response = await fetch(
        `http://localhost:8000/series/${seriesId}/check`,
        { method: "POST" }
      );
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

  async function handleEditUrl(seriesId: number, currentUrl: string) {
    const newUrl = prompt("Enter new check URL:", currentUrl || "");
    if (newUrl === null) return;

    try {
      const response = await fetch(`http://localhost:8000/series/${seriesId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ check_url: newUrl }),
      });

      if (response.ok) {
        toast({
          title: "URL updated",
          description: `Series ${seriesId} check URL saved.`,
        });
        fetchSeries();
      } else {
        toast({
          title: "Error",
          description: "Failed to update URL.",
        });
      }
    } catch (error) {
      console.error("Error updating URL:", error);
      toast({
        title: "Error",
        description: "Network or server issue while updating URL.",
      });
    }
  }

  return (
    <div className="p-4 space-y-3">
      <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
        <div className="space-y-2">
          <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground">
            Series library
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-2xl font-bold">Series</h1>
            <span className="rounded-full bg-muted px-3 py-1 text-xs uppercase tracking-[0.2em] text-muted-foreground">
              {series.length} tracked
            </span>
          </div>
          <p className="max-w-2xl text-xs leading-5 text-muted-foreground">
            Browse your tracked series, update check URLs, and refresh status for each series.
          </p>
        </div>

        <div className="flex flex-wrap gap-2">
          <Link href="/books">
            <Button variant="outline">View Library</Button>
          </Link>
          <Link href={detailHref}>
            <Button variant="secondary">Series detail</Button>
          </Link>
        </div>
      </div>

      <div className="rounded-md border bg-card/70 px-3 py-2">
        <div className="grid grid-cols-1 gap-2 text-xs sm:grid-cols-3">
          <div className="rounded bg-background/70 px-2 py-1">
            <span className="text-muted-foreground">Series tracked</span>
            <span className="ml-2 font-semibold">{series.length}</span>
          </div>
          <div className="rounded bg-background/70 px-2 py-1">
            <span className="text-muted-foreground">Books tracked</span>
            <span className="ml-2 font-semibold">{totalBooks}</span>
          </div>
          <div className="rounded bg-background/70 px-2 py-1">
            <span className="text-muted-foreground">Refresh</span>
            <span className="ml-2 font-semibold">{loadingId ? "Refreshing..." : "Ready"}</span>
          </div>
        </div>
      </div>

      {message && (
        <div className="rounded-lg border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-900">
          {message}
        </div>
      )}

      <div className="overflow-x-auto rounded-lg border bg-card/80">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>
                <button type="button" className="text-left" onClick={() => toggleSort("id")}>ID{sortLabel("id")}</button>
              </TableHead>
              <TableHead>
                <button type="button" className="text-left" onClick={() => toggleSort("name")}>Name{sortLabel("name")}</button>
              </TableHead>
              <TableHead>
                <button type="button" className="text-left" onClick={() => toggleSort("author")}>Author{sortLabel("author")}</button>
              </TableHead>
              <TableHead>
                <button type="button" className="text-left" onClick={() => toggleSort("nextUnread")}>Next unread{sortLabel("nextUnread")}</button>
              </TableHead>
              <TableHead>
                <button type="button" className="text-left" onClick={() => toggleSort("nextUpcoming")}>Next upcoming{sortLabel("nextUpcoming")}</button>
              </TableHead>
              <TableHead>
                <button type="button" className="text-left" onClick={() => toggleSort("total")}>Total{sortLabel("total")}</button>
              </TableHead>
              <TableHead className="min-w-[200px]">
                <button type="button" className="text-left" onClick={() => toggleSort("lastChecked")}>Last checked{sortLabel("lastChecked")}</button>
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
                  value={filters.name}
                  onChange={(event) => setFilters((prev) => ({ ...prev, name: event.target.value }))}
                  placeholder="Filter"
                  className="h-8 w-full rounded border bg-background px-2 text-xs"
                />
                <div className="mt-1">
                  <ValueFilterMenu
                    label="Values"
                    options={nameOptions}
                    selectedValues={valueFilters.name}
                    onToggleValue={(value) => toggleValueFilter("name", value)}
                    onClear={() => {
                      setValueFilters((prev) => ({ ...prev, name: [] }));
                      setValueFilterSearch((prev) => ({ ...prev, name: "" }));
                      setFilters((prev) => ({ ...prev, name: "" }));
                    }}
                    searchValue={valueFilterSearch.name}
                    onSearchChange={(value) => setValueFilterSearch((prev) => ({ ...prev, name: value }))}
                  />
                </div>
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
                <input
                  value={filters.nextUnread}
                  onChange={(event) => setFilters((prev) => ({ ...prev, nextUnread: event.target.value }))}
                  placeholder="Filter"
                  className="h-8 w-full rounded border bg-background px-2 text-xs"
                />
              </TableHead>
              <TableHead>
                <input
                  value={filters.nextUpcoming}
                  onChange={(event) => setFilters((prev) => ({ ...prev, nextUpcoming: event.target.value }))}
                  placeholder="Filter"
                  className="h-8 w-full rounded border bg-background px-2 text-xs"
                />
              </TableHead>
              <TableHead>
                <input
                  value={filters.total}
                  onChange={(event) => setFilters((prev) => ({ ...prev, total: event.target.value }))}
                  placeholder="Filter"
                  className="h-8 w-full rounded border bg-background px-2 text-xs"
                />
              </TableHead>
              <TableHead className="min-w-[200px] align-top">
                <select
                  value={sortConfig.key === "lastChecked" ? sortConfig.direction : "none"}
                  onChange={(event) =>
                    setExplicitSort("lastChecked", event.target.value as "none" | "asc" | "desc")
                  }
                  className="block h-8 w-full rounded border bg-background px-2 text-xs"
                >
                  <option value="none">Sort date</option>
                  <option value="asc">A to Z (oldest)</option>
                  <option value="desc">Z to A (newest)</option>
                </select>
              </TableHead>
              <TableHead>
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
                <TableCell>{s.name}</TableCell>
                <TableCell>{s.author || "—"}</TableCell>
                <TableCell>{s.next_unread_book_number ?? "—"}</TableCell>
                <TableCell>{s.next_upcoming_book_number ?? "—"}</TableCell>
                <TableCell>{s.total_books ?? "—"}</TableCell>
                <TableCell>{formatDate(s.last_checked)}</TableCell>
                <TableCell className="space-x-2 whitespace-nowrap">
                  <Link href={`/books?series_id=${s.id}`}>
                    <Button variant="ghost" size="sm">
                      View books
                    </Button>
                  </Link>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => handleEditUrl(s.id, s.check_url)}
                  >
                    Edit URL
                  </Button>
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
        Showing {sortedSeries.length} of {series.length} series.
      </p>
    </div>
  );
}
