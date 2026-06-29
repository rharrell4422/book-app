"use client";

import { useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
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

const SUGGESTION_CACHE_PREFIX = "series-suggestions-v1:";
const SUGGESTION_SCAN_PREFIX = "series-scan-v1:";
const SUGGESTION_AUTOSTART_PREFIX = "series-scan-autostarted-v1:";
const SUGGESTION_SERP_USAGE_PREFIX = "series-serp-usage-v1:";
const SUGGESTION_STORE_ONLY_PREFIX = "series-store-only-v1:";

type ScanStatus = "idle" | "running" | "paused" | "completed";

type ScanProgress = {
  status: ScanStatus;
  pendingOrders: string[];
  completedCount: number;
  totalCount: number;
};

function loadCachedSuggestions(seriesId: string): Record<string, any[]> {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.sessionStorage.getItem(`${SUGGESTION_CACHE_PREFIX}${seriesId}`);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function saveCachedSuggestions(seriesId: string, suggestions: Record<string, any[]>) {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(`${SUGGESTION_CACHE_PREFIX}${seriesId}`, JSON.stringify(suggestions));
  } catch {
    // Ignore storage errors in private mode or restricted browsers.
  }
}

function loadScanProgress(seriesId: string): ScanProgress | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(`${SUGGESTION_SCAN_PREFIX}${seriesId}`);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    return {
      status: parsed.status || "idle",
      pendingOrders: Array.isArray(parsed.pendingOrders) ? parsed.pendingOrders : [],
      completedCount: Number.isFinite(parsed.completedCount) ? parsed.completedCount : 0,
      totalCount: Number.isFinite(parsed.totalCount) ? parsed.totalCount : 0,
    };
  } catch {
    return null;
  }
}

function saveScanProgress(seriesId: string, progress: ScanProgress) {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(`${SUGGESTION_SCAN_PREFIX}${seriesId}`, JSON.stringify(progress));
  } catch {
    // Ignore storage errors in private mode or restricted browsers.
  }
}

function hasAutoStartedSeriesScan(seriesId: string): boolean {
  if (typeof window === "undefined") return false;
  return window.sessionStorage.getItem(`${SUGGESTION_AUTOSTART_PREFIX}${seriesId}`) === "1";
}

function markAutoStartedSeriesScan(seriesId: string) {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(`${SUGGESTION_AUTOSTART_PREFIX}${seriesId}`, "1");
  } catch {
    // Ignore storage errors in private mode or restricted browsers.
  }
}

function loadSerpUsageCount(seriesId: string): number {
  if (typeof window === "undefined") return 0;
  try {
    const raw = window.sessionStorage.getItem(`${SUGGESTION_SERP_USAGE_PREFIX}${seriesId}`);
    if (!raw) return 0;
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? Math.max(0, parsed) : 0;
  } catch {
    return 0;
  }
}

function saveSerpUsageCount(seriesId: string, count: number) {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(`${SUGGESTION_SERP_USAGE_PREFIX}${seriesId}`, String(Math.max(0, count)));
  } catch {
    // Ignore storage errors in private mode or restricted browsers.
  }
}

function formatDate(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleDateString();
}

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

function getBookDate(book: any) {
  const status = getBookStatus(book);
  return status === "upcoming" ? book.release_date || book.read_date : book.read_date || book.release_date;
}

function getStatusChipClass(status: string) {
  if (status === "read") {
    return "inline-flex rounded-full border border-emerald-300 bg-emerald-100 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-emerald-800";
  }
  return "inline-flex rounded-full border border-rose-300 bg-rose-100 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-rose-800";
}

function getSuggestionSourceQuality(suggestion: any): { label: string; className: string } {
  const sourceUrl = String(suggestion?.source_url || "").toLowerCase();
  const source = String(suggestion?.source || "").toLowerCase();

  if (sourceUrl.includes("amazon.") || sourceUrl.includes("audible.") || sourceUrl.includes("kindle")) {
    return {
      label: "store",
      className: "bg-emerald-100 text-emerald-800 border-emerald-200",
    };
  }

  if (sourceUrl.includes("openlibrary.org") || sourceUrl.includes("goodreads.")) {
    return {
      label: "catalog",
      className: "bg-sky-100 text-sky-800 border-sky-200",
    };
  }

  if (sourceUrl.includes("reddit.") || sourceUrl.includes("facebook.") || sourceUrl.includes("x.com") || sourceUrl.includes("twitter.")) {
    return {
      label: "community",
      className: "bg-amber-100 text-amber-800 border-amber-200",
    };
  }

  if (source === "serpapi") {
    return {
      label: "web",
      className: "bg-violet-100 text-violet-800 border-violet-200",
    };
  }

  if (sourceUrl) {
    return {
      label: "author-site",
      className: "bg-zinc-100 text-zinc-800 border-zinc-200",
    };
  }

  return {
    label: "other",
    className: "bg-slate-100 text-slate-700 border-slate-200",
  };
}

function isStoreSuggestion(suggestion: any): boolean {
  return getSuggestionSourceQuality(suggestion).label === "store";
}

function sortSuggestionsStoreFirst(suggestions: any[]): any[] {
  return [...suggestions].sort((a, b) => {
    const aStore = isStoreSuggestion(a) ? 1 : 0;
    const bStore = isStoreSuggestion(b) ? 1 : 0;
    return bStore - aStore;
  });
}

