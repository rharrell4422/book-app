"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import Spinner from "@/components/ui/spinner";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { publishBookStatusUpdate, subscribeBookStatusUpdates } from "@/lib/book-status-sync";
import { scheduleSeriesCheckReset } from "@/lib/series-check-progress";
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
const STATIC_API_BASE_CANDIDATES = [
  process.env.NEXT_PUBLIC_API_BASE_URL,
  "http://localhost:8000",
  "http://127.0.0.1:8000",
].filter(Boolean) as string[];

type ScanStatus = "idle" | "running" | "paused" | "completed";

type ScanProgress = {
  status: ScanStatus;
  pendingOrders: string[];
  completedCount: number;
  totalCount: number;
};

 type TitleNormalizationMode = "keep_original" | "clean_up" | "new_clean_title" | "match_other_titles";

type BookRecord = {
  id: number;
  title?: string | null;
  author?: string | null;
  read_status?: string | null;
  is_read?: boolean | null;
  read_date?: string | null;
  release_date?: string | null;
  publication_date?: string | null;
  book_number?: number | null;
  series_order?: number | null;
  auto_summary?: string | null;
  notes?: string | null;
  [key: string]: unknown;
};

type SuggestionRecord = {
  title?: string | null;
  author?: string | null;
  year?: string | number | null;
  source_url?: string | null;
  source?: string | null;
  [key: string]: unknown;
};

type SeriesRecord = {
  id: number;
  name: string;
  author?: string | null;
  description?: string | null;
  genre?: string | null;
  tags?: unknown;
  is_finished?: boolean;
  total_books?: number | null;
  series_status?: string | null;
  next_unread_book_number?: number | null;
  next_upcoming_book_number?: number | null;
  missing_books?: string[];
  title_normalization_mode_override?: TitleNormalizationMode | null;
  books?: BookRecord[];
  [key: string]: unknown;
};

type SeriesCheckStatusPayload = {
  session_id?: string | null;
  status: "idle" | "started" | "running" | "complete";
  progress?: number;
  current_pass?: string | null;
  elapsed_seconds?: number;
  timed_out?: boolean;
  missing_books?: Array<number | string>;
  no_new_books?: boolean;
  result?: {
    added_books?: unknown[];
    missing_books?: string[];
    discovery_mode?: string | null;
  };
  error?: string;
};

type SeriesDetailColumnKey = "title" | "author" | "status" | "date" | "bookNumber" | "actions";

const DEFAULT_SERIES_DETAIL_COLUMN_WIDTHS: Record<SeriesDetailColumnKey, number> = {
  title: 26,
  author: 16,
  status: 10,
  date: 10,
  bookNumber: 8,
  actions: 30,
};

const MIN_SERIES_DETAIL_COLUMN_WIDTHS: Record<SeriesDetailColumnKey, number> = {
  title: 12,
  author: 10,
  status: 8,
  date: 8,
  bookNumber: 6,
  actions: 15,
};

const SERIES_DETAIL_RESIZE_NEIGHBOR: Record<SeriesDetailColumnKey, SeriesDetailColumnKey | null> = {
  title: "author",
  author: "status",
  status: "date",
  date: "bookNumber",
  bookNumber: "actions",
  actions: null,
};

const SERIES_DETAIL_TABLE_COLUMN_WIDTHS_STORAGE_PREFIX = "seriesDetailTableColumnWidthsV1:";
const TITLE_NORMALIZATION_MODES: TitleNormalizationMode[] = ["keep_original", "clean_up", "new_clean_title", "match_other_titles"];

function isTitleNormalizationMode(value: unknown): value is TitleNormalizationMode {
  return typeof value === "string" && TITLE_NORMALIZATION_MODES.includes(value as TitleNormalizationMode);
}

function getTitleNormalizationModeLabel(mode: TitleNormalizationMode) {
  if (mode === "keep_original") return "Keep Original Title - Leave As Is";
  if (mode === "clean_up") return "Clean Up Title - Fix formatting junk";
  if (mode === "new_clean_title") return "New Clean Title - Keep book name, add clean series suffix";
  return "Match Other Titles - Format like the rest of the series";
}

function getTitleNormalizationModeDescription(mode: TitleNormalizationMode) {
  if (mode === "keep_original") return "Keeps the title exactly as imported.";
  if (mode === "clean_up") return "Removes junk formatting while keeping the official book title structure.";
  if (mode === "new_clean_title") return "Keeps the unique book title and adds (Series Name Book #).";
  return "Matches the formatting style used by other titles in this series.";
}

function normalizeBookTitleCleanupOnly(rawTitle: string): string {
  let title = String(rawTitle || "").trim();
  if (!title) return "";

  title = title.replace(/\s+ebook\s*$/i, "");
  title = title.replace(/\s+kindle\s+edition\s*$/i, "");
  title = title.replace(/\s*\(unabridged\)\s*$/i, "");
  title = title.replace(/:\s*/g, ": ");
  title = title.replace(/\(\s+/g, "(");
  title = title.replace(/\s+\)/g, ")");
  title = title.replace(/\s{2,}/g, " ");

  title = title.replace(/:\s*a\s+litrpg\s+apocalypse\s*:?$/i, ": A LitRPG").trim();
  title = title.replace(/:\s*a\s+litrpg\s+(?:adventure|novel|saga|epic|fantasy|progression\s+fantasy)\s*:?$/i, ": A LitRPG").trim();
  title = title.replace(/:\s*litrpg\s+(?:adventure|novel|saga|epic|fantasy|progression\s+fantasy)\s*:?$/i, ": LitRPG").trim();

  return title.replace(/\s{2,}/g, " ").trim();
}

function normalizeBookTitleCleanUp(rawTitle: string, seriesName?: string): string {
  let title = normalizeBookTitleCleanupOnly(rawTitle);
  if (!title) return "";

  title = title.replace(/:\s*:/g, ": ");

  const repeatedWrappedBookPattern = /^(.*?):\s*\((book\s+[^)]+)\)\s*:\s*\(([^)]*\bbook\s*\d+[^)]*)\)\s*$/i;
  const repeatedMatch = title.match(repeatedWrappedBookPattern);
  if (repeatedMatch) {
    const stem = String(repeatedMatch[1] || "").trim();
    const bookWord = String(repeatedMatch[2] || "").trim();
    const suffix = String(repeatedMatch[3] || "").trim();
    return `${stem}: ${bookWord} (${suffix})`.replace(/\s{2,}/g, " ").trim();
  }

  if (seriesName) {
    const escaped = escapeRegExp(String(seriesName).trim());
    title = title.replace(new RegExp(`^(${escaped})\s*:\s*${escaped}\s*`, "i"), "$1: ").trim();
  }

  return title;
}

function normalizeBookTitleBookNameOnly(rawTitle: string): string {
  const cleaned = normalizeBookTitleCleanupOnly(rawTitle);
  if (!cleaned) return "";

  const stripped = cleaned
    .replace(/\s*:\s*\([^)]*\)\s*$/i, "")
    .replace(/\s*:\s*.*$/i, "")
    .replace(/\s+[-–]\s+.*$/i, "")
    .trim();

  return stripped || cleaned;
}

function normalizeBookTitleSeriesNameOnly(rawTitle: string, seriesName?: string, bookNumber?: number | null): string {
  const cleaned = normalizeBookTitleCleanupOnly(rawTitle);
  if (!cleaned) return "";

  const inferredBookNumberMatch = cleaned.match(/\bbook\s+(\d+(?:\.\d+)?)\b/i);
  const resolvedBookNumber = Number.isFinite(bookNumber ?? NaN)
    ? Number(bookNumber)
    : inferredBookNumberMatch
      ? Number(inferredBookNumberMatch[1])
      : null;
  const cleanSeriesName = String(seriesName || "").trim();
  if (!cleanSeriesName) {
    return cleaned;
  }

  if (resolvedBookNumber === null) {
    return cleanSeriesName;
  }

  const prettyBookNumber = Number.isInteger(resolvedBookNumber)
    ? String(Math.trunc(resolvedBookNumber))
    : String(resolvedBookNumber);
  return `${cleanSeriesName} Book ${prettyBookNumber}`;
}

function normalizeBookTitleNewClean(rawTitle: string, seriesName?: string, bookNumber?: number | null): string {
  const cleaned = normalizeBookTitleCleanUp(rawTitle, seriesName);
  if (!cleaned) return "";

  const inferredBookNumberMatch = cleaned.match(/\bbook\s+(\d+(?:\.\d+)?)\b/i);
  const resolvedBookNumber = Number.isFinite(bookNumber ?? NaN)
    ? Number(bookNumber)
    : inferredBookNumberMatch
      ? Number(inferredBookNumberMatch[1])
      : null;
  const inferredSeriesNameMatch = cleaned.match(/\(\s*([^()]*?)\s+book\s*\d+(?:\.\d+)?\s*\)\s*$/i);
  const inferredSeriesName = inferredSeriesNameMatch ? String(inferredSeriesNameMatch[1] || "").trim() : "";
  const cleanSeriesName = String(seriesName || inferredSeriesName || "").trim();

  if (!cleanSeriesName || resolvedBookNumber === null) {
    return normalizeBookTitleBookNameOnly(cleaned);
  }

  const prettyBookNumber = Number.isInteger(resolvedBookNumber)
    ? String(Math.trunc(resolvedBookNumber))
    : String(resolvedBookNumber);
  const coreTitle = normalizeBookTitleBookNameOnly(cleaned);
  return `${coreTitle} (${cleanSeriesName} Book ${prettyBookNumber})`.replace(/\s{2,}/g, " ").trim();
}