function normalizeSuggestedTitle(rawTitle: string, fallbackBookNumber?: string): string {
  let title = String(rawTitle || "").trim();
  if (!title && fallbackBookNumber) {
    return `Book ${fallbackBookNumber}`;
  }

  // Remove common storefront/media suffix noise.
  title = title.replace(/\s+ebook\s*$/i, "");
  title = title.replace(/\s*\(unabridged\)\s*$/i, "");
  title = title.replace(/\s*\([^)]*\bbook\s*\d+[^)]*\)\s*$/i, "");
  title = title.replace(/:\s*unbound\s*,?\s*book\s*\d+.*$/i, "");
  title = title.replace(/:\s*book\s*\d+.*$/i, "");
  title = title.replace(/\s*,\s*book\s*\d+.*$/i, "");

  // Trim review/blog attribution tails: " - Author Name".
  if (/\s-\s/i.test(title) && /\bby\b/i.test(title)) {
    title = title.split(/\s-\s/i)[0].trim();
  }

  return title || (fallbackBookNumber ? `Book ${fallbackBookNumber}` : "Untitled");
}

function inferSeriesTitleSuffix(books: any[]): string | null {
  const suffixCounts: Record<string, number> = {};
  const suffixDisplay: Record<string, string> = {};

  for (const book of books || []) {
    const title = String(book?.title || "").trim();
    const match = title.match(/^([^:]+):\s*(.+)$/);
    if (!match) continue;

    let suffix = match[2].trim();
    suffix = suffix.replace(/\s*\([^)]*\bbook\s*\d+[^)]*\)\s*$/i, "").trim();
    if (!suffix) continue;

    const key = suffix.toLowerCase();
    suffixCounts[key] = (suffixCounts[key] || 0) + 1;
    if (!suffixDisplay[key]) {
      suffixDisplay[key] = suffix;
    }
  }

  const ranked = Object.entries(suffixCounts).sort((a, b) => b[1] - a[1]);
  if (!ranked.length) return null;

  const [bestKey, bestCount] = ranked[0];
  if (bestCount < 2) return null;

  return suffixDisplay[bestKey] || null;
}

function inferSingleWordStemPreference(books: any[]): boolean {
  const stems: string[] = [];
  for (const book of books || []) {
    const title = String(book?.title || "").trim();
    if (!title) continue;
    const stem = title.split(":")[0].trim();
    if (stem) stems.push(stem);
  }
  if (stems.length < 3) {
    return false;
  }

  const singleWordCount = stems.filter((stem) => stem.split(/\s+/).length === 1).length;
  return singleWordCount / stems.length >= 0.7;
}

function canonicalizeSuggestionTitle(
  rawTitle: string,
  fallbackBookNumber: string | undefined,
  books: any[],
  seriesName?: string,
): string {
  let cleaned = normalizeSuggestedTitle(rawTitle, fallbackBookNumber);

  // Trim noisy tails from review/blog style titles.
  cleaned = cleaned.replace(/\s*-\s*unbound\s*#\d+.*$/i, "").trim();
  cleaned = cleaned.replace(/\s*\|\s*book\s*\d+.*$/i, "").trim();
  cleaned = cleaned.replace(/\s*\(unbound\s*book\s*\d+\)\s*$/i, "").trim();
  cleaned = cleaned.replace(/\s+one\s+city\s+saved.*$/i, "").trim();

  const suffix = inferSeriesTitleSuffix(books);
  const preferSingleWordStem = inferSingleWordStemPreference(books);
  if (preferSingleWordStem && !cleaned.includes(":")) {
    const firstWord = cleaned.split(/\s+/)[0]?.trim();
    if (firstWord) {
      cleaned = firstWord;
    }
  }

  if (suffix && !cleaned.includes(":")) {
    cleaned = `${cleaned}: ${suffix}`;
  }

  const hasBookTag = /\([^)]*\bbook\s*\d+[^)]*\)/i.test(cleaned);
  const bookNumber = fallbackBookNumber ? String(fallbackBookNumber).trim() : "";
  const safeSeriesName = String(seriesName || "").trim();
  if (!hasBookTag && bookNumber && safeSeriesName) {
    cleaned = `${cleaned} (${safeSeriesName} Book ${bookNumber})`;
  }

  return cleaned;
}

function sortBooksBySeriesOrder(books: any[]): any[] {
  return [...books].sort((a, b) => {
    const aNum = Number(a?.book_number ?? a?.series_order ?? 0);
    const bNum = Number(b?.book_number ?? b?.series_order ?? 0);
    const aVal = Number.isFinite(aNum) ? aNum : 0;
    const bVal = Number.isFinite(bNum) ? bNum : 0;
    return aVal - bVal;
  });
}

function loadStoreOnlyPreference(seriesId: string): boolean {
  if (typeof window === "undefined") return false;
  return window.sessionStorage.getItem(`${SUGGESTION_STORE_ONLY_PREFIX}${seriesId}`) === "1";
}

function saveStoreOnlyPreference(seriesId: string, value: boolean) {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(`${SUGGESTION_STORE_ONLY_PREFIX}${seriesId}`, value ? "1" : "0");
  } catch {
    // Ignore storage errors in private mode or restricted browsers.
  }
}

export default function SeriesDetailPage() {
  const params = useParams();
  const seriesId = params.seriesId as string;
  const [series, setSeries] = useState<any | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [summaryLoadingId, setSummaryLoadingId] = useState<number | null>(null);
  const [missingSuggestions, setMissingSuggestions] = useState<Record<string, any[]>>({});
  const [missingSuggestionLoading, setMissingSuggestionLoading] = useState<string | null>(null);
  const [quickSuggestResults, setQuickSuggestResults] = useState<any[]>([]);
  const [quickSuggestLoading, setQuickSuggestLoading] = useState(false);
  const [storeOnly, setStoreOnly] = useState(false);
  const [scanStatus, setScanStatus] = useState<ScanStatus>("idle");
  const [scanCompletedCount, setScanCompletedCount] = useState(0);
  const [scanTotalCount, setScanTotalCount] = useState(0);
  const [scanCurrentOrder, setScanCurrentOrder] = useState<string | null>(null);
  const [serpUsageCount, setSerpUsageCount] = useState(0);
  const [recentAddMessage, setRecentAddMessage] = useState<string | null>(null);
  const scanAbortRef = useRef<AbortController | null>(null);
  const scanPendingRef = useRef<string[]>([]);
  const scanCompletedRef = useRef(0);
  const scanTotalRef = useRef(0);
  const addMessageTimeoutRef = useRef<number | null>(null);

  useEffect(() => {
    let isActive = true;
    const seriesController = new AbortController();

    async function fetchSeries() {
      setLoading(true);
      setError(null);

      try {
        const response = await fetch(`http://localhost:8000/series/${seriesId}`, {
          cache: "no-store",
          signal: seriesController.signal,
        });

        if (!response.ok) {
          throw new Error(`Failed to load series (${response.status})`);
        }

        const data = await response.json();
        if (!isActive) return;
        setSeries(data);

        const cachedSuggestions = loadCachedSuggestions(seriesId);
        if (Object.keys(cachedSuggestions).length > 0) {
          setMissingSuggestions(cachedSuggestions);
        }

        setStoreOnly(loadStoreOnlyPreference(seriesId));

        setSerpUsageCount(loadSerpUsageCount(seriesId));

        const allMissingOrders: string[] = Array.isArray(data.missing_books) ? data.missing_books : [];
        const pendingFromCache = allMissingOrders.filter((order: string) => !(order in cachedSuggestions));
        const defaultCompleted = Math.max(0, allMissingOrders.length - pendingFromCache.length);

        const existingProgress = loadScanProgress(seriesId);
        if (existingProgress && existingProgress.totalCount > 0) {
          const validPending = existingProgress.pendingOrders.filter((order) => allMissingOrders.includes(order));
          const validCompleted = Math.max(0, existingProgress.totalCount - validPending.length);
          const total = allMissingOrders.length;

          scanPendingRef.current = validPending;
          scanCompletedRef.current = Math.min(validCompleted, total);
          scanTotalRef.current = total;

          setScanStatus(existingProgress.status);
          setScanCompletedCount(scanCompletedRef.current);
          setScanTotalCount(total);

          saveScanProgress(seriesId, {
            status: existingProgress.status,
            pendingOrders: validPending,
            completedCount: scanCompletedRef.current,
            totalCount: total,
          });

          if (existingProgress.status === "running" && validPending.length > 0) {
            runBackgroundScan(validPending, data, seriesId, cachedSuggestions);
          }
        } else {
          scanPendingRef.current = pendingFromCache;
          scanCompletedRef.current = defaultCompleted;
          scanTotalRef.current = allMissingOrders.length;

          setScanStatus(allMissingOrders.length > 0 && pendingFromCache.length === 0 ? "completed" : "idle");
          setScanCompletedCount(defaultCompleted);
          setScanTotalCount(allMissingOrders.length);

          saveScanProgress(seriesId, {
            status: allMissingOrders.length > 0 && pendingFromCache.length === 0 ? "completed" : "idle",
            pendingOrders: pendingFromCache,
            completedCount: defaultCompleted,
            totalCount: allMissingOrders.length,
          });

          // Auto-start only once per series (first ever load for this series in session).
          const shouldAutoStart =
            allMissingOrders.length > 0 &&
            pendingFromCache.length > 0 &&
            !hasAutoStartedSeriesScan(seriesId);

          if (shouldAutoStart) {
            markAutoStartedSeriesScan(seriesId);
            runBackgroundScan(pendingFromCache, data, seriesId, cachedSuggestions);
          }
        }
      } catch (error) {
        if (!isActive) return;
        setError("Unable to load this series right now.");
        console.error("Error fetching series:", error);
      } finally {
        if (isActive) {
          setLoading(false);
        }
      }
    }

    if (seriesId) {
      fetchSeries();
    }

    return () => {
      isActive = false;
      seriesController.abort();
      if (scanAbortRef.current) {
        scanAbortRef.current.abort();
        scanAbortRef.current = null;
      }
      if (addMessageTimeoutRef.current !== null) {
        window.clearTimeout(addMessageTimeoutRef.current);
      }
    };
  }, [seriesId]);

  useEffect(() => {
    const unsubscribe = subscribeBookStatusUpdates((payload) => {
      setSeries((prev: any) => {
        if (!prev || !Array.isArray(prev.books)) return prev;

        let didChange = false;
        const nextBooks = prev.books.map((book: any) => {
          if (book.id !== payload.id) return book;
          didChange = true;
          return {
            ...book,
            is_read: payload.is_read,
            read_status: payload.read_status,
            read_date: payload.read_date,
            release_date: payload.release_date,
            publication_date: payload.publication_date,
          };
        });

        return didChange ? { ...prev, books: nextBooks } : prev;
      });
    });

    return unsubscribe;
  }, []);

  async function runBackgroundScan(
    orders: string[],
    seriesData: any,
    targetSeriesId: string,
    seedSuggestions?: Record<string, any[]>
  ) {
    if (scanAbortRef.current || orders.length === 0) {
      return;
    }

    const scanController = new AbortController();
    scanAbortRef.current = scanController;
    scanPendingRef.current = [...orders];
    setScanStatus("running");

    const nextSuggestions: Record<string, any[]> = {
      ...(seedSuggestions || loadCachedSuggestions(targetSeriesId)),
    };

    saveScanProgress(targetSeriesId, {
      status: "running",
      pendingOrders: [...scanPendingRef.current],
      completedCount: scanCompletedRef.current,
      totalCount: scanTotalRef.current,
    });

    try {
      while (scanPendingRef.current.length > 0 && !scanController.signal.aborted) {
        const order = scanPendingRef.current[0];
        setScanCurrentOrder(order);

        let results = await fetchSuggestionForMissingBook(order, seriesData, scanController.signal);
        if (scanController.signal.aborted) {
          break;
        }
        if (results === null) {
          // Aborted requests should remain pending for resume.
          break;
        }

        if (results.length === 0) {
          // Retry once for transient misses before caching an empty result.
          const retryResults = await fetchSuggestionForMissingBook(order, seriesData, scanController.signal);
          if (retryResults === null) {
            break;
          }
          results = retryResults;
        }

        nextSuggestions[order] = results;
        scanPendingRef.current = scanPendingRef.current.slice(1);
        scanCompletedRef.current += 1;

        setMissingSuggestions((prev) => ({
          ...prev,
          [order]: results,
        }));
        setScanCompletedCount(scanCompletedRef.current);

        saveCachedSuggestions(targetSeriesId, nextSuggestions);
        saveScanProgress(targetSeriesId, {
          status: "running",
          pendingOrders: [...scanPendingRef.current],
          completedCount: scanCompletedRef.current,
          totalCount: scanTotalRef.current,
        });
      }

      const finished = scanPendingRef.current.length === 0 && !scanController.signal.aborted;
      const nextStatus: ScanStatus = finished ? "completed" : "paused";
      setScanStatus(nextStatus);
      setScanCurrentOrder(null);

      saveScanProgress(targetSeriesId, {
        status: nextStatus,
        pendingOrders: [...scanPendingRef.current],
        completedCount: scanCompletedRef.current,
        totalCount: scanTotalRef.current,
      });
    } finally {
      scanAbortRef.current = null;
    }
  }

  if (loading) {
    return <div className="p-6">Loading series...</div>;
  }

  if (error) {
    return <div className="p-6 text-red-600">{error}</div>;
  }

  if (!series) {
    return <div className="p-6">Series not found.</div>;
  }

  const books: any[] = Array.isArray(series.books) ? series.books : [];
  const missingOrders: string[] = Array.isArray(series.missing_books)
    ? series.missing_books
    : [];
  const totalBooks = series.total_books ?? books.length;
  const readCount = books.filter((book) => book.is_read).length;
  const upcomingCount = books.filter((book) => getBookStatus(book) === "upcoming").length;
  const unreadCount = books.filter((book) => !book.is_read).length;
  const displayAuthor = series.author || books.find((book) => book.author)?.author || "Unknown author";
  const maxBookNumber = books.reduce((max: number, book: any) => {
    const num = Number(book.book_number);
    return Number.isFinite(num) ? Math.max(max, num) : max;
  }, 0);
  const suggestedNextNumber = String(Math.max(1, Math.floor(maxBookNumber) + 1));
  const scanPercent = scanTotalCount > 0 ? Math.min(100, Math.round((scanCompletedCount / scanTotalCount) * 100)) : 0;
  const autoStartedOnce = hasAutoStartedSeriesScan(seriesId);
  const quickSortedSuggestions = sortSuggestionsStoreFirst(quickSuggestResults);
  const quickVisibleSuggestions = storeOnly
    ? quickSortedSuggestions.filter(isStoreSuggestion)
    : quickSortedSuggestions;

  function setStoreOnlyAndPersist(value: boolean) {
    setStoreOnly(value);
    saveStoreOnlyPreference(seriesId, value);
  }

  function incrementSerpUsage(byCount: number) {
    if (!Number.isFinite(byCount) || byCount <= 0) {
      return;
    }
    setSerpUsageCount((prev) => {
      const next = prev + byCount;
      saveSerpUsageCount(seriesId, next);
      return next;
    });
  }

  function flashAddedMessage(message: string) {
    setRecentAddMessage(message);
    if (addMessageTimeoutRef.current !== null) {
      window.clearTimeout(addMessageTimeoutRef.current);
    }
    addMessageTimeoutRef.current = window.setTimeout(() => {
      setRecentAddMessage(null);
      addMessageTimeoutRef.current = null;
    }, 5000);
  }

  function removeOrderFromScanTracking(bookNumber: string) {
    const target = String(bookNumber);
    const hadPending = scanPendingRef.current.some((order) => String(order) === target);
    scanPendingRef.current = scanPendingRef.current.filter((order) => String(order) !== target);

    if (scanTotalRef.current > 0) {
      scanTotalRef.current -= 1;
      setScanTotalCount(scanTotalRef.current);
    }

    if (hadPending) {
      scanCompletedRef.current = Math.min(scanCompletedRef.current + 1, scanTotalRef.current);
      setScanCompletedCount(scanCompletedRef.current);
    } else {
      scanCompletedRef.current = Math.min(scanCompletedRef.current, scanTotalRef.current);
      setScanCompletedCount(scanCompletedRef.current);
    }

    const nextStatus: ScanStatus = scanPendingRef.current.length === 0 ? "completed" : scanStatus;
    if (nextStatus !== scanStatus) {
      setScanStatus(nextStatus);
    }

    saveScanProgress(seriesId, {
      status: nextStatus,
      pendingOrders: [...scanPendingRef.current],
      completedCount: scanCompletedRef.current,
      totalCount: scanTotalRef.current,
    });
  }

  function buildGoodreadsSearchUrl(query: string) {
    const encoded = encodeURIComponent(query);
    return `https://www.goodreads.com/search?q=${encoded}`;
  }

  function handleOpenSearch(query: string) {
    window.open(buildGoodreadsSearchUrl(query), "_blank");
  }

  function buildGoogleSearchUrl(query: string) {
    const encoded = encodeURIComponent(query);
    return `https://www.google.com/search?q=${encoded}`;
  }

  function handleOpenGoogleSearch(query: string) {
    window.open(buildGoogleSearchUrl(query), "_blank");
  }

  async function handleFetchSummary(bookId: number, title: string, author?: string | null) {
    setSummaryLoadingId(bookId);
    try {
      const response = await fetch(`http://localhost:8000/books/${bookId}/summary`, {
        method: "POST",
      });
      if (!response.ok) {
        throw new Error(`Failed to fetch summary (${response.status})`);
      }

      const data = await response.json();
      const updatedBook = data.book;
      setSeries((prev: any) => ({
        ...prev,
        books: prev.books.map((book: any) =>
          book.id === updatedBook.id ? updatedBook : book
        ),
      }));
    } catch (err) {
      console.error(err);
      alert("Unable to fetch a summary for this book right now.");
    } finally {
      setSummaryLoadingId(null);
    }
  }

  async function handleToggleRead(book: any) {
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

      if (!response.ok) {
        throw new Error(`Failed to update book (${response.status})`);
      }

      const updatedBook = await response.json();
      setSeries((prev: any) => {
        const prevBooks = Array.isArray(prev?.books) ? prev.books : [];
        return {
          ...prev,
          books: prevBooks.map((item: any) =>
            item.id === updatedBook.id ? { ...item, ...updatedBook } : item
          ),
        };
      });
      publishBookStatusUpdate(updatedBook);
    } catch (err) {
      console.error(err);
      alert("Unable to update read status right now.");
    }
  }

  async function fetchSuggestionForMissingBook(bookNumber: string, seriesData?: any, signal?: AbortSignal): Promise<any[] | null> {
    const seriesPayload = seriesData || series;
    if (!seriesPayload) {
      return [];
    }

    let timeoutId: number | null = null;

    try {
      const params = new URLSearchParams();
      params.set("series_name", seriesPayload.name || "");
      params.set("book_number", bookNumber);
      const seriesBooks = Array.isArray(seriesPayload.books) ? seriesPayload.books : [];
      const suggestAuthor = seriesPayload.author || seriesBooks.find((book) => book.author)?.author;
      if (suggestAuthor && !["unknown", "unknown author", "n/a", "na", "none"].includes(String(suggestAuthor).trim().toLowerCase())) {
        params.set("author", suggestAuthor);
      }

      const url = `http://localhost:8000/books/suggest?${params.toString()}`;
      console.log(`[Suggestion ${bookNumber}] Fetching from: ${url}`);

      const timeoutController = new AbortController();
      timeoutId = window.setTimeout(() => timeoutController.abort(), 90000);
      const combinedSignal = signal
        ? AbortSignal.any([signal, timeoutController.signal])
        : timeoutController.signal;

      const response = await fetch(url, { signal: combinedSignal });
      if (!response.ok) {
        throw new Error(`Failed to lookup suggestions (${response.status})`);
      }

      const responseData = await response.json();
      const serpUsed = Number(responseData?.diagnostics?.provider_counts?.serpapi || 0);
      incrementSerpUsage(serpUsed);
      console.log(`[Suggestion ${bookNumber}] Got ${responseData.results?.length || 0} results`);
      return responseData.results || [];
    } catch (err) {
      if ((err as Error)?.name === "AbortError") {
        return null;
      }
      console.error(`[Suggestion ${bookNumber}] Error:`, err);
      return [];
    } finally {
      if (timeoutId !== null) {
        window.clearTimeout(timeoutId);
      }
    }
  }

  async function handleSuggestMissingBook(bookNumber: string) {
    setMissingSuggestionLoading(bookNumber);
    try {
      const results = await fetchSuggestionForMissingBook(bookNumber);
      if (results === null) {
        return;
      }
      setMissingSuggestions((prev) => {
        const next = {
          ...prev,
          [bookNumber]: results,
        };
        saveCachedSuggestions(seriesId, next);
        return next;
      });
    } catch (err) {
      console.error(err);
      alert("Unable to suggest a title for this missing book right now.");
    } finally {
      setMissingSuggestionLoading(null);
    }
  }

  function handleStartFullScan() {
    if (!series) return;
    const allMissingOrders: string[] = Array.isArray(series.missing_books) ? series.missing_books : [];
    const cached = loadCachedSuggestions(seriesId);
    const pendingOrders = allMissingOrders.filter((order: string) => !(order in cached));

    scanPendingRef.current = pendingOrders;
    scanTotalRef.current = allMissingOrders.length;
    scanCompletedRef.current = Math.max(0, allMissingOrders.length - pendingOrders.length);

    setScanCompletedCount(scanCompletedRef.current);
    setScanTotalCount(scanTotalRef.current);

    if (pendingOrders.length === 0) {
      setScanStatus("completed");
      setScanCurrentOrder(null);
      saveScanProgress(seriesId, {
        status: "completed",
        pendingOrders: [],
        completedCount: scanCompletedRef.current,
        totalCount: scanTotalRef.current,
      });
      return;
    }

    runBackgroundScan(pendingOrders, series, seriesId, cached);
  }

  function handlePauseScan() {
    if (scanAbortRef.current) {
      scanAbortRef.current.abort();
      scanAbortRef.current = null;
    }

    setScanStatus("paused");
    setScanCurrentOrder(null);
    saveScanProgress(seriesId, {
      status: "paused",
      pendingOrders: [...scanPendingRef.current],
      completedCount: scanCompletedRef.current,
      totalCount: scanTotalRef.current,
    });
  }

  function handleResumeScan() {
    if (!series || scanPendingRef.current.length === 0) {
      return;
    }
    runBackgroundScan(scanPendingRef.current, series, seriesId, loadCachedSuggestions(seriesId));
  }

  function handleResetScanProgress() {
    if (scanAbortRef.current) {
      scanAbortRef.current.abort();
      scanAbortRef.current = null;
    }

    window.sessionStorage.removeItem(`${SUGGESTION_CACHE_PREFIX}${seriesId}`);
    window.sessionStorage.removeItem(`${SUGGESTION_SCAN_PREFIX}${seriesId}`);
    window.sessionStorage.removeItem(`${SUGGESTION_SERP_USAGE_PREFIX}${seriesId}`);

    const allMissingOrders: string[] = Array.isArray(series?.missing_books) ? series.missing_books : [];
    scanPendingRef.current = [...allMissingOrders];
    scanCompletedRef.current = 0;
    scanTotalRef.current = allMissingOrders.length;

    setMissingSuggestions({});
    setScanStatus("idle");
    setScanCurrentOrder(null);
    setScanCompletedCount(0);
    setScanTotalCount(allMissingOrders.length);
    setSerpUsageCount(0);
  }

  async function handleSuggestNextBook() {
    setQuickSuggestLoading(true);
    try {
      const results = await fetchSuggestionForMissingBook(suggestedNextNumber);
      if (results === null) {
        return;
      }
      setQuickSuggestResults(results);
    } catch (err) {
      console.error(err);
      alert("Unable to suggest a title right now.");
    } finally {
      setQuickSuggestLoading(false);
    }
  }

  async function handleAddSuggestion(bookNumber: string, suggestion: any) {
    try {
      // Reduce write contention while adding a book by pausing active scans.
      if (scanAbortRef.current) {
        scanAbortRef.current.abort();
        scanAbortRef.current = null;
        setScanStatus("paused");
      }

      const cleanedTitle = canonicalizeSuggestionTitle(
        suggestion.title,
        bookNumber,
        series?.books || [],
        series?.name,
      );
      const editedTitle = prompt(`Confirm title for book ${bookNumber}:`, cleanedTitle);
      if (editedTitle === null) {
        return;
      }
      const finalTitle = editedTitle.trim() || cleanedTitle;

      const response = await fetch("http://localhost:8000/books/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: finalTitle,
          author: suggestion.author || series.author || "Unknown author",
          series_id: Number(series.id),
          series_order: Number(bookNumber),
          book_number: Number(bookNumber),
          read_status: "unread",
          is_read: false,
          publication_date: suggestion.year ? `${suggestion.year}-01-01` : undefined,
        }),
      });

      if (!response.ok) {
        let detail = "";
        try {
          const data = await response.json();
          detail = data?.detail ? ` - ${data.detail}` : "";
        } catch {
          // ignore parse errors and fall back to status only
        }
        throw new Error(`Failed to add suggested book (${response.status})${detail}`);
      }

      const newBook = await response.json();
      setSeries((prev: any) => ({
        ...prev,
        books: sortBooksBySeriesOrder([...(prev.books || []), newBook]),
        missing_books: Array.isArray(prev.missing_books)
          ? prev.missing_books.filter((order: string) => String(order) !== String(bookNumber))
          : prev.missing_books,
      }));
      removeOrderFromScanTracking(bookNumber);

      // After add, clear this slot's suggestion list to reduce visual clutter.
      setMissingSuggestions((prev) => {
        const next = { ...prev };
        delete next[bookNumber];
        saveCachedSuggestions(seriesId, next);
        return next;
      });

      flashAddedMessage(`Added book #${bookNumber}: ${finalTitle}`);
    } catch (err) {
      console.error(err);
      const message = err instanceof Error ? err.message : "Unable to add the suggested book.";
      alert(message);
    }
  }

  async function handleAddMissingBook(bookNumber: string) {
    const title = prompt(`Title for book ${bookNumber}:`, `Book ${bookNumber}`);
    if (!title) {
      return;
    }

    try {
      const response = await fetch("http://localhost:8000/books/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title,
          author: series.author || "Unknown author",
          series_id: Number(series.id),
          series_order: Number(bookNumber),
          book_number: Number(bookNumber),
          read_status: "unread",
          is_read: false,
        }),
      });

      if (!response.ok) {
        throw new Error(`Failed to add book ${bookNumber}`);
      }

      const updatedBook = await response.json();
      setSeries((prev: any) => ({
        ...prev,
        books: sortBooksBySeriesOrder([...(prev.books || []), updatedBook]),
        missing_books: Array.isArray(prev.missing_books)
          ? prev.missing_books.filter((order: string) => String(order) !== String(bookNumber))
          : prev.missing_books,
      }));
      removeOrderFromScanTracking(bookNumber);
      flashAddedMessage(`Added missing book #${bookNumber}.`);
    } catch (error) {
      console.error(error);
      alert("Could not add the missing book. Check the console for details.");
    }
  }

  return (
    <div className="p-6 space-y-6">
      <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
        <div className="space-y-3">
          <p className="text-sm uppercase tracking-[0.2em] text-muted-foreground">Series detail</p>
          <div>
            <h1 className="text-3xl font-bold">{series.name}</h1>
            <p className="text-sm text-muted-foreground">{series.author || "Unknown author"}</p>
          </div>
          {series.description && (
            <p className="max-w-3xl text-sm leading-6 text-muted-foreground">{series.description}</p>
          )}
        </div>

        <div className="flex flex-wrap gap-2">
          <Link href="/books">
            <Button variant="outline">Back to Library</Button>
          </Link>
          <Link href="/series">
            <Button variant="secondary">View all series</Button>
          </Link>
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-lg border bg-card/80 p-4">
          <p className="text-sm text-muted-foreground">Total books</p>
          <p className="text-2xl font-semibold">{totalBooks}</p>
        </div>
        <div className="rounded-lg border bg-card/80 p-4">
          <p className="text-sm text-muted-foreground">Read</p>
          <p className="text-2xl font-semibold">{readCount}</p>
        </div>
        <div className="rounded-lg border bg-card/80 p-4">
          <p className="text-sm text-muted-foreground">Unread</p>
          <p className="text-2xl font-semibold">{unreadCount}</p>
        </div>
        <div className="rounded-lg border bg-card/80 p-4">
          <p className="text-sm text-muted-foreground">Upcoming</p>
          <p className="text-2xl font-semibold">{upcomingCount}</p>
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 text-sm text-muted-foreground">
        <div>Series status: {series.series_status || "Unknown"}</div>
        <div>Next unread: {series.next_unread_book_number ?? "—"}</div>
        <div>Next upcoming: {series.next_upcoming_book_number ?? "—"}</div>
        <div>Total missing: {missingOrders.length}</div>
        <div>Serp calls (session, this series): {serpUsageCount}</div>
      </div>

      {recentAddMessage ? (
        <div className="fixed bottom-4 right-4 z-50 max-w-md rounded-md border-2 border-emerald-900 bg-emerald-800 px-3 py-2 text-sm font-semibold text-white shadow-2xl">
          {recentAddMessage}
        </div>
      ) : null}

      {missingOrders.length === 0 && (
        <div className="rounded-lg border bg-slate-50 p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-sm font-semibold">No missing slots detected</p>
              <p className="text-sm text-muted-foreground">
                You can still test suggestions for the next likely book number.
              </p>
            </div>
            <Button
              variant="secondary"
              size="sm"
              onClick={handleSuggestNextBook}
              disabled={quickSuggestLoading}
            >
              {quickSuggestLoading ? "Finding…" : `Suggest for book #${suggestedNextNumber}`}
            </Button>
          </div>

          <div className="mt-3 flex items-center justify-end">
            <label className="inline-flex items-center gap-2 text-xs text-muted-foreground">
              <input
                type="checkbox"
                checked={storeOnly}
                onChange={(event) => setStoreOnlyAndPersist(event.target.checked)}
              />
              Store only
            </label>
          </div>

          {quickVisibleSuggestions.length > 0 ? (
            <div className="mt-3 space-y-2 rounded border bg-white p-3 text-sm">
              {quickVisibleSuggestions.map((suggestion, idx) => (
                <div key={idx} className="space-y-1">
                  <div className="font-medium">{suggestion.title}</div>
                  <div className="text-xs text-muted-foreground">
                    {suggestion.author || "Unknown author"}
                    {suggestion.year ? ` • ${suggestion.year}` : ""}
                  </div>
                  {(() => {
                    const quality = getSuggestionSourceQuality(suggestion);
                    return (
                      <span
                        className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide ${quality.className}`}
                      >
                        {quality.label}
                      </span>
                    );
                  })()}
                  <div className="flex flex-wrap gap-2">
                    <Button
                      variant="outline"
                      size="xs"
                      onClick={() => handleAddSuggestion(suggestedNextNumber, suggestion)}
                    >
                      Add suggestion
                    </Button>
                    {suggestion.source_url ? (
                      <a
                        href={suggestion.source_url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-xs text-blue-600 underline"
                      >
                        View source
                      </a>
                    ) : null}
                  </div>
                </div>
              ))}
            </div>
          ) : quickSuggestResults.length > 0 && storeOnly ? (
            <p className="mt-3 text-sm text-muted-foreground">
              No store results for this slot. Turn off Store only to view all suggestions.
            </p>
          ) : null}
        </div>
      )}

      {missingOrders.length > 0 && (
        <div className="rounded-lg border border-yellow-200 bg-yellow-50 p-4">
          <div className="flex items-center justify-between gap-4">
            <div>
              <p className="text-sm font-semibold text-yellow-900">Missing books detected</p>
              <p className="text-sm text-muted-foreground">
                These books are not in your library yet. Add them if you want to track them here.
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              {scanStatus !== "running" ? (
                <Button variant="secondary" size="sm" onClick={handleStartFullScan}>
                  {scanStatus === "paused" ? "Restart Full Scan" : "Run Full Scan"}
                </Button>
              ) : (
                <Button variant="secondary" size="sm" onClick={handlePauseScan}>
                  Pause Scan
                </Button>
              )}
              {scanStatus === "paused" && scanPendingRef.current.length > 0 ? (
                <Button variant="outline" size="sm" onClick={handleResumeScan}>
                  Resume Scan
                </Button>
              ) : null}
              <Button variant="ghost" size="sm" onClick={handleResetScanProgress}>
                Reset Cache
              </Button>
            </div>
          </div>

          <div className="mt-3 space-y-2">
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>
                Progress: {scanCompletedCount}/{scanTotalCount}
                {scanCurrentOrder ? ` • fetching #${scanCurrentOrder}` : ""}
              </span>
              <span>{scanPercent}%</span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-white/70">
              <div
                className="h-full bg-yellow-500 transition-all duration-300"
                style={{ width: `${scanPercent}%` }}
              />
            </div>
            <p className="text-xs text-muted-foreground">
              Scan status: {scanStatus}
            </p>
            <p className="text-xs text-muted-foreground">
              Auto-start: {autoStartedOnce ? "Auto-started once" : "Not auto-started yet"}
            </p>
            <div className="pt-1">
              <label className="inline-flex items-center gap-2 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  checked={storeOnly}
                  onChange={(event) => setStoreOnlyAndPersist(event.target.checked)}
                />
                Store only
              </label>
            </div>
          </div>

          <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {missingOrders.map((order) => (
              (() => {
                const orderSuggestions = missingSuggestions[order] || [];
                const sortedOrderSuggestions = sortSuggestionsStoreFirst(orderSuggestions);
                const visibleOrderSuggestions = storeOnly
                  ? sortedOrderSuggestions.filter(isStoreSuggestion)
                  : sortedOrderSuggestions;

                return (
              <div key={order} className="rounded-lg border bg-white p-3 shadow-sm">
                <p className="text-sm text-muted-foreground">Missing book</p>
                <p className="text-xl font-semibold">#{order}</p>
                <div className="flex flex-wrap gap-2">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => handleOpenSearch(`${series.name} ${order}`)}
                  >
                    Search Goodreads
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => handleOpenGoogleSearch(`${series.name} book ${order} ${series.author || ""}`.trim())}
                  >
                    Search Google
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => handleSuggestMissingBook(order)}
                    disabled={missingSuggestionLoading === order}
                  >
                    {missingSuggestionLoading === order ? "Finding…" : "Suggest title"}
                  </Button>
                </div>
                {visibleOrderSuggestions.length > 0 ? (
                  <div className="mt-3 space-y-2 rounded border bg-slate-50 p-3 text-sm">
                    {visibleOrderSuggestions.map((suggestion, idx) => (
                      <div key={idx} className="space-y-1">
                        <div className="font-medium">{suggestion.title}</div>
                        <div className="text-xs text-muted-foreground">
                          {suggestion.author || "Unknown author"}
                          {suggestion.year ? ` • ${suggestion.year}` : ""}
                        </div>
                        {(() => {
                          const quality = getSuggestionSourceQuality(suggestion);
                          return (
                            <span
                              className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide ${quality.className}`}
                            >
                              {quality.label}
                            </span>
                          );
                        })()}
                        <div className="flex flex-wrap gap-2">
                          <Button
                            variant="outline"
                            size="xs"
                            onClick={() => handleAddSuggestion(order, suggestion)}
                          >
                            Add suggestion
                          </Button>
                          {suggestion.source_url ? (
                            <a
                              href={suggestion.source_url}
                              target="_blank"
                              rel="noreferrer"
                              className="text-xs text-blue-600 underline"
                            >
                              View source
                            </a>
                          ) : null}
                        </div>
                      </div>
                    ))}
                  </div>
                ) : orderSuggestions.length > 0 && storeOnly ? (
                  <p className="mt-3 text-sm text-muted-foreground">
                    No store results for this slot. Turn off Store only to view all suggestions.
                  </p>
                ) : missingSuggestions[order] ? (
                  <p className="mt-3 text-sm text-muted-foreground">No suggestions found.</p>
                ) : null}
              </div>
                );
              })()
            ))}
          </div>
        </div>
      )}

      <div className="space-y-2">
        <p className="text-sm font-semibold uppercase tracking-wide text-emerald-800">Added To Library</p>
        <p className="text-xs text-muted-foreground">Books currently saved in this series.</p>
      </div>

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Title</TableHead>
            <TableHead>Author</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Date</TableHead>
            <TableHead>Book #</TableHead>
            <TableHead>Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {books.map((book) => {
            const status = getBookStatus(book);
            const displayDate = getBookDate(book);
            const summary = book.auto_summary;
            return (
              <TableRow key={book.id}>
                <TableCell>
                  <div>{book.title}</div>
                  {summary ? (
                    <p className="text-xs text-muted-foreground line-clamp-2">
                      {summary}
                    </p>
                  ) : null}
                </TableCell>
                <TableCell>{book.author || "—"}</TableCell>
                <TableCell>
                  <span className={getStatusChipClass(status)}>{status}</span>
                </TableCell>
                <TableCell>{formatDate(displayDate)}</TableCell>
                <TableCell>{book.book_number ?? "—"}</TableCell>
                <TableCell className="space-x-2 whitespace-nowrap">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      handleOpenSearch(
                        `${book.title} ${book.author || ""}`.trim()
                      )
                    }
                  >
                    Search
                  </Button>
                  <Button
                    variant="outline"
                    className={
                      book.is_read
                        ? "border-rose-300 text-rose-700 hover:bg-rose-50"
                        : "border-emerald-300 text-emerald-700 hover:bg-emerald-50"
                    }
                    size="sm"
                    onClick={() => handleToggleRead(book)}
                  >
                    {book.is_read ? "Book: mark unread" : "Book: mark read"}
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => handleFetchSummary(book.id, book.title, book.author)}
                    disabled={summaryLoadingId === book.id}
                  >
                    {summary ? "Refresh summary" : "Fetch summary"}
                  </Button>
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}