function inferSeriesTitlePattern(books: BookRecord[]): "with_suffix" | "title_only" {
  let withSuffix = 0;
  let titleOnly = 0;

  for (const book of books || []) {
    const title = String(book?.title || "").trim();
    if (!title) continue;

    if (/\([^)]*\bbook\s*\d+(?:\.\d+)?[^)]*\)\s*$/i.test(title)) {
      withSuffix += 1;
    } else {
      titleOnly += 1;
    }
  }

  return withSuffix >= titleOnly ? "with_suffix" : "title_only";
}

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
  const isSuggestGetRequest = (init?.method || "GET").toUpperCase() === "GET" && /\/suggest(?:\?|$)/.test(normalizedPath);
  const requestInit: RequestInit = isSuggestGetRequest
    ? { ...init, cache: "no-store" }
    : init ?? {};
  const baseCandidates = getApiBaseCandidates();
  const candidates = [
    `/api${normalizedPath}`,
    ...baseCandidates.map((base) => `${normalizeBaseUrl(base)}${normalizedPath}`),
  ];

  if (normalizedPath.endsWith("/")) {
    const trimmedPath = normalizedPath.slice(0, -1);
    candidates.push(`/api${trimmedPath}`);
    candidates.push(...baseCandidates.map((base) => `${normalizeBaseUrl(base)}${trimmedPath}`));
  }

  let lastError: Error | null = null;
  for (const url of candidates) {
    try {
      const response = await fetch(url, requestInit);
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

function delay(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function loadCachedSuggestions(seriesId: string): Record<string, SuggestionRecord[]> {
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

function saveCachedSuggestions(seriesId: string, suggestions: Record<string, SuggestionRecord[]>) {
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

function normalizeDateInput(value: string | null | undefined): string | null {
  const raw = String(value || "").trim();
  if (!raw) return null;

  if (/^\d{4}-\d{1,2}-\d{1,2}$/.test(raw)) {
    return raw;
  }

  const mdyMatch = raw.match(/^(\d{1,2})[-\/](\d{1,2})[-\/](\d{4})$/);
  if (mdyMatch) {
    const month = mdyMatch[1].padStart(2, "0");
    const day = mdyMatch[2].padStart(2, "0");
    const year = mdyMatch[3];
    return `${year}-${month}-${day}`;
  }

  return raw;
}

function parseMonthNameDateToIso(value: string): string | null {
  const raw = String(value || "").trim();
  if (!raw) return null;

  const normalized = raw.replace(/\b(\d{1,2})(st|nd|rd|th)\b/gi, "$1");

  const parsed = new Date(normalized);
  if (Number.isNaN(parsed.valueOf())) {
    return null;
  }

  const year = parsed.getFullYear();
  const month = String(parsed.getMonth() + 1).padStart(2, "0");
  const day = String(parsed.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function parseReleaseIntelText(text: string): Array<{ bookNumber: number; title: string; releaseDate: string | null }> {
  const normalized = String(text || "").replace(/\s+/g, " ").trim();
  if (!normalized) return [];

  const extractReleaseDate = (value: string): string | null => {
    const monthNameMatch = value.match(
      /\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?(?:,)?\s+\d{4}\b/i
    );
    if (monthNameMatch) {
      return parseMonthNameDateToIso(monthNameMatch[0]);
    }

    const numericDateMatch = value.match(/\b\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}\b/);
    if (!numericDateMatch) {
      return null;
    }

    return normalizeDateInput(numericDateMatch[0]);
  };

  const matches = Array.from(
    normalized.matchAll(/Book\s*#?\s*(\d+(?:\.\d+)?)\b(?:\s*(?:\(([^)]+)\)|[:\-]\s*([^,.;|\n]+)))?/gi)
  );
  if (!matches.length) {
    const fallbackMatch = normalized.match(/(?:book|bk)\s*#?\s*(\d+(?:\.\d+)?)/i);
    if (!fallbackMatch) return [];

    const fallbackNumber = Number(fallbackMatch[1]);
    if (!Number.isFinite(fallbackNumber)) return [];

    return [
      {
        bookNumber: fallbackNumber,
        title: `Book ${fallbackNumber}`,
        releaseDate: extractReleaseDate(normalized),
      },
    ];
  }

  const parsed = matches
    .map((match, index) => {
      const num = Number(match[1]);
      if (!Number.isFinite(num)) return null;

      const title = String(match[2] || match[3] || "").trim() || `Book ${num}`;
      const start = match.index ?? 0;
      const end = (matches[index + 1]?.index ?? normalized.length);
      const localWindow = normalized.slice(start, end);
      const releaseDate = extractReleaseDate(localWindow);

      return {
        bookNumber: num,
        title,
        releaseDate,
      };
    })
    .filter((value): value is { bookNumber: number; title: string; releaseDate: string | null } => Boolean(value));

  const byBookNumber = new Map<number, { bookNumber: number; title: string; releaseDate: string | null }>();
  for (const entry of parsed) {
    byBookNumber.set(entry.bookNumber, entry);
  }

  return Array.from(byBookNumber.values());
}

function parseKnownSeriesListText(text: string): Array<{ bookNumber: number; title: string; publicationYear: number | null; note: string | null }> {
  const normalized = String(text || "").replace(/\s+/g, " ").trim();
  if (!normalized) return [];

  const entries = Array.from(
    normalized.matchAll(/(\d+(?:\.\d+)?)\s+(.+?)\s+\((\d{4})\)(?:\s+\(([^)]*)\))?(?=\s*\d+(?:\.\d+)?\s+|$)/g)
  );

  const parsed = entries
    .map((match) => {
      const bookNumber = Number(match[1]);
      if (!Number.isFinite(bookNumber)) return null;

      return {
        bookNumber,
        title: String(match[2] || "").trim(),
        publicationYear: Number(match[3]) || null,
        note: String(match[4] || "").trim() || null,
      };
    })
    .filter((value): value is { bookNumber: number; title: string; publicationYear: number | null; note: string | null } => Boolean(value));

  const deduped = new Map<number, { bookNumber: number; title: string; publicationYear: number | null; note: string | null }>();
  for (const entry of parsed) {
    deduped.set(entry.bookNumber, entry);
  }

  return Array.from(deduped.values()).sort((a, b) => a.bookNumber - b.bookNumber);
}

function hasUpcomingBookSignals(book: BookRecord) {
  const status = String(book.read_status || "").trim().toLowerCase();
  if (status === "upcoming" || status === "tbr" || status === "to be read") {
    return true;
  }

  if (book.is_read) {
    return false;
  }

  if (book.release_date || book.publication_date) {
    const parsedDate = new Date(book.release_date || book.publication_date || "");
    if (!Number.isNaN(parsedDate.valueOf())) {
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      parsedDate.setHours(0, 0, 0, 0);
      return parsedDate > today;
    }
  }

  return false;
}

function getBookStatus(book: BookRecord) {
  if (book.is_read) {
    return "read";
  }

  if (hasUpcomingBookSignals(book)) {
    return "upcoming";
  }

  return "unread";
}

function getBookDate(book: BookRecord) {
  const status = getBookStatus(book);
  return status === "upcoming" ? book.release_date || book.read_date : book.read_date || book.release_date;
}

function getStatusChipClass(status: string) {
  if (status === "read") {
    return "inline-flex rounded-full border border-emerald-300 bg-emerald-100 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-emerald-800";
  }
  return "inline-flex rounded-full border border-rose-300 bg-rose-100 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-rose-800";
}

function getSuggestionSourceQuality(suggestion: SuggestionRecord): { label: string; className: string } {
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

function isStoreSuggestion(suggestion: SuggestionRecord): boolean {
  return getSuggestionSourceQuality(suggestion).label === "store";
}

function sortSuggestionsStoreFirst(suggestions: SuggestionRecord[]): SuggestionRecord[] {
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

  // Some store results use "Series Name - Author List" as the title.
  if (/\s-\s/.test(title) && /,/.test(title) && !/\bby\b/i.test(title)) {
    const [left, right] = title.split(/\s-\s/, 2);
    const rightLooksLikeAuthorList = /^[A-Za-z.'\-\s,]+$/.test(right || "") && /,/.test(right || "");
    if (left?.trim() && rightLooksLikeAuthorList) {
      title = left.trim();
    }
  }

  // Trim review/blog attribution tails: " - Author Name".
  if (/\s-\s/i.test(title) && /\bby\b/i.test(title)) {
    title = title.split(/\s-\s/i)[0].trim();
  }

  return title || (fallbackBookNumber ? `Book ${fallbackBookNumber}` : "Untitled");
}

function escapeRegExp(value: string): string {
  return String(value || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function normalizeBookTitleBookNameSeries(rawTitle: string, seriesName?: string, bookNumber?: number | null): string {
  const title = normalizeBookTitleCleanupOnly(rawTitle);
  if (!title) return "";

  const inferredBookNumberMatch = title.match(/\bbook\s+(\d+(?:\.\d+)?)\b/i);
  const resolvedBookNumber = Number.isFinite(bookNumber ?? NaN)
    ? Number(bookNumber)
    : inferredBookNumberMatch
      ? Number(inferredBookNumberMatch[1])
      : null;
  const inferredSeriesNameMatch = title.match(/\(\s*([^()]*?)\s+book\s*\d+(?:\.\d+)?\s*\)\s*$/i);
  const inferredSeriesName = inferredSeriesNameMatch ? String(inferredSeriesNameMatch[1] || "").trim() : "";
  const cleanSeriesName = String(seriesName || inferredSeriesName || "").trim();

  if (!cleanSeriesName) {
    return title;
  }

  const escapedSeriesName = escapeRegExp(cleanSeriesName);
  let normalized = title
    .replace(new RegExp(`:\\s*${escapedSeriesName}\\s*,?\\s*book\\s*\\d+(?:\\.\\d+)?\\s*$`, "i"), "")
    .replace(new RegExp(`:\\s*${escapedSeriesName}\\s*$`, "i"), "")
    .trim();

  const genericBookStemMatch = normalized.match(/^book\s+(\d+(?:\.\d+)?)\s*:??\s*$/i);
  if (genericBookStemMatch) {
    const numberFromStem = Number(genericBookStemMatch[1]);
    const normalizedNumber = Number.isFinite(numberFromStem)
      ? numberFromStem
      : resolvedBookNumber;
    const prettyNumber = normalizedNumber !== null
      ? (Number.isInteger(normalizedNumber) ? String(Math.trunc(normalizedNumber)) : String(normalizedNumber))
      : "";
    normalized = prettyNumber ? `${cleanSeriesName} ${prettyNumber}` : cleanSeriesName;
  }

  normalized = normalized.replace(/\s*:\s*$/, "").trim();
  if (!normalized) {
    normalized = resolvedBookNumber !== null ? `Book ${resolvedBookNumber}` : "Untitled";
  }
  normalized = `${normalized}:`;

  if (resolvedBookNumber !== null) {
    const prettyBookNumber = Number.isInteger(resolvedBookNumber)
      ? String(Math.trunc(resolvedBookNumber))
      : String(resolvedBookNumber);
    normalized = `${normalized} (${cleanSeriesName} Book ${prettyBookNumber})`;
  }

  return normalized.trim();
}

function normalizeBookTitleForMode(
  rawTitle: string,
  mode: TitleNormalizationMode,
  seriesName?: string,
  bookNumber?: number | null,
  books: BookRecord[] = [],
): string {
  const raw = String(rawTitle || "").trim();
  if (!raw || mode === "keep_original") {
    return raw;
  }

  if (mode === "clean_up") {
    return normalizeBookTitleCleanUp(raw, seriesName);
  }

  if (mode === "new_clean_title") {
    return normalizeBookTitleNewClean(raw, seriesName, bookNumber);
  }

  const cleanTitle = normalizeBookTitleCleanUp(raw, seriesName);
  const seriesPattern = inferSeriesTitlePattern(books);
  if (seriesPattern === "title_only") {
    return normalizeBookTitleBookNameOnly(cleanTitle);
  }

  return normalizeBookTitleNewClean(cleanTitle, seriesName, bookNumber);
}

function inferSeriesTitleSuffix(books: BookRecord[]): string | null {
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

function inferSingleWordStemPreference(books: BookRecord[]): boolean {
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
  books: BookRecord[],
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

function sortBooksBySeriesOrder(books: BookRecord[]): BookRecord[] {
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
  const router = useRouter();
  const searchParams = useSearchParams();
  const seriesId = params.seriesId as string;
  const fromView = searchParams.get("fromView") === "finished" ? "finished" : "ongoing";
  const viewAllSeriesHref = `/series?view=${fromView}`;
  const [series, setSeries] = useState<SeriesRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [summaryLoadingId, setSummaryLoadingId] = useState<number | null>(null);
  const [finishedToggleSaving, setFinishedToggleSaving] = useState(false);
  const [summaryEditorBook, setSummaryEditorBook] = useState<BookRecord | null>(null);
  const [summaryDraft, setSummaryDraft] = useState("");
  const [notesDraft, setNotesDraft] = useState("");
  const [summarySaving, setSummarySaving] = useState(false);
  const [missingSuggestions, setMissingSuggestions] = useState<Record<string, SuggestionRecord[]>>({});
  const [missingSuggestionLoading, setMissingSuggestionLoading] = useState<string | null>(null);
  const [quickSuggestResults, setQuickSuggestResults] = useState<SuggestionRecord[]>([]);
  const [quickSuggestLoading, setQuickSuggestLoading] = useState(false);
  const [bookSortMode, setBookSortMode] = useState<"series" | "az">("series");
  const [storeOnly, setStoreOnly] = useState(false);
  const [showMissingFinderDetails, setShowMissingFinderDetails] = useState(false);
  const [scanStatus, setScanStatus] = useState<ScanStatus>("idle");
  const [scanCompletedCount, setScanCompletedCount] = useState(0);
  const [scanTotalCount, setScanTotalCount] = useState(0);
  const [scanCurrentOrder, setScanCurrentOrder] = useState<string | null>(null);
  const [serpUsageCount, setSerpUsageCount] = useState(0);
  const [recentAddMessage, setRecentAddMessage] = useState<string | null>(null);
  const [seriesCheckLoading, setSeriesCheckLoading] = useState(false);
  const [seriesCheckProgress, setSeriesCheckProgress] = useState(0);
  const [seriesCheckCurrentPass, setSeriesCheckCurrentPass] = useState<string | null>(null);
  const [seriesCheckStillChecking, setSeriesCheckStillChecking] = useState(false);
  const [releaseIntelText, setReleaseIntelText] = useState("");
  const [releaseIntelSaving, setReleaseIntelSaving] = useState(false);
  const [releaseIntelMessage, setReleaseIntelMessage] = useState<string | null>(null);
  const [addBookDialogOpen, setAddBookDialogOpen] = useState(false);
  const [addBookSaving, setAddBookSaving] = useState(false);
  const [addBookTitle, setAddBookTitle] = useState("");
  const [addBookNumber, setAddBookNumber] = useState("");
  const [addBookStatus, setAddBookStatus] = useState<"upcoming" | "unread" | "read">("upcoming");
  const [addBookDate, setAddBookDate] = useState("");
  const [recentUpcomingBookIds, setRecentUpcomingBookIds] = useState<number[]>([]);
  const [titleNormalizeSaving, setTitleNormalizeSaving] = useState(false);
  const [normalizeTitlesConfirmed, setNormalizeTitlesConfirmed] = useState(false);
  const [titleNormalizationExamplesOpen, setTitleNormalizationExamplesOpen] = useState(false);
  const [releaseIntelDialogOpen, setReleaseIntelDialogOpen] = useState(false);
  const [normalizeTitlesDialogOpen, setNormalizeTitlesDialogOpen] = useState(false);
  const [knownTotalDraft, setKnownTotalDraft] = useState("");
  const [knownTotalSaving, setKnownTotalSaving] = useState(false);
  const [knownSeriesListDialogOpen, setKnownSeriesListDialogOpen] = useState(false);
  const [knownSeriesListText, setKnownSeriesListText] = useState("");
  const [knownSeriesListSaving, setKnownSeriesListSaving] = useState(false);
  const [columnWidths, setColumnWidths] = useState<Record<SeriesDetailColumnKey, number>>(DEFAULT_SERIES_DETAIL_COLUMN_WIDTHS);
  const scanAbortRef = useRef<AbortController | null>(null);
  const scanPendingRef = useRef<string[]>([]);
  const scanCompletedRef = useRef(0);
  const scanTotalRef = useRef(0);
  const addMessageTimeoutRef = useRef<number | null>(null);
  const seriesCheckResetTimeoutRef = useRef<number | null>(null);
  const booksTableWrapRef = useRef<HTMLDivElement | null>(null);
  const resizeStateRef = useRef<{
    key: SeriesDetailColumnKey;
    neighborKey: SeriesDetailColumnKey;
    startX: number;
    startWidth: number;
    startNeighborWidth: number;
    containerWidth: number;
  } | null>(null);

  const seriesNormalizationMode = (series?.title_normalization_mode_override as TitleNormalizationMode | null | undefined) || "keep_original";

  function sanitizeSavedSeriesDetailColumnWidths(value: unknown): Record<SeriesDetailColumnKey, number> | null {
    if (!value || typeof value !== "object") return null;
    const candidate = value as Partial<Record<SeriesDetailColumnKey, unknown>>;
    const keys: SeriesDetailColumnKey[] = ["title", "author", "status", "date", "bookNumber", "actions"];
    const next: Partial<Record<SeriesDetailColumnKey, number>> = {};
    let hasAtLeastOneSavedKey = false;

    for (const key of keys) {
      const raw = candidate[key];
      if (typeof raw === "number" && Number.isFinite(raw)) {
        const minimum = MIN_SERIES_DETAIL_COLUMN_WIDTHS[key];
        next[key] = Math.max(minimum, Number(raw));
        hasAtLeastOneSavedKey = true;
      } else {
        next[key] = DEFAULT_SERIES_DETAIL_COLUMN_WIDTHS[key];
      }
    }

    if (!hasAtLeastOneSavedKey) return null;

    const total = keys.reduce((sum, key) => sum + (next[key] ?? 0), 0);
    if (total <= 0) return null;

    return {
      title: Number((((next.title ?? DEFAULT_SERIES_DETAIL_COLUMN_WIDTHS.title) / total) * 100).toFixed(2)),
      author: Number((((next.author ?? DEFAULT_SERIES_DETAIL_COLUMN_WIDTHS.author) / total) * 100).toFixed(2)),
      status: Number((((next.status ?? DEFAULT_SERIES_DETAIL_COLUMN_WIDTHS.status) / total) * 100).toFixed(2)),
      date: Number((((next.date ?? DEFAULT_SERIES_DETAIL_COLUMN_WIDTHS.date) / total) * 100).toFixed(2)),
      bookNumber: Number((((next.bookNumber ?? DEFAULT_SERIES_DETAIL_COLUMN_WIDTHS.bookNumber) / total) * 100).toFixed(2)),
      actions: Number((((next.actions ?? DEFAULT_SERIES_DETAIL_COLUMN_WIDTHS.actions) / total) * 100).toFixed(2)),
    };
  }

  useEffect(() => {
    const rafId = window.requestAnimationFrame(() => {
      try {
        const storageKey = `${SERIES_DETAIL_TABLE_COLUMN_WIDTHS_STORAGE_PREFIX}${seriesId}`;
        const saved = window.localStorage.getItem(storageKey);
        if (!saved) return;
        const parsed = JSON.parse(saved);
        const restored = sanitizeSavedSeriesDetailColumnWidths(parsed);
        if (restored) {
          setColumnWidths(restored);
        }
      } catch {
        // Ignore storage parse/read errors and keep defaults.
      }
    });

    return () => window.cancelAnimationFrame(rafId);
  }, [seriesId]);

  useEffect(() => {
    try {
      const storageKey = `${SERIES_DETAIL_TABLE_COLUMN_WIDTHS_STORAGE_PREFIX}${seriesId}`;
      window.localStorage.setItem(storageKey, JSON.stringify(columnWidths));
    } catch {
      // Ignore storage write errors.
    }
  }, [seriesId, columnWidths]);

  useEffect(() => {
    const handleMouseMove = (event: MouseEvent) => {
      const active = resizeStateRef.current;
      if (!active) return;

      const deltaX = event.clientX - active.startX;
      const deltaPercent = (deltaX / active.containerWidth) * 100;
      const minCurrent = MIN_SERIES_DETAIL_COLUMN_WIDTHS[active.key];
      const minNeighbor = MIN_SERIES_DETAIL_COLUMN_WIDTHS[active.neighborKey];
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

  function startColumnResize(key: SeriesDetailColumnKey, event: React.MouseEvent<HTMLButtonElement>) {
    const neighborKey = SERIES_DETAIL_RESIZE_NEIGHBOR[key];
    const containerWidth = booksTableWrapRef.current?.getBoundingClientRect().width ?? 0;
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

  useEffect(() => {
    let isActive = true;
    const seriesController = new AbortController();

    async function fetchSeries() {
      setLoading(true);
      setError(null);

      try {
        const response = await fetchApiWithFallback(`/series/${seriesId}`, {
          cache: "no-store",
          signal: seriesController.signal,
        });

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
  // runBackgroundScan is intentionally referenced from latest render while this effect is keyed by series changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seriesId]);

  useEffect(() => {
    const unsubscribe = subscribeBookStatusUpdates((payload) => {
      setSeries((prev) => {
        if (!prev || !Array.isArray(prev.books)) return prev;

        let didChange = false;
        const nextBooks = prev.books.map((book) => {
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

  useEffect(() => {
    if (!series) return;
    setKnownTotalDraft(series.total_books ? String(series.total_books) : "");
  }, [series?.id, series?.total_books]);

  async function runBackgroundScan(
    orders: string[],
    seriesData: SeriesRecord,
    targetSeriesId: string,
    seedSuggestions?: Record<string, SuggestionRecord[]>
  ) {
    if (scanAbortRef.current || orders.length === 0) {
      return;
    }

    const scanController = new AbortController();
    scanAbortRef.current = scanController;
    scanPendingRef.current = [...orders];
    setScanStatus("running");

    const nextSuggestions: Record<string, SuggestionRecord[]> = {
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

  const books = useMemo<BookRecord[]>(() => (Array.isArray(series?.books) ? series.books : []), [series?.books]);
  const activeRecentUpcomingBookIds = useMemo(
    () => recentUpcomingBookIds.filter((id) => books.some((book) => Number(book.id) === id && getBookStatus(book) === "upcoming")),
    [recentUpcomingBookIds, books],
  );

  useEffect(() => {
    return () => {
      clearSeriesCheckResetTimeout();
    };
  }, []);

  if (loading) {
    return <div className="p-6">Loading series...</div>;
  }

  if (error) {
    return <div className="p-6 text-red-600">{error}</div>;
  }

  if (!series) {
    return <div className="p-6">Series not found.</div>;
  }

  const displayedBooks = (() => {
    if (bookSortMode === "az") {
      return [...books].sort((a, b) =>
        String(a?.title || "").localeCompare(String(b?.title || ""), undefined, {
          sensitivity: "base",
        })
      );
    }

    const ordered = sortBooksBySeriesOrder(books);
    if (!activeRecentUpcomingBookIds.length) {
      return ordered;
    }

    const rankByPinnedOrder = new Map<number, number>();
    activeRecentUpcomingBookIds.forEach((id, index) => {
      rankByPinnedOrder.set(id, index);
    });

    const pinnedUpcoming = ordered
      .filter((book) => {
        const id = Number(book?.id);
        return rankByPinnedOrder.has(id) && getBookStatus(book) === "upcoming";
      })
      .sort((a, b) => {
        const aRank = rankByPinnedOrder.get(Number(a?.id)) ?? Number.MAX_SAFE_INTEGER;
        const bRank = rankByPinnedOrder.get(Number(b?.id)) ?? Number.MAX_SAFE_INTEGER;
        return aRank - bRank;
      });

    if (!pinnedUpcoming.length) {
      return ordered;
    }

    const pinnedIdSet = new Set(pinnedUpcoming.map((book) => Number(book?.id)));
    const rest = ordered.filter((book) => !pinnedIdSet.has(Number(book?.id)));
    return [...pinnedUpcoming, ...rest];
  })();
  const missingOrders: string[] = Array.isArray(series.missing_books)
    ? series.missing_books
    : [];
  const totalBooks = series.total_books ?? books.length;
  const readCount = books.filter((book) => book.is_read).length;
  const upcomingCount = books.filter((book) => getBookStatus(book) === "upcoming").length;
  const unreadCount = books.filter((book) => !book.is_read).length;
  const maxBookNumber = books.reduce((max: number, book) => {
    const num = Number(book.book_number);
    return Number.isFinite(num) ? Math.max(max, num) : max;
  }, 0);
  const suggestedNextNumber = String(Math.max(1, Math.floor(maxBookNumber) + 1));
  const scanPercent = scanTotalCount > 0 ? Math.min(100, Math.round((scanCompletedCount / scanTotalCount) * 100)) : 0;
  const quickSortedSuggestions = sortSuggestionsStoreFirst(quickSuggestResults);
  const quickVisibleSuggestions = storeOnly
    ? quickSortedSuggestions.filter(isStoreSuggestion)
    : quickSortedSuggestions;
  const titleNormalizationPreview = displayedBooks
    .map((book) => {
      const currentTitle = String(book?.title || "").trim();
      const normalizedTitle = normalizeBookTitleForMode(
        currentTitle,
        seriesNormalizationMode,
        series?.name,
        Number(book?.book_number ?? book?.series_order ?? NaN),
        Array.isArray(series?.books) ? series.books : [],
      );
      if (!currentTitle || !normalizedTitle || currentTitle === normalizedTitle) {
        return null;
      }
      return {
        id: Number(book.id),
        currentTitle,
        normalizedTitle,
      };
    })
    .filter(
      (value): value is { id: number; currentTitle: string; normalizedTitle: string } =>
        Boolean(value)
    );

  async function handleSaveTitleNormalizationOverride(nextMode: TitleNormalizationMode) {
    if (!series) return;

    try {
      const response = await fetchApiWithFallback(`/series/${series.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: series.name,
          author: series.author || undefined,
          description: series.description || undefined,
          genre: series.genre || undefined,
          tags: series.tags || undefined,
          total_books: series.total_books ?? undefined,
          series_status: series.series_status || undefined,
          next_unread_book_number: series.next_unread_book_number ?? undefined,
          next_upcoming_book_number: series.next_upcoming_book_number ?? undefined,
          missing_books: series.missing_books ?? undefined,
          is_finished: series.is_finished ?? false,
          title_normalization_mode_override: nextMode,
        }),
      });

      if (!response.ok) {
        throw new Error(`Failed to save series normalization override (${response.status})`);
      }

      await refreshSeriesFromApi();
      flashAddedMessage(
        `Series normalization mode set to ${getTitleNormalizationModeLabel(nextMode)}.`,
      );
    } catch (error) {
      console.error(error);
      alert(error instanceof Error ? error.message : "Unable to save normalization override.");
    }
  }


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

  function resetSeriesCheckUiState() {
    setSeriesCheckLoading(false);
    setSeriesCheckCurrentPass(null);
    setSeriesCheckProgress(0);
    setSeriesCheckStillChecking(false);
  }

  function clearSeriesCheckResetTimeout() {
    if (seriesCheckResetTimeoutRef.current !== null) {
      window.clearTimeout(seriesCheckResetTimeoutRef.current);
      seriesCheckResetTimeoutRef.current = null;
    }
  }

  async function refreshSeriesFromApi() {
    const response = await fetchApiWithFallback(`/series/${seriesId}`, {
      cache: "no-store",
    });
    const data = await response.json();
    setSeries(data);
  }

  async function handleCheckForNew() {
    if (!series) return;

    clearSeriesCheckResetTimeout();
    setSeriesCheckLoading(true);
    setSeriesCheckProgress(0);
    setSeriesCheckCurrentPass("exact match");
    setSeriesCheckStillChecking(false);
    flashAddedMessage(`Checking ${series.name} for new books...`);

    try {
      const response = await fetchApiWithFallback(`/series/${series.id}/check`, { method: "POST" });
      if (!response.ok) {
        throw new Error(`Unable to start check (${response.status})`);
      }

      const kickoff = await response.json() as SeriesCheckStatusPayload;
      const sessionId = kickoff.session_id;

      let statusPayload: SeriesCheckStatusPayload = {
        status: kickoff.status === "complete" ? "complete" : "running",
        progress: 0,
        current_pass: "exact match",
      };

      while (statusPayload.status === "running") {
        await delay(2500);
        const statusPath = sessionId
          ? `/series/${series.id}/check/status?session_id=${encodeURIComponent(sessionId)}`
          : `/series/${series.id}/check/status`;
        const statusResponse = await fetchApiWithFallback(statusPath, { cache: "no-store" });
        statusPayload = await statusResponse.json();

        setSeriesCheckProgress(Math.max(0, Math.min(100, Number(statusPayload.progress ?? 0))));
        setSeriesCheckCurrentPass(statusPayload.current_pass || null);
        setSeriesCheckStillChecking(Boolean(statusPayload.timed_out) || Number(statusPayload.elapsed_seconds ?? 0) >= 120);

        if (statusPayload.status === "idle") {
          statusPayload = { ...statusPayload, status: "complete", no_new_books: true };
        }
      }

      if (statusPayload.error) {
        throw new Error(statusPayload.error);
      }

      const data = statusPayload.result ?? {};
      const addedCount = Array.isArray(data.added_books) ? data.added_books.length : 0;
      const missingList = Array.isArray(statusPayload.missing_books)
        ? statusPayload.missing_books
        : Array.isArray(data.missing_books)
          ? data.missing_books
          : [];
      const missingCount = missingList.length;
      const message = addedCount > 0
        ? `${series.name}: Check complete. ${addedCount} book${addedCount === 1 ? "" : "s"} added.`
        : missingCount > 0
          ? `${series.name}: Check complete. Missing books: ${missingList.join(", ")}.`
          : `${series.name}: Check complete. No new books found.`;

      await refreshSeriesFromApi();
      flashAddedMessage(message);
      setSeriesCheckStillChecking(false);

      const terminalStatusSignal =
        String(statusPayload.status || "").toLowerCase() === "complete"
          ? "complete"
          : String(statusPayload.current_pass || data.discovery_mode || "");

      const timeoutId = scheduleSeriesCheckReset(
        terminalStatusSignal,
        () => {
          resetSeriesCheckUiState();
          seriesCheckResetTimeoutRef.current = null;
        },
        (cb, delayMs) => window.setTimeout(cb, delayMs),
      );
      if (timeoutId !== null) {
        setSeriesCheckProgress(100);
        setSeriesCheckCurrentPass(statusPayload.current_pass || String(statusPayload.status || "complete"));
        seriesCheckResetTimeoutRef.current = timeoutId;
      } else {
        resetSeriesCheckUiState();
      }
    } catch (error) {
      console.error(error);
      alert(error instanceof Error ? error.message : "Unable to check for new books right now.");
      resetSeriesCheckUiState();
    }
  }

  async function handleSaveKnownTotal() {
    if (!series) return;

    const parsed = Number(knownTotalDraft);
    if (!Number.isFinite(parsed) || parsed <= 0) {
      alert("Known total must be a positive number.");
      return;
    }

    setKnownTotalSaving(true);
    try {
      const response = await fetchApiWithFallback(`/series/${series.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: series.name,
          author: series.author || undefined,
          description: series.description || undefined,
          genre: series.genre || undefined,
          tags: series.tags || undefined,
          total_books: parsed,
          series_status: series.series_status || "ongoing",
          next_unread_book_number: series.next_unread_book_number ?? undefined,
          next_upcoming_book_number: series.next_upcoming_book_number ?? undefined,
          missing_books: series.missing_books ?? undefined,
          is_finished: series.is_finished ?? false,
        }),
      });

      if (!response.ok) {
        throw new Error(`Failed to save known total (${response.status})`);
      }

      await refreshSeriesFromApi();
      flashAddedMessage(`Saved known total of ${parsed}.`);
    } catch (error) {
      console.error(error);
      alert(error instanceof Error ? error.message : "Unable to save known total.");
    } finally {
      setKnownTotalSaving(false);
    }
  }

  async function handleApplyKnownSeriesList() {
    if (!series) return;

    const parsedEntries = parseKnownSeriesListText(knownSeriesListText);
    if (!parsedEntries.length) {
      alert("I could not parse numbered entries like '53 Forgotten In Death (2021)'.");
      return;
    }

    setKnownSeriesListSaving(true);
    try {
      const existingBooks: BookRecord[] = Array.isArray(series.books) ? series.books : [];
      const payloadEntries = parsedEntries.map((entry) => ({
        ...entry,
        title: canonicalizeSuggestionTitle(
          entry.title,
          String(entry.bookNumber),
          existingBooks,
          series.name,
        ),
      }));

      const response = await fetchApiWithFallback(`/series/${series.id}/apply_known_list`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ entries: payloadEntries }),
      });

      if (!response.ok) {
        throw new Error(`Failed to apply known series list (${response.status})`);
      }

      const result = await response.json();

      await refreshSeriesFromApi();
      setKnownSeriesListDialogOpen(false);
      setKnownSeriesListText("");
      flashAddedMessage(`Applied known series list: created ${result.created}, updated ${result.updated}.`);
    } catch (error) {
      console.error(error);
      alert(error instanceof Error ? error.message : "Unable to apply known series list.");
    } finally {
      setKnownSeriesListSaving(false);
    }
  }

  async function handleApplyReleaseIntel() {
    if (!series) return;

    const parsedEntries = parseReleaseIntelText(releaseIntelText);
    if (!parsedEntries.length) {
      alert("I could not detect entries like 'Book 11 (Title)', 'Book 11: Title', or 'Book 11 ... releases on Month Day, Year'.");
      return;
    }

    setReleaseIntelSaving(true);
    setReleaseIntelMessage(null);

    try {
      let created = 0;
      let updated = 0;

      const existingBooks: BookRecord[] = Array.isArray(series?.books) ? series.books : [];

      for (const entry of parsedEntries) {
        const existing = existingBooks.find((book) => Number(book?.book_number) === entry.bookNumber);
        const authorValue = String(series?.author || existing?.author || "Unknown author").trim();
        const normalizedTitle = canonicalizeSuggestionTitle(
          entry.title,
          String(entry.bookNumber),
          existingBooks,
          series?.name,
        );

        if (existing?.id) {
          const response = await fetchApiWithFallback(`/books/${existing.id}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              title: normalizedTitle,
              author: authorValue,
              is_read: false,
              read_status: "upcoming",
              release_date: entry.releaseDate || undefined,
              publication_date: entry.releaseDate || undefined,
            }),
          });

          if (!response.ok) {
            throw new Error(`Failed to update book #${entry.bookNumber}`);
          }
          updated += 1;
        } else {
          const response = await fetchApiWithFallback("/books/", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              title: normalizedTitle,
              author: authorValue,
              series_id: Number(series.id),
              series_order: entry.bookNumber,
              book_number: entry.bookNumber,
              is_read: false,
              read_status: "upcoming",
              release_date: entry.releaseDate || undefined,
              publication_date: entry.releaseDate || undefined,
            }),
          });

          if (!response.ok) {
            throw new Error(`Failed to create book #${entry.bookNumber}`);
          }
          created += 1;
        }
      }

      await fetchApiWithFallback(`/series/${series.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: series.name,
          author: series.author || undefined,
          description: series.description || undefined,
          genre: series.genre || undefined,
          tags: series.tags || undefined,
          total_books: series.total_books ?? totalBooks,
          series_status: "ongoing",
          next_unread_book_number: series.next_unread_book_number ?? undefined,
          next_upcoming_book_number: series.next_upcoming_book_number ?? undefined,
          missing_books: series.missing_books ?? undefined,
          is_finished: false,
        }),
      });

      await refreshSeriesFromApi();
      setReleaseIntelMessage(`Applied release intel: created ${created}, updated ${updated}.`);
      setReleaseIntelText("");
    } catch (error) {
      console.error(error);
      const message = error instanceof Error ? error.message : "Unable to apply release intel.";
      alert(message);
    } finally {
      setReleaseIntelSaving(false);
    }
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

  async function handleEditBookTitle(book: BookRecord) {
    const currentTitle = String(book?.title || "").trim();
    const editedTitle = prompt("Edit book title:", currentTitle);
    if (editedTitle === null) {
      return;
    }

    const nextTitle = String(editedTitle || "").trim();
    if (!nextTitle) {
      alert("Title cannot be empty.");
      return;
    }
    if (nextTitle === currentTitle) {
      return;
    }

    try {
      const response = await fetchApiWithFallback(`/books/${book.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: nextTitle }),
      });

      if (!response.ok) {
        throw new Error(`Failed to update title (${response.status})`);
      }

      const updatedBook = await response.json();
      setSeries((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          books: Array.isArray(prev.books)
            ? prev.books.map((item) => (item.id === updatedBook.id ? { ...item, ...updatedBook } : item))
            : prev.books,
        };
      });
      flashAddedMessage(`Updated title for book #${book.book_number ?? "?"}.`);
    } catch (error) {
      console.error(error);
      alert("Unable to update title right now.");
    }
  }

  async function handleApplyTitleNormalization() {
    if (!titleNormalizationPreview.length) {
      return;
    }
    if (!normalizeTitlesConfirmed) {
      alert("Please confirm the preview before applying title normalization.");
      return;
    }

    setTitleNormalizeSaving(true);
    try {
      const updatedById = new Map<number, BookRecord>();

      for (const row of titleNormalizationPreview) {
        const response = await fetchApiWithFallback(`/books/${row.id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: row.normalizedTitle }),
        });

        if (!response.ok) {
          throw new Error(`Failed to normalize title for book id ${row.id}`);
        }

        const updatedBook = await response.json();
        updatedById.set(updatedBook.id, updatedBook);
      }

      setSeries((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          books: Array.isArray(prev.books)
            ? prev.books.map((item) => (updatedById.has(item.id) ? { ...item, ...updatedById.get(item.id) } : item))
            : prev.books,
        };
      });
          await refreshSeriesFromApi();
          setNormalizeTitlesConfirmed(false);

      flashAddedMessage(`Normalized ${updatedById.size} title${updatedById.size === 1 ? "" : "s"}.`);
    } catch (error) {
      console.error(error);
      alert("Unable to apply title normalization right now.");
    } finally {
      setTitleNormalizeSaving(false);
    }
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

  async function handleFetchSummary(bookId: number) {
    setSummaryLoadingId(bookId);
    try {
        const response = await fetchApiWithFallback(`/books/${bookId}/summary`, {
        method: "POST",
      });
      if (!response.ok) {
        throw new Error(`Failed to fetch summary (${response.status})`);
      }

      const data = await response.json();
      const updatedBook = data.book;
      setSeries((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          books: prev.books?.map((book) =>
            book.id === updatedBook.id ? updatedBook : book
          ),
        };
      });
      setSummaryEditorBook(updatedBook);
      setSummaryDraft(String(updatedBook.auto_summary || ""));
      setNotesDraft(String(updatedBook.notes || ""));
    } catch (err) {
      console.error(err);
      alert("Unable to fetch a summary for this book right now.");
    } finally {
      setSummaryLoadingId(null);
    }
  }

  function openSummaryEditor(book: BookRecord) {
    setSummaryEditorBook(book);
    setSummaryDraft(String(book?.auto_summary || ""));
    setNotesDraft(String(book?.notes || ""));
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
      setSeries((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          books: Array.isArray(prev.books)
            ? prev.books.map((book) => (book.id === updatedBook.id ? { ...book, ...updatedBook } : book))
            : prev.books,
        };
      });
      setSummaryEditorBook(updatedBook);
      setSummaryDraft(String(updatedBook.auto_summary || ""));
      setNotesDraft(String(updatedBook.notes || ""));
    } catch (err) {
      console.error(err);
      alert("Unable to save summary or notes right now.");
    } finally {
      setSummarySaving(false);
    }
  }

  async function handleToggleRead(book: BookRecord) {
    const nextIsRead = !book.is_read;
    const nextStatus = nextIsRead ? "read" : (hasUpcomingBookSignals(book) ? "upcoming" : "unread");

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

      if (!response.ok) {
        throw new Error(`Failed to update book (${response.status})`);
      }

      const updatedBook = await response.json();
      setSeries((prev) => {
        if (!prev) return prev;
        const prevBooks = Array.isArray(prev.books) ? prev.books : [];
        return {
          ...prev,
          books: prevBooks.map((item) =>
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

  async function handleDeleteBook(book: BookRecord) {
    const confirmed = window.confirm(`Delete \"${book.title || "this book"}\"? This cannot be undone.`);
    if (!confirmed) {
      return;
    }

    try {
      const response = await fetchApiWithFallback(`/books/${book.id}`, {
        method: "DELETE",
      });

      if (!response.ok) {
        throw new Error(`Failed to delete book (${response.status})`);
      }

      setSeries((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          books: Array.isArray(prev.books) ? prev.books.filter((item) => item.id !== book.id) : prev.books,
        };
      });

      await refreshSeriesFromApi();
      setNormalizeTitlesConfirmed(false);
      flashAddedMessage(`Deleted book #${book.book_number ?? book.id}.`);
    } catch (error) {
      console.error(error);
      alert(error instanceof Error ? error.message : "Unable to delete book right now.");
    }
  }

  async function handleSetBookStatus(book: BookRecord) {
    const currentStatus = getBookStatus(book);
    const editedStatus = prompt("Set status (read/unread/upcoming):", currentStatus);
    if (editedStatus === null) {
      return;
    }

    const normalizedStatus = editedStatus.trim().toLowerCase();
    if (!["read", "unread", "upcoming"].includes(normalizedStatus)) {
      alert("Status must be read, unread, or upcoming.");
      return;
    }

    const today = new Date().toISOString().split("T")[0];
    let readDate: string | null = null;
    let releaseDate: string | null = null;

    if (normalizedStatus === "read") {
      const readDatePrompt = prompt("Read date (MM-DD-YYYY):", String(book.read_date || today));
      if (readDatePrompt === null) {
        return;
      }
      readDate = normalizeDateInput(readDatePrompt) || today;
      releaseDate = book.release_date || null;
    } else {
      const defaultReleaseDate = String(book.release_date || book.publication_date || "");
      const datePrompt = prompt(
        "Date for this book (MM-DD-YYYY, optional):",
        defaultReleaseDate,
      );
      if (datePrompt === null) {
        return;
      }
      releaseDate = normalizeDateInput(datePrompt);
      readDate = null;
    }

    try {
      const response = await fetchApiWithFallback(`/books/${book.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          is_read: normalizedStatus === "read",
          read_status: normalizedStatus,
          read_date: readDate,
          release_date: releaseDate,
        }),
      });

      if (!response.ok) {
        throw new Error(`Failed to update book (${response.status})`);
      }

      const updatedBook = await response.json();
      setSeries((prev) => {
        if (!prev) return prev;
        const prevBooks = Array.isArray(prev.books) ? prev.books : [];
        return {
          ...prev,
          books: prevBooks.map((item) =>
            item.id === updatedBook.id ? { ...item, ...updatedBook } : item
          ),
        };
      });
      publishBookStatusUpdate(updatedBook);
    } catch (err) {
      console.error(err);
      alert("Unable to update status right now.");
    }
  }

  async function handleToggleSeriesFinished() {
    if (!series) return;
    setFinishedToggleSaving(true);

    try {
      const movingToUnfinished = Boolean(series.is_finished);
      const confirmed = window.confirm(
        movingToUnfinished
          ? "Move this series to unfinished?"
          : "Move this series to finished?"
      );
      if (!confirmed) {
        return;
      }

      const endpoint = movingToUnfinished
        ? `/series/${series.id}/mark_unfinished`
        : `/series/${series.id}/mark_finished`;
      const response = await fetchApiWithFallback(endpoint, { method: "POST" });

      if (!response.ok) {
        throw new Error(`Failed to update series (${response.status})`);
      }

      const result = await response.json();
      await refreshSeriesFromApi();

      if (movingToUnfinished) {
        flashAddedMessage("Series moved to unfinished.");
        window.setTimeout(() => {
          router.push("/series?view=finished");
        }, 700);
      } else if (result?.is_finished) {
        flashAddedMessage("Series moved to finished.");
        window.setTimeout(() => {
          router.push("/series?view=ongoing");
        }, 700);
      } else {
        flashAddedMessage("Finished override saved, but series remains ongoing due to current intelligence rules.");
      }
    } catch (err) {
      console.error(err);
      alert("Unable to update series finished state right now.");
    } finally {
      setFinishedToggleSaving(false);
    }
  }

  async function fetchSuggestionForMissingBook(bookNumber: string, seriesData?: SeriesRecord, signal?: AbortSignal): Promise<SuggestionRecord[] | null> {
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

      const path = `/books/suggest?${params.toString()}`;
      console.log(`[Suggestion ${bookNumber}] Fetching from: ${path}`);

      const timeoutController = new AbortController();
      timeoutId = window.setTimeout(() => timeoutController.abort(), 90000);
      const combinedSignal = signal
        ? AbortSignal.any([signal, timeoutController.signal])
        : timeoutController.signal;

      const response = await fetchApiWithFallback(path, { signal: combinedSignal });
      if (!response.ok) {
        throw new Error(`Failed to lookup suggestions (${response.status})`);
      }

      const responseData = await response.json();
      const serpUsed = Number(responseData?.diagnostics?.provider_counts?.serpapi || 0);
      incrementSerpUsage(serpUsed);
      console.log(`[Suggestion ${bookNumber}] Got ${responseData.results?.length || 0} results`);
      return (responseData.results || []) as SuggestionRecord[];
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

  async function handleAddSuggestion(bookNumber: string, suggestion: SuggestionRecord) {
    if (!series) return;

    try {
      // Reduce write contention while adding a book by pausing active scans.
      if (scanAbortRef.current) {
        scanAbortRef.current.abort();
        scanAbortRef.current = null;
        setScanStatus("paused");
      }

      const cleanedTitle = canonicalizeSuggestionTitle(
        String(suggestion.title || ""),
        bookNumber,
        series?.books || [],
        series?.name,
      );
      const editedTitle = prompt(`Confirm title for book ${bookNumber} (author next):`, cleanedTitle);
      if (editedTitle === null) {
        return;
      }
      const finalTitle = editedTitle.trim() || cleanedTitle;
      const suggestedAuthor = String(suggestion.author || series.author || "Unknown author").trim();
      const editedAuthor = prompt(`Confirm author for book ${bookNumber}:`, suggestedAuthor);
      if (editedAuthor === null) {
        return;
      }
      const finalAuthor = editedAuthor.trim() || suggestedAuthor;

      const suggestedStatus = "upcoming";
      const editedStatus = prompt(
        `Status for book ${bookNumber}? (upcoming/unread/read)`,
        suggestedStatus,
      );
      if (editedStatus === null) {
        return;
      }
      const normalizedStatus = editedStatus.trim().toLowerCase();
      if (!["upcoming", "unread", "read"].includes(normalizedStatus)) {
        alert("Status must be one of: upcoming, unread, read.");
        return;
      }

      const releaseDateDefault = suggestion.year ? `${String(suggestion.year).slice(0, 4)}-01-01` : "";
      let releaseDate: string | null = null;
      if (normalizedStatus !== "read") {
          const releaseDatePrompt = prompt(
            `Date for book ${bookNumber} (MM-DD-YYYY, optional):`,
            releaseDateDefault,
          );
        if (releaseDatePrompt === null) {
          return;
        }
          releaseDate = normalizeDateInput(releaseDatePrompt);
      }

      let readDate: string | null = null;
      if (normalizedStatus === "read") {
        const today = new Date().toISOString().split("T")[0];
          const readDatePrompt = prompt(`Read date for book ${bookNumber} (MM-DD-YYYY):`, today);
        if (readDatePrompt === null) {
          return;
        }
          readDate = normalizeDateInput(readDatePrompt) || today;
      }

      const response = await fetchApiWithFallback("/books/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: finalTitle,
          author: finalAuthor,
          series_id: Number(series.id),
          series_order: Number(bookNumber),
          book_number: Number(bookNumber),
          read_status: normalizedStatus,
          is_read: normalizedStatus === "read",
          read_date: readDate || undefined,
          release_date: releaseDate || undefined,
          publication_date: suggestion.year ? `${String(suggestion.year).slice(0, 4)}-01-01` : undefined,
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
      if (normalizedStatus === "upcoming") {
        setRecentUpcomingBookIds((prev) => [Number(newBook.id), ...prev.filter((id) => id !== Number(newBook.id))]);
      }
      setSeries((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          books: sortBooksBySeriesOrder([...(prev.books || []), newBook]),
          missing_books: Array.isArray(prev.missing_books)
            ? prev.missing_books.filter((order: string) => String(order) !== String(bookNumber))
            : prev.missing_books,
        };
      });
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

  async function handleCreateBookFromDialog() {
    if (!series) return;

    const title = String(addBookTitle || "").trim();
    const parsedNumber = Number(addBookNumber);
    if (!title) {
      alert("Title is required.");
      return;
    }
    if (!Number.isFinite(parsedNumber) || parsedNumber <= 0) {
      alert("Book number must be a positive number.");
      return;
    }

    const normalizedDate = normalizeDateInput(addBookDate);
    const today = new Date().toISOString().split("T")[0];
    const payload: Record<string, unknown> = {
      title,
      author: String(series.author || "Unknown author").trim() || "Unknown author",
      series_id: Number(series.id),
      series_order: parsedNumber,
      book_number: parsedNumber,
      read_status: addBookStatus,
      is_read: addBookStatus === "read",
    };

    if (addBookStatus === "read") {
      payload.read_date = normalizedDate || today;
    } else if (normalizedDate) {
      payload.release_date = normalizedDate;
    }

    setAddBookSaving(true);
    try {
      const response = await fetchApiWithFallback("/books/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        let detail = "";
        try {
          const data = await response.json();
          detail = data?.detail ? ` - ${data.detail}` : "";
        } catch {
          // ignore
        }
        throw new Error(`Failed to add book (${response.status})${detail}`);
      }

      const createdBook = await response.json();
      if (addBookStatus === "upcoming") {
        setRecentUpcomingBookIds((prev) => [Number(createdBook.id), ...prev.filter((id) => id !== Number(createdBook.id))]);
      }
      setSeries((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          books: sortBooksBySeriesOrder([...(prev.books || []), createdBook]),
        };
      });

      setAddBookTitle("");
      setAddBookNumber("");
      setAddBookStatus("upcoming");
      setAddBookDate("");
      setAddBookDialogOpen(false);
      flashAddedMessage(`Added book #${parsedNumber}: ${title}`);
      await refreshSeriesFromApi();
    } catch (err) {
      console.error(err);
      const message = err instanceof Error ? err.message : "Unable to add book right now.";
      alert(message);
    } finally {
      setAddBookSaving(false);
    }
  }

  return (
    <div className="p-3 space-y-2">
      <div className="grid gap-2 md:grid-cols-[minmax(0,1fr)_auto] md:items-start">
        <div className="space-y-1">
          <p className="text-sm uppercase tracking-[0.2em] text-muted-foreground">Series detail</p>
          <div>
            <h1 className="text-3xl font-bold">{series.name}</h1>
            <p className="text-sm text-muted-foreground">{series.author || "Unknown author"}</p>
            <p className="mt-2 text-base font-semibold text-foreground">Books in this Series:</p>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setAddBookDialogOpen(true)}
              >
                Add Book
              </Button>
              <Button
                type="button"
                variant="secondary"
                size="sm"
                onClick={() => void handleCheckForNew()}
                disabled={seriesCheckLoading}
              >
                {seriesCheckLoading ? `Checking ${series.name}…` : `Check ${series.name} for New`}
              </Button>
              {seriesCheckLoading ? (
                <div className="flex min-w-[240px] items-center gap-2 rounded border bg-background px-2 py-1 text-xs">
                  <Spinner />
                  <div className="w-32 overflow-hidden rounded-full bg-slate-200">
                    <div
                      className="h-1.5 bg-slate-700 transition-all duration-500"
                      style={{ width: `${Math.max(4, seriesCheckProgress)}%` }}
                    />
                  </div>
                  <span className={seriesCheckStillChecking ? "animate-pulse text-muted-foreground" : "text-muted-foreground"}>
                    {seriesCheckStillChecking ? "Still checking..." : `${seriesCheckProgress}%`}
                  </span>
                  {seriesCheckCurrentPass ? (
                    <span className="text-muted-foreground">{seriesCheckCurrentPass}</span>
                  ) : null}
                </div>
              ) : null}
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => handleOpenGoogleSearch(`${series.name} ${series.author || ""} next book release`.trim())}
              >
                Check URL
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setReleaseIntelDialogOpen(true)}
              >
                Paste Series Intel
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setNormalizeTitlesDialogOpen(true)}
              >
                Normalize Titles
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setKnownSeriesListDialogOpen(true)}
              >
                Apply Known Series List
              </Button>
              <div className="flex items-center gap-2 rounded border bg-slate-50 px-2 py-1 text-xs">
                <label htmlFor="series-title-normalization-mode" className="whitespace-nowrap text-muted-foreground">
                  Title normalization
                </label>
                <select
                  id="series-title-normalization-mode"
                  value={series?.title_normalization_mode_override ?? "keep_original"}
                  onChange={(event) => {
                    void handleSaveTitleNormalizationOverride(event.target.value as TitleNormalizationMode);
                    setNormalizeTitlesConfirmed(false);
                  }}
                  className="h-8 rounded border bg-background px-2 text-xs"
                >
                  <option value="keep_original">Keep Original Title - Leave As Is</option>
                  <option value="clean_up">Clean Up Title - Fix formatting junk</option>
                  <option value="new_clean_title">New Clean Title - Keep book name, add clean series suffix</option>
                  <option value="match_other_titles">Match Other Titles - Format like the rest of the series</option>
                </select>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => setTitleNormalizationExamplesOpen(true)}
                >
                  Examples
                </Button>
              </div>
              <label htmlFor="known-total-books" className="text-xs text-muted-foreground">Known total</label>
              <input
                id="known-total-books"
                value={knownTotalDraft}
                onChange={(event) => setKnownTotalDraft(event.target.value)}
                placeholder="e.g. 64"
                className="h-8 w-24 rounded border bg-background px-2 text-xs"
              />
              <Button type="button" variant="outline" size="sm" onClick={handleSaveKnownTotal} disabled={knownTotalSaving}>
                {knownTotalSaving ? "Saving..." : "Save total"}
              </Button>
            </div>
          </div>
          {series.description && (
            <p className="max-w-3xl text-sm leading-6 text-muted-foreground">{series.description}</p>
          )}
        </div>

        <div className="flex flex-col items-start gap-1 md:items-end md:pl-3">
          <div className="flex w-full flex-wrap items-center gap-2 md:justify-end">
            <Button
              variant="outline"
              onClick={handleToggleSeriesFinished}
              disabled={finishedToggleSaving}
            >
              {finishedToggleSaving
                ? "Saving..."
                : series.is_finished
                  ? "Move to unfinished"
                  : "Move to finished"}
            </Button>
            <Link href="/books">
              <Button variant="outline">Back to Library</Button>
            </Link>
            <Link href={viewAllSeriesHref}>
              <Button variant="secondary">View all series</Button>
            </Link>
          </div>

          <div className="flex flex-wrap items-start gap-2 md:justify-end">
            <Table className="w-auto min-w-[270px] text-sm">
              <TableBody>
                <TableRow>
                  <TableCell className="py-1.5">Unread: <span className="font-semibold">{unreadCount}</span></TableCell>
                  <TableCell className="py-1.5">Read: <span className="font-semibold">{readCount}</span></TableCell>
                </TableRow>
                <TableRow>
                  <TableCell className="py-1.5">Total: <span className="font-semibold">{totalBooks}</span></TableCell>
                  <TableCell className="py-1.5">Upcoming: <span className="font-semibold">{upcomingCount}</span></TableCell>
                </TableRow>
              </TableBody>
            </Table>
          </div>

          <Table className="w-auto text-xs">
              <TableBody>
                <TableRow>
                  <TableCell className="py-1 px-2">Status: <span className="font-medium">{series.series_status || "Unknown"}</span></TableCell>
                  <TableCell className="py-1 px-2">Next unread: <span className="font-medium">{series.next_unread_book_number ?? "—"}</span></TableCell>
                  <TableCell className="py-1 px-2">Next upcoming: <span className="font-medium">{series.next_upcoming_book_number ?? "—"}</span></TableCell>
                  <TableCell className="py-1 px-2">Missing: <span className="font-medium">{missingOrders.length}</span></TableCell>
                  <TableCell className="py-1 px-2">Serp: <span className="font-medium">{serpUsageCount}</span></TableCell>
                </TableRow>
              </TableBody>
            </Table>
        </div>
      </div>

      {recentAddMessage ? (
        <div className="fixed bottom-4 right-4 z-50 max-w-md rounded-md border-2 border-emerald-900 bg-emerald-800 px-3 py-2 text-sm font-semibold text-white shadow-2xl">
          {recentAddMessage}
        </div>
      ) : null}

      <div className="flex justify-end">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <label htmlFor="series-books-sort">Sort</label>
          <select
            id="series-books-sort"
            aria-label="Sort books"
            value={bookSortMode}
            onChange={(event) => setBookSortMode(event.target.value as "series" | "az")}
            className="h-7 rounded-md border bg-background px-2 text-[11px] font-normal"
          >
            <option value="series">Series order</option>
            <option value="az">Title A to Z</option>
          </select>
        </div>
      </div>

      <div ref={booksTableWrapRef} className="overflow-x-auto rounded-lg border bg-card/80">
      <Table className="w-full table-fixed">
        <TableHeader>
          <TableRow>
            <TableHead className="relative" style={{ width: `${columnWidths.title}%` }}>
              Title
              <button
                type="button"
                aria-label="Resize Title column"
                onMouseDown={(event) => startColumnResize("title", event)}
                className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
              />
            </TableHead>
            <TableHead className="relative" style={{ width: `${columnWidths.author}%` }}>
              Author
              <button
                type="button"
                aria-label="Resize Author column"
                onMouseDown={(event) => startColumnResize("author", event)}
                className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
              />
            </TableHead>
            <TableHead className="relative" style={{ width: `${columnWidths.status}%` }}>
              Status
              <button
                type="button"
                aria-label="Resize Status column"
                onMouseDown={(event) => startColumnResize("status", event)}
                className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
              />
            </TableHead>
            <TableHead className="relative" style={{ width: `${columnWidths.date}%` }}>
              Date
              <button
                type="button"
                aria-label="Resize Date column"
                onMouseDown={(event) => startColumnResize("date", event)}
                className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
              />
            </TableHead>
            <TableHead className="relative" style={{ width: `${columnWidths.bookNumber}%` }}>
              Book #
              <button
                type="button"
                aria-label="Resize Book number column"
                onMouseDown={(event) => startColumnResize("bookNumber", event)}
                className="absolute right-0 top-0 z-20 h-full w-3 cursor-col-resize border-r border-border/60 hover:bg-muted/30"
              />
            </TableHead>
            <TableHead style={{ width: `${columnWidths.actions}%` }}>Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {displayedBooks.map((book) => {
            const status = getBookStatus(book);
            const displayDate = getBookDate(book);
            const summary = book.auto_summary;
            const notes = book.notes;
            return (
              <TableRow key={book.id}>
                <TableCell className="truncate" title={book.title ?? undefined}>
                  <div>{book.title || "—"}</div>
                </TableCell>
                <TableCell className="truncate" title={book.author || "—"}>{book.author || "—"}</TableCell>
                <TableCell>
                  <span className={getStatusChipClass(status)}>{status}</span>
                </TableCell>
                <TableCell>{formatDate(displayDate)}</TableCell>
                <TableCell>{book.book_number ?? "—"}</TableCell>
                <TableCell className="space-x-2 whitespace-nowrap">
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
                    {book.is_read ? "Mark unread" : "Mark read"}
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => handleSetBookStatus(book)}
                  >
                    Set status/date
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => handleEditBookTitle(book)}
                  >
                    Edit title
                  </Button>
                  <Button
                    variant="destructive"
                    size="sm"
                    onClick={() => handleDeleteBook(book)}
                  >
                    Delete
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => handleFetchSummary(book.id)}
                    disabled={summaryLoadingId === book.id}
                  >
                    {summary ? "Refresh summary" : "Fetch summary"}
                  </Button>
                  {summary || notes ? (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => openSummaryEditor(book)}
                    >
                      See summary
                    </Button>
                  ) : null}
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
      </div>

      <Dialog
        open={Boolean(summaryEditorBook)}
        onOpenChange={(open) => {
          if (!open) {
            setSummaryEditorBook(null);
          }
        }}
      >
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>{summaryEditorBook?.title || "Book summary"}</DialogTitle>
            <DialogDescription>
              Review the fetched summary and add your own notes without stretching the table rows.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-3">
            <div className="space-y-1">
              <Label htmlFor="series-book-summary">Summary</Label>
              <textarea
                id="series-book-summary"
                value={summaryDraft}
                onChange={(event) => setSummaryDraft(event.target.value)}
                className="min-h-32 w-full rounded-lg border border-input bg-transparent px-3 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="series-book-notes">My notes</Label>
              <textarea
                id="series-book-notes"
                value={notesDraft}
                onChange={(event) => setNotesDraft(event.target.value)}
                className="min-h-28 w-full rounded-lg border border-input bg-transparent px-3 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
              />
            </div>
          </div>

          <DialogFooter showCloseButton>
            <Button type="button" onClick={handleSaveSummaryEditor} disabled={summarySaving}>
              {summarySaving ? "Saving..." : "Save changes"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={addBookDialogOpen}
        onOpenChange={setAddBookDialogOpen}
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Add Book</DialogTitle>
            <DialogDescription>
              Add a new book directly to this series while you review release intel.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-3">
            <div className="space-y-1">
              <Label htmlFor="add-book-title">Title</Label>
              <input
                id="add-book-title"
                value={addBookTitle}
                onChange={(event) => setAddBookTitle(event.target.value)}
                placeholder="Book title"
                className="h-9 w-full rounded border bg-white px-2 text-sm"
              />
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <Label htmlFor="add-book-number">Book #</Label>
                <input
                  id="add-book-number"
                  type="number"
                  step="0.1"
                  min="0"
                  value={addBookNumber}
                  onChange={(event) => setAddBookNumber(event.target.value)}
                  placeholder="e.g. 28"
                  className="h-9 w-full rounded border bg-white px-2 text-sm"
                />
              </div>

              <div className="space-y-1">
                <Label htmlFor="add-book-status">Status</Label>
                <select
                  id="add-book-status"
                  value={addBookStatus}
                  onChange={(event) => setAddBookStatus(event.target.value as "upcoming" | "unread" | "read")}
                  className="h-9 w-full rounded border bg-white px-2 text-sm"
                >
                  <option value="upcoming">upcoming</option>
                  <option value="unread">unread</option>
                  <option value="read">read</option>
                </select>
              </div>
            </div>

            <div className="space-y-1">
              <Label htmlFor="add-book-date">Date (optional)</Label>
              <input
                id="add-book-date"
                value={addBookDate}
                onChange={(event) => setAddBookDate(event.target.value)}
                placeholder={addBookStatus === "read" ? "Read date (MM-DD-YYYY)" : "Release date (MM-DD-YYYY)"}
                className="h-9 w-full rounded border bg-white px-2 text-sm"
              />
            </div>
          </div>

          <DialogFooter showCloseButton>
            <Button
              type="button"
              variant="secondary"
              onClick={handleCreateBookFromDialog}
              disabled={addBookSaving}
            >
              {addBookSaving ? "Adding..." : "Add Book"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={releaseIntelDialogOpen}
        onOpenChange={setReleaseIntelDialogOpen}
      >
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Paste Release Intel</DialogTitle>
            <DialogDescription>
              Paste Google results text. Entries like &quot;Book 12: Unique&quot; and dates like &quot;August 3rd, 2026&quot; are parsed automatically.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-2">
            <textarea
              value={releaseIntelText}
              onChange={(event) => setReleaseIntelText(event.target.value)}
              placeholder="Paste release summary text..."
              className="min-h-40 w-full rounded border bg-white px-2 py-2 text-xs"
            />
            {releaseIntelMessage ? (
              <p className="text-xs text-blue-900">{releaseIntelMessage}</p>
            ) : null}
          </div>

          <DialogFooter showCloseButton>
            <Button
              type="button"
              variant="secondary"
              onClick={handleApplyReleaseIntel}
              disabled={releaseIntelSaving}
            >
              {releaseIntelSaving ? "Applying…" : "Apply Release Intel"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={normalizeTitlesDialogOpen}
        onOpenChange={(open) => {
          setNormalizeTitlesDialogOpen(open);
          if (!open) {
            setNormalizeTitlesConfirmed(false);
          }
        }}
      >
        <DialogContent className="sm:max-w-4xl">
          <DialogHeader>
            <DialogTitle>Normalize Titles</DialogTitle>
            <DialogDescription>
              Active mode: {getTitleNormalizationModeLabel(seriesNormalizationMode)}. {getTitleNormalizationModeDescription(seriesNormalizationMode)}
            </DialogDescription>
          </DialogHeader>

          {titleNormalizationPreview.length > 0 ? (
            <div className="max-h-[32rem] overflow-auto rounded border bg-white text-xs">
              <div className="grid grid-cols-[1fr_auto_1fr] gap-2 border-b bg-slate-50 px-3 py-2 font-semibold text-muted-foreground">
                <div>Current title</div>
                <div />
                <div>Normalized title</div>
              </div>
              {titleNormalizationPreview.map((row) => (
                <div key={row.id} className="grid grid-cols-[1fr_auto_1fr] items-center gap-2 border-b px-3 py-2 last:border-b-0">
                  <div className="min-w-0">
                    <p className="truncate font-medium text-foreground">{row.currentTitle}</p>
                  </div>
                  <div className="px-1 text-sm text-muted-foreground" aria-hidden="true">
                    →
                  </div>
                  <div className="min-w-0">
                    <p className="truncate font-medium text-emerald-700">{row.normalizedTitle}</p>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              {seriesNormalizationMode === "keep_original"
                ? "Keep Original Title mode is selected, so no batch changes are needed."
                : "No title normalization changes are needed for the current mode."}
            </p>
          )}

          <label className="flex items-start gap-2 rounded border bg-slate-50 px-3 py-2 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={normalizeTitlesConfirmed}
              onChange={(event) => setNormalizeTitlesConfirmed(event.target.checked)}
              className="mt-0.5"
            />
            <span>I reviewed the preview and want to apply these title changes.</span>
          </label>

          <DialogFooter showCloseButton>
            <Button
              type="button"
              variant="secondary"
              onClick={handleApplyTitleNormalization}
              disabled={titleNormalizeSaving || titleNormalizationPreview.length === 0 || !normalizeTitlesConfirmed}
            >
              {titleNormalizeSaving
                ? "Applying…"
                : `Apply all (${titleNormalizationPreview.length})`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={titleNormalizationExamplesOpen} onOpenChange={setTitleNormalizationExamplesOpen}>
        <DialogContent className="sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>Title Style Examples</DialogTitle>
            <DialogDescription>
              Each option applies only to this series and does not use external metadata.
            </DialogDescription>
          </DialogHeader>

          <div className="max-h-[34rem] space-y-4 overflow-auto rounded border bg-white p-3 text-xs">
            <div className="space-y-1">
              <p className="font-semibold">1. Keep Original Title - Leave As Is</p>
              <p className="text-muted-foreground">Original:</p>
              <p>Cherry Blossom Girls International: (Book Nine): (Cherry Blossom Girls Book 9)</p>
              <p className="text-muted-foreground">Stays as:</p>
              <p>Cherry Blossom Girls International: (Book Nine): (Cherry Blossom Girls Book 9)</p>
            </div>

            <div className="space-y-1">
              <p className="font-semibold">2. Clean Up Title - Fix formatting junk</p>
              <p className="text-muted-foreground">Original:</p>
              <p>Cherry Blossom Girls International: (Book Nine): (Cherry Blossom Girls Book 9)</p>
              <p className="text-muted-foreground">Becomes:</p>
              <p>Cherry Blossom Girls International: Book Nine (Cherry Blossom Girls Book 9)</p>
            </div>

            <div className="space-y-1">
              <p className="font-semibold">3. New Clean Title - Keep book name, add clean series suffix</p>
              <p className="text-muted-foreground">Original:</p>
              <p>Cherry Blossom Girls International: (Book Nine): (Cherry Blossom Girls Book 9)</p>
              <p className="text-muted-foreground">Becomes:</p>
              <p>Cherry Blossom Girls International (Cherry Blossom Girls Book 9)</p>
            </div>

            <div className="space-y-1">
              <p className="font-semibold">4. Match Other Titles - Format like the rest of the series</p>
              <p className="text-muted-foreground">Original:</p>
              <p>Cherry Blossom Girls International: (Book Nine): (Cherry Blossom Girls Book 9)</p>
              <p className="text-muted-foreground">Becomes:</p>
              <p>Cherry Blossom Girls International (Cherry Blossom Girls Book 9)</p>
              <p className="text-muted-foreground">(matching the style used by other books in the same series)</p>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog
        open={knownSeriesListDialogOpen}
        onOpenChange={setKnownSeriesListDialogOpen}
      >
        <DialogContent className="sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>Apply Known Series List</DialogTitle>
            <DialogDescription>
              Paste numbered entries such as &quot;53 Forgotten In Death (2021)&quot;. This will create or update books in the current series and set the known total from the highest whole-numbered entry.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-2">
            <textarea
              value={knownSeriesListText}
              onChange={(event) => setKnownSeriesListText(event.target.value)}
              placeholder="Paste the known series list here..."
              className="min-h-56 w-full rounded border bg-white px-2 py-2 text-xs"
            />
          </div>

          <DialogFooter showCloseButton>
            <Button
              type="button"
              variant="secondary"
              onClick={handleApplyKnownSeriesList}
              disabled={knownSeriesListSaving}
            >
              {knownSeriesListSaving ? "Applying..." : "Apply Known List"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

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
        <div className="space-y-2">
          <div className="rounded-md border border-yellow-300 bg-yellow-100 px-3 py-2 text-center text-sm font-bold uppercase tracking-wide text-yellow-900">
            Missing Book Finder
          </div>
          <div className="rounded-lg border border-yellow-200 bg-yellow-50 p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-semibold text-yellow-900">Missing: {missingOrders.length}</span>
              <span className="text-xs text-muted-foreground">{scanCompletedCount}/{scanTotalCount} scanned ({scanPercent}%)</span>
              <span className="text-xs text-muted-foreground">Status: {scanStatus}</span>
              {scanCurrentOrder ? <span className="text-xs text-muted-foreground">Fetching #{scanCurrentOrder}</span> : null}
              <label className="ml-1 inline-flex items-center gap-1 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  checked={storeOnly}
                  onChange={(event) => setStoreOnlyAndPersist(event.target.checked)}
                />
                Store only
              </label>
              {scanStatus !== "running" ? (
                <Button variant="secondary" size="sm" onClick={handleStartFullScan}>
                  {scanStatus === "paused" ? "Restart Scan" : "Run Scan"}
                </Button>
              ) : (
                <Button variant="secondary" size="sm" onClick={handlePauseScan}>
                  Pause
                </Button>
              )}
              {scanStatus === "paused" && scanPendingRef.current.length > 0 ? (
                <Button variant="outline" size="sm" onClick={handleResumeScan}>
                  Resume
                </Button>
              ) : null}
              <Button variant="ghost" size="sm" onClick={handleResetScanProgress}>
                Reset
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setShowMissingFinderDetails((prev) => !prev)}
              >
                {showMissingFinderDetails ? "Hide details" : `Show details (${missingOrders.length})`}
              </Button>
            </div>

            <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-white/70">
              <div
                className="h-full bg-yellow-500 transition-all duration-300"
                style={{ width: `${scanPercent}%` }}
              />
            </div>

            {showMissingFinderDetails ? (
              <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
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
            ) : (
              <p className="mt-2 text-xs text-muted-foreground">
                Details are collapsed to save space. Expand when you want to browse missing slots.
              </p>
            )}
          </div>
        </div>
      )}

    </div>
  );
}
