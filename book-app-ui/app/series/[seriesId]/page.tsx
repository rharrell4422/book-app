"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
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
import { useToast } from "@/components/ui/use-toast";

const STATIC_API_BASE_CANDIDATES = [
  process.env.NEXT_PUBLIC_API_BASE_URL,
  "http://localhost:8000",
  "http://127.0.0.1:8000",
].filter(Boolean) as string[];

 type TitleNormalizationMode = "keep_original" | "clean_up" | "new_clean_title" | "match_other_titles";
type TitleNormalizationWizardMode = TitleNormalizationMode | "custom";

type BookRecord = {
  id: number;
  title?: string | null;
  subtitle?: string | null;
  author?: string | null;
  read_status?: string | null;
  is_read?: boolean | null;
  is_missing?: boolean | null;
  is_upcoming_auto?: boolean | null;
  is_upcoming_final?: boolean | null;
  record_status?: string | null;
  read_date?: string | null;
  release_date?: string | null;
  publication_date?: string | null;
  book_number?: number | null;
  series_order?: number | null;
  auto_summary?: string | null;
  notes?: string | null;
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

type EditBookFormState = {
  id: number | null;
  title: string;
  author: string;
  bookNumber: string;
  status: "unread" | "upcoming" | "available" | "read";
  date: string;
};

type SeriesCheckStatusPayload = {
  session_id?: string | null;
  status: "idle" | "started" | "running" | "success" | "no_new_books" | "error" | "complete";
  progress?: number;
  current_pass?: string | null;
  elapsed_seconds?: number;
  timed_out?: boolean;
  missing_books?: Array<number | string>;
  no_new_books?: boolean;
  message?: string;
  new_books?: Array<Record<string, unknown>>;
  counters?: {
    total_books?: number;
    unread_books?: number;
    read_books?: number;
    upcoming_books?: number;
  };
  status_bar?: {
    status?: string | null;
    next_unread?: number | null;
    next_upcoming?: number | null;
    missing?: Array<number | string>;
  };
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
const TITLE_NORMALIZATION_WIZARD_MODES: TitleNormalizationWizardMode[] = ["keep_original", "clean_up", "new_clean_title", "match_other_titles", "custom"];
const CUSTOM_TITLE_PATTERN_PRESETS = [
  {
    id: "book_title_series_suffix",
    label: "Book Title + Series Suffix",
    pattern: "{book_title} ({series_name} Book {book_number})",
  },
  {
    id: "series_dash_title",
    label: "Series - Number - Title",
    pattern: "{series_name} - Book {book_number} - {book_title}",
  },
  {
    id: "title_optional_subtitle",
    label: "Title with Optional Subtitle",
    pattern: "{book_title}[[ - {book_subtitle}]]",
  },
  {
    id: "subtitle_preferred_fallback",
    label: "Prefer Subtitle, Fallback to Title",
    pattern: "[[{book_subtitle} ({series_name} Book {book_number})]] || {book_title} ({series_name} Book {book_number})",
  },
] as const;
type CustomTitlePatternPresetId = (typeof CUSTOM_TITLE_PATTERN_PRESETS)[number]["id"];

function isTitleNormalizationMode(value: unknown): value is TitleNormalizationMode {
  return typeof value === "string" && TITLE_NORMALIZATION_MODES.includes(value as TitleNormalizationMode);
}

function isTitleNormalizationWizardMode(value: unknown): value is TitleNormalizationWizardMode {
  return typeof value === "string" && TITLE_NORMALIZATION_WIZARD_MODES.includes(value as TitleNormalizationWizardMode);
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
    title = title.replace(new RegExp(`^(${escaped})\\s*:\\s*${escaped}\\s*`, "i"), "$1: ").trim();
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

function formatDate(value?: string | null) {
  if (!value) return "—";
  const normalized = toIsoDateString(value);
  let date: Date;
  if (normalized && /^\d{4}-\d{2}-\d{2}$/.test(normalized)) {
    const [year, month, day] = normalized.split("-").map(Number);
    date = new Date(year, month - 1, day);
  } else {
    date = new Date(value);
  }
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

function toIsoDateString(value: string | null | undefined): string | null {
  const raw = String(value || "").trim();
  if (!raw) return null;

  const strictIso = raw.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if (strictIso) {
    const year = strictIso[1];
    const month = strictIso[2].padStart(2, "0");
    const day = strictIso[3].padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  const normalized = normalizeDateInput(raw);
  if (normalized && /^\d{4}-\d{2}-\d{2}$/.test(normalized)) {
    return normalized;
  }

  const parsed = new Date(raw);
  if (Number.isNaN(parsed.valueOf())) {
    return null;
  }
  const year = parsed.getFullYear();
  const month = String(parsed.getMonth() + 1).padStart(2, "0");
  const day = String(parsed.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
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

function isGhostBookRecord(book: BookRecord): boolean {
  return Boolean(book?.is_missing || book?.is_upcoming_auto || book?.is_upcoming_final);
}

function isFutureDate(value?: string | null): boolean {
  if (!value) return false;
  const parsedDate = new Date(value);
  if (Number.isNaN(parsedDate.valueOf())) return false;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  parsedDate.setHours(0, 0, 0, 0);
  return parsedDate > today;
}

function isPastOrTodayDate(value?: string | null): boolean {
  if (!value) return false;
  const parsedDate = new Date(value);
  if (Number.isNaN(parsedDate.valueOf())) return false;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  parsedDate.setHours(0, 0, 0, 0);
  return parsedDate <= today;
}

function getBookStatus(book: BookRecord) {
  if (book.is_read || String(book.read_status || "").trim().toLowerCase() === "read") {
    return "read";
  }

  const explicitStatus = String(book.read_status || "").trim().toLowerCase();
  if (explicitStatus === "upcoming") {
    return "upcoming";
  }
  if (explicitStatus === "available") {
    return "available";
  }

  const releaseDate = String(book.release_date || book.publication_date || "").trim();
  if (releaseDate) {
    return isFutureDate(releaseDate) ? "upcoming" : "available";
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
  if (status === "available") {
    return "inline-flex rounded-full border border-sky-300 bg-sky-100 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-sky-800";
  }
  return "inline-flex rounded-full border border-rose-300 bg-rose-100 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-rose-800";
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

function formatBookNumberValue(value: number | null | undefined): string {
  if (!Number.isFinite(value ?? NaN)) return "";
  const numeric = Number(value);
  return Number.isInteger(numeric) ? String(Math.trunc(numeric)) : String(numeric);
}

function inferBookSubtitle(rawTitle: string): string {
  const cleanedOriginal = normalizeBookTitleCleanupOnly(rawTitle);
  const withoutSuffix = cleanedOriginal
    .replace(/\s*\([^)]*\bbook\s*\d+(?:\.\d+)?[^)]*\)\s*$/i, "")
    .trim();

  if (!withoutSuffix) return "";
  if (withoutSuffix.includes(":")) {
    return String(withoutSuffix.split(":", 2)[1] || "").trim();
  }
  if (withoutSuffix.includes(" - ")) {
    return String(withoutSuffix.split(" - ", 2)[1] || "").trim();
  }

  return "";
}

function normalizeBookTitleWithCustomPattern(
  rawTitle: string,
  customPattern: string,
  seriesName?: string,
  bookNumber?: number | null,
  bookSubtitle?: string | null,
): string {
  const fallbackTitle = normalizeBookTitleBookNameOnly(rawTitle) || String(rawTitle || "").trim();
  const pattern = String(customPattern || "").trim();
  if (!pattern) {
    return fallbackTitle;
  }

  const resolvedSubtitle = String(bookSubtitle || inferBookSubtitle(rawTitle) || "").trim();

  const replacements: Record<string, string> = {
    "{series_name}": String(seriesName || "").trim(),
    "{book_number}": formatBookNumberValue(bookNumber),
    "{book_title}": fallbackTitle,
    "{book_subtitle}": resolvedSubtitle,
    "{original_title}": String(rawTitle || "").trim(),
  };

  const candidates = pattern
    .split(/\s*\|\|\s*|\n+/)
    .map((value) => value.trim())
    .filter(Boolean);
  const normalizedCandidates = candidates.length ? candidates : [pattern];

  const renderCandidate = (template: string) => {
    let rendered = String(template || "");

    const renderOptionalBlock = (blockText: string) => {
      const tokens = Array.from(new Set(blockText.match(/\{[a-z_]+\}/g) || []));
      if (tokens.some((token) => !String(replacements[token] || "").trim())) {
        return "";
      }

      let blockRendered = blockText;
      for (const [token, value] of Object.entries(replacements)) {
        blockRendered = blockRendered.split(token).join(value);
      }
      return blockRendered;
    };

    let previous = "";
    while (previous !== rendered) {
      previous = rendered;
      rendered = rendered.replace(/\[\[([\s\S]*?)\]\]/g, (_match, block) => renderOptionalBlock(String(block || "")));
    }

    for (const [token, value] of Object.entries(replacements)) {
      rendered = rendered.split(token).join(value);
    }

    rendered = rendered.replace(/\(\s*\)/g, "");
    rendered = rendered.replace(/\[\s*\]/g, "");
    rendered = rendered.replace(/\s+([,;:.!?])/g, "$1");
    rendered = rendered.replace(/\s{2,}/g, " ");
    return rendered.trim().replace(/^[\s\-,:;]+|[\s\-,:;]+$/g, "");
  };

  let firstRendered = "";
  for (const candidate of normalizedCandidates) {
    const rendered = renderCandidate(candidate);
    if (!rendered) continue;
    if (!firstRendered) {
      firstRendered = rendered;
    }
    if (rendered !== fallbackTitle) {
      return rendered;
    }
  }

  return firstRendered || fallbackTitle;
}

function shouldExcludeUpcomingBySpec(book: BookRecord): boolean {
  const status = String(book.read_status || "").trim().toLowerCase();
  if (status !== "upcoming") {
    return false;
  }

  const publicationDate = String(book.publication_date || "").trim();
  if (!publicationDate) {
    return false;
  }

  const parsedDate = new Date(publicationDate);
  if (Number.isNaN(parsedDate.valueOf())) {
    return false;
  }

  const today = new Date();
  today.setHours(0, 0, 0, 0);
  parsedDate.setHours(0, 0, 0, 0);
  return parsedDate > today;
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

export default function SeriesDetailPage() {
  const params = useParams();
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
  const [bookSortMode, setBookSortMode] = useState<"series" | "az">("series");
  const [recentAddMessage, setRecentAddMessage] = useState<string | null>(null);
  const [seriesCheckLoading, setSeriesCheckLoading] = useState(false);
  const [seriesCheckProgress, setSeriesCheckProgress] = useState(0);
  const [seriesCheckCurrentPass, setSeriesCheckCurrentPass] = useState<string | null>(null);
  const [seriesCheckStillChecking, setSeriesCheckStillChecking] = useState(false);
  const [deleteGhostSaving, setDeleteGhostSaving] = useState(false);
  const [releaseIntelText, setReleaseIntelText] = useState("");
  const [releaseIntelSaving, setReleaseIntelSaving] = useState(false);
  const [releaseIntelMessage, setReleaseIntelMessage] = useState<string | null>(null);
  const [addBookDialogOpen, setAddBookDialogOpen] = useState(false);
  const [addBookSaving, setAddBookSaving] = useState(false);
  const [addBookTitle, setAddBookTitle] = useState("");
  const [addBookNumber, setAddBookNumber] = useState("");
  const [addBookStatus, setAddBookStatus] = useState<"upcoming" | "unread" | "available" | "read">("upcoming");
  const [addBookDate, setAddBookDate] = useState("");
  const [recentUpcomingBookIds, setRecentUpcomingBookIds] = useState<number[]>([]);
  const [titleNormalizeSaving, setTitleNormalizeSaving] = useState(false);
  const [normalizeWizardMode, setNormalizeWizardMode] = useState<TitleNormalizationWizardMode>("clean_up");
  const [normalizeCustomPattern, setNormalizeCustomPattern] = useState("{book_title} ({series_name} Book {book_number})");
  const [normalizeCustomPreset, setNormalizeCustomPreset] = useState<CustomTitlePatternPresetId>("book_title_series_suffix");
  const [normalizeExcludeUpcoming, setNormalizeExcludeUpcoming] = useState(true);
  const [releaseIntelDialogOpen, setReleaseIntelDialogOpen] = useState(false);
  const [normalizeTitlesDialogOpen, setNormalizeTitlesDialogOpen] = useState(false);
  const [knownTotalDraft, setKnownTotalDraft] = useState("");
  const [knownTotalSaving, setKnownTotalSaving] = useState(false);
  const [knownSeriesListDialogOpen, setKnownSeriesListDialogOpen] = useState(false);
  const [knownSeriesListText, setKnownSeriesListText] = useState("");
  const [knownSeriesListSaving, setKnownSeriesListSaving] = useState(false);
  const [deleteSeriesSaving, setDeleteSeriesSaving] = useState(false);
  const [editBookDialogOpen, setEditBookDialogOpen] = useState(false);
  const [savingEditBook, setSavingEditBook] = useState(false);
  const [statusDialogOpen, setStatusDialogOpen] = useState(false);
  const [statusTargetBook, setStatusTargetBook] = useState<BookRecord | null>(null);
  const [statusAction, setStatusAction] = useState<"read" | "unread" | "upcoming" | "available">("unread");
  const [statusDate, setStatusDate] = useState("");
  const [statusSaving, setStatusSaving] = useState(false);
  const [editBookForm, setEditBookForm] = useState<EditBookFormState>({
    id: null,
    title: "",
    author: "",
    bookNumber: "",
    status: "unread",
    date: "",
  });
  const [columnWidths, setColumnWidths] = useState<Record<SeriesDetailColumnKey, number>>(DEFAULT_SERIES_DETAIL_COLUMN_WIDTHS);
  const { toast } = useToast();
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
      if (addMessageTimeoutRef.current !== null) {
        window.clearTimeout(addMessageTimeoutRef.current);
      }
    };
  }, [seriesId]);

  useEffect(() => {
    const unsubscribe = subscribeBookStatusUpdates((payload) => {
      setSeries((prev) => {
        if (!prev || !Array.isArray(prev.books)) return prev;

        if (String(payload.record_status || "").toLowerCase() === "deleted") {
          const nextBooks = prev.books.filter((book) => Number(book.id) !== Number(payload.id));
          return nextBooks.length !== prev.books.length ? { ...prev, books: nextBooks } : prev;
        }

        let didChange = false;
        const nextBooks = prev.books.map((book) => {
          if (book.id !== payload.id) return book;
          didChange = true;
          return {
            ...book,
            ...payload,
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

  useEffect(() => {
    if (!series) return;
    const storedMode = series.title_normalization_mode_override;
    setNormalizeWizardMode(isTitleNormalizationMode(storedMode) ? storedMode : "keep_original");
  }, [series?.id, series?.title_normalization_mode_override]);

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
  const ghostCount = books.filter((book) => isGhostBookRecord(book)).length;
  const maxBookNumber = books.reduce((max: number, book) => {
    const num = Number(book.book_number);
    return Number.isFinite(num) ? Math.max(max, num) : max;
  }, 0);
  const titleNormalizationPreview = displayedBooks
    .map((book) => {
      const currentTitle = String(book?.title || "").trim();
      if (!currentTitle) {
        return null;
      }
      if (normalizeExcludeUpcoming && shouldExcludeUpcomingBySpec(book)) {
        return {
          id: Number(book.id),
          currentTitle,
          normalizedTitle: currentTitle,
          skipped: true,
        };
      }
      const resolvedBookNumber = Number(book?.book_number ?? book?.series_order ?? NaN);
      const normalizedTitle = normalizeWizardMode === "custom"
        ? normalizeBookTitleWithCustomPattern(
            currentTitle,
            normalizeCustomPattern,
            series?.name,
            Number.isFinite(resolvedBookNumber) ? resolvedBookNumber : null,
            String(book?.subtitle || "").trim(),
          )
        : normalizeBookTitleForMode(
            currentTitle,
            normalizeWizardMode,
            series?.name,
            Number.isFinite(resolvedBookNumber) ? resolvedBookNumber : null,
            Array.isArray(series?.books) ? series.books : [],
          );
      if (!currentTitle || !normalizedTitle || currentTitle === normalizedTitle) {
        return null;
      }
      return {
        id: Number(book.id),
        currentTitle,
        normalizedTitle,
        skipped: false,
      };
    })
    .filter(
      (value): value is { id: number; currentTitle: string; normalizedTitle: string; skipped: boolean } =>
        Boolean(value)
    );
  const titleNormalizationApplicablePreview = titleNormalizationPreview.filter((row) => !row.skipped);
  const skippedUpcomingCount = titleNormalizationPreview.filter((row) => row.skipped).length;

  const titleNormalizationOptions: Array<{
    mode: TitleNormalizationWizardMode;
    label: string;
    description: string;
    note: string;
  }> = [
    {
      mode: "keep_original",
      label: "Keep Original Title - Leave As Is",
      description: "No changes; preserves current formatting.",
      note: "Useful for legacy or manually curated titles.",
    },
    {
      mode: "clean_up",
      label: "Clean Up Title - Fix Formatting Junk",
      description: "Removes redundant punctuation, stray parentheses, and spacing.",
      note: "Ideal for imported or messy metadata.",
    },
    {
      mode: "new_clean_title",
      label: "New Clean Title - Keep Book Name, Add Clean Series Suffix",
      description: "Rebuilds titles with consistent series suffix formatting.",
      note: "Great for mixed-source consistency.",
    },
    {
      mode: "match_other_titles",
      label: "Match Other Titles - Format Like the Rest of the Series",
      description: "Detects the dominant series pattern and applies it.",
      note: "Best for aligning inconsistent entries.",
    },
    {
      mode: "custom",
      label: "Other (Custom)",
      description: "Use templates with optional blocks and fallback patterns.",
      note: "Supports [[optional blocks]] and fallbacks with ||.",
    },
  ];

  const titleNormalizationExamplesByMode = (() => {
    const samples = displayedBooks.filter((book) => String(book?.title || "").trim()).slice(0, 3);
    const byMode = new Map<TitleNormalizationWizardMode, Array<{ before: string; after: string }>>();

    for (const option of titleNormalizationOptions) {
      const rows = samples.map((book) => {
        const before = String(book?.title || "").trim();
        const resolvedBookNumber = Number(book?.book_number ?? book?.series_order ?? NaN);
        const after = option.mode === "custom"
          ? normalizeBookTitleWithCustomPattern(
              before,
              normalizeCustomPattern,
              series?.name,
              Number.isFinite(resolvedBookNumber) ? resolvedBookNumber : null,
              String(book?.subtitle || "").trim(),
            )
          : normalizeBookTitleForMode(
              before,
              option.mode,
              series?.name,
              Number.isFinite(resolvedBookNumber) ? resolvedBookNumber : null,
              Array.isArray(series?.books) ? series.books : [],
            );

        return { before, after: after || before };
      });

      byMode.set(option.mode, rows);
    }

    return byMode;
  })();

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

      while (statusPayload.status === "running" || statusPayload.status === "started") {
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
          statusPayload = { ...statusPayload, status: "no_new_books", no_new_books: true };
        }
      }

      if (statusPayload.error) {
        throw new Error(statusPayload.error);
      }

      const data = statusPayload.result ?? {};
      const contractNewBooks = Array.isArray(statusPayload.new_books) ? statusPayload.new_books : [];
      const addedCount = contractNewBooks.length > 0
        ? contractNewBooks.length
        : Array.isArray(data.added_books)
          ? data.added_books.length
          : 0;
      const missingList = Array.isArray(statusPayload.missing_books)
        ? statusPayload.missing_books
        : Array.isArray(data.missing_books)
          ? data.missing_books
          : [];
      const message = statusPayload.status === "success"
        ? "NEW BOOKS found and added to library."
        : statusPayload.status === "no_new_books"
          ? "NO NEW BOOKS FOUND."
          : statusPayload.message || (missingList.length > 0 ? `Missing books: ${missingList.join(", ")}.` : "NO NEW BOOKS FOUND.");

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

  async function handleDeleteGhostBooks() {
    if (!series) return;

    const ghostBooks = books.filter(isGhostBookRecord);
    if (!ghostBooks.length) {
      alert("No ghost books to delete.");
      return;
    }

    const confirmed = window.confirm(
      `Delete ${ghostBooks.length} ghost book${ghostBooks.length === 1 ? "" : "s"}? This keeps them permanently removed from check-for-new.`
    );
    if (!confirmed) {
      return;
    }

    setDeleteGhostSaving(true);
    try {
      const response = await fetchApiWithFallback(`/series/${series.id}/delete_ghost_books`, {
        method: "POST",
      });

      if (!response.ok) {
        throw new Error(`Failed to delete ghost books (${response.status})`);
      }

      const result = await response.json();
      const deletedBooks = Array.isArray(result?.deleted_books) ? result.deleted_books : [];
      const deletedIds = new Set<number>(
        deletedBooks
          .map((entry: { id?: unknown }) => Number(entry?.id))
          .filter((value: number) => Number.isFinite(value))
      );

      setSeries((prev) => {
        if (!prev || !Array.isArray(prev.books)) return prev;
        return {
          ...prev,
          books: prev.books.filter((book) => !deletedIds.has(Number(book.id))),
        };
      });

      for (const ghostBook of ghostBooks) {
        if (!deletedIds.has(Number(ghostBook.id))) {
          continue;
        }
        publishBookStatusUpdate({
          id: Number(ghostBook.id),
          record_status: "deleted",
          series_id: series.id,
        });
      }

      await refreshSeriesFromApi();
      const deletedCount = Number(result?.deleted_count || deletedIds.size || 0);
      const summary = `Deleted ${deletedCount} ghost book${deletedCount === 1 ? "" : "s"}.`;
      flashAddedMessage(summary);
      toast({ title: "Ghost cleanup complete", description: summary });
    } catch (error) {
      console.error(error);
      alert(error instanceof Error ? error.message : "Unable to delete ghost books right now.");
    } finally {
      setDeleteGhostSaving(false);
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

  async function handleDeleteSeriesWithBooks() {
    if (!series) return;

    const visibleBookCount = Array.isArray(series.books) ? series.books.length : 0;
    const confirmed = window.confirm(
      `Delete series "${series.name}" and all books in it? This permanently removes the series and its books from Library.`
    );
    if (!confirmed) {
      return;
    }

    setDeleteSeriesSaving(true);
    try {
      const response = await fetchApiWithFallback(`/series/${series.id}`, {
        method: "DELETE",
      });

      if (!response.ok) {
        let detail = "";
        try {
          const data = await response.json();
          detail = data?.detail ? ` - ${data.detail}` : "";
        } catch {
          // ignore response parse errors
        }
        throw new Error(`Failed to delete series (${response.status})${detail}`);
      }

      let deletedBooks = visibleBookCount;
      try {
        const result = await response.json();
        const candidate = Number(result?.deleted_books);
        if (Number.isFinite(candidate)) {
          deletedBooks = candidate;
        }
      } catch {
        // ignore response parse errors
      }

      alert(`Deleted series "${series.name}" and ${deletedBooks} book${deletedBooks === 1 ? "" : "s"}.`);
      window.location.href = viewAllSeriesHref;
    } catch (error) {
      console.error(error);
      alert(error instanceof Error ? error.message : "Unable to delete this series right now.");
    } finally {
      setDeleteSeriesSaving(false);
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

  async function handleEditBookTitle(book: BookRecord) {
    const status = (getBookStatus(book) as "unread" | "upcoming" | "available" | "read") || "unread";
    setEditBookForm({
      id: Number(book.id),
      title: String(book.title || ""),
      author: String(book.author || ""),
      bookNumber: book.book_number !== null && book.book_number !== undefined ? String(book.book_number) : "",
      status,
      date: toIsoDateString(getBookDate(book)) || "",
    });
    setEditBookDialogOpen(true);
  }

  async function handleSaveBookEdit() {
    const bookId = Number(editBookForm.id);
    if (!Number.isFinite(bookId) || bookId <= 0) return;

    const title = editBookForm.title.trim();
    const author = editBookForm.author.trim();
    if (!title || !author) {
      alert("Title and author are required.");
      return;
    }

    const numberRaw = editBookForm.bookNumber.trim();
    const parsedBookNumber = numberRaw ? Number(numberRaw) : null;
    if (numberRaw && !Number.isFinite(parsedBookNumber)) {
      alert("Book number must be numeric when provided.");
      return;
    }

    const rawDate = editBookForm.date.trim();
    const normalizedDate = rawDate ? toIsoDateString(rawDate) : null;
    if (rawDate && !normalizedDate) {
      alert("Use a valid date format, such as YYYY-MM-DD.");
      return;
    }

    setSavingEditBook(true);
    try {
      const status = editBookForm.status;
      const payload: Record<string, unknown> = {
        title,
        author,
        series_id: Number(series?.id),
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
      setSeries((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          books: Array.isArray(prev.books)
            ? prev.books.map((item) => (item.id === updatedBook.id ? { ...item, ...updatedBook } : item))
            : prev.books,
        };
      });
      publishBookStatusUpdate(updatedBook);
      setEditBookDialogOpen(false);
      flashAddedMessage(`Book updated: ${updatedBook.title || title}`);
      toast({
        title: "Book updated",
        description: "Changes were saved and reflected in the Library view.",
      });
    } catch (error) {
      console.error(error);
      alert(error instanceof Error ? error.message : "Unable to update book right now.");
    } finally {
      setSavingEditBook(false);
    }
  }

  async function handleApplyTitleNormalization() {
    if (!series) {
      return;
    }

    if (!isTitleNormalizationWizardMode(normalizeWizardMode)) {
      alert("Please select a normalization mode.");
      return;
    }

    if (normalizeWizardMode === "custom" && !String(normalizeCustomPattern || "").trim()) {
      alert("Enter a custom pattern before applying.");
      return;
    }

    if (!titleNormalizationApplicablePreview.length) {
      alert("No eligible title changes to apply for the selected mode.");
      return;
    }

    setTitleNormalizeSaving(true);
    try {
      const response = await fetchApiWithFallback(`/series/${series.id}/normalize_titles`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          normalization_mode: normalizeWizardMode,
          custom_pattern: normalizeWizardMode === "custom" ? normalizeCustomPattern : undefined,
          exclude_upcoming: normalizeExcludeUpcoming,
        }),
      });

      if (!response.ok) {
        throw new Error(`Failed to normalize titles (${response.status})`);
      }

      const result = await response.json();
      const updatedCount = Number(result?.updated_count || 0);
      const skippedCount = Number(result?.skipped_upcoming_count || 0);
      const diagnostics = ((result as any)?.normalization_diagnostics ?? null) as any;
      const unchangedCount = Number(diagnostics?.unchanged_count ?? 0);
      const consideredCount = Number(diagnostics?.considered_count ?? 0);

      // Broadcast normalized title updates so the Main Library view stays in sync.
      const updatedBooks = Array.isArray((result as any)?.updated_books)
        ? ((result as any).updated_books as any[])
        : [];
      const currentSeriesBooks = Array.isArray(series?.books) ? series.books : [];
      const booksById = new Map(currentSeriesBooks.map((book) => [Number(book.id), book]));

      for (const row of updatedBooks) {
        const bookId = Number((row as any)?.id);
        const normalizedTitle = typeof (row as any)?.to === "string" ? String((row as any).to).trim() : "";
        if (!Number.isFinite(bookId) || !normalizedTitle) {
          continue;
        }

        const existing = booksById.get(bookId);
        if (!existing) {
          continue;
        }

        publishBookStatusUpdate({
          id: bookId,
          is_read: Boolean(existing.is_read),
          read_status: String(existing.read_status || (existing.is_read ? "read" : "unread")),
          read_date: existing.read_date ?? null,
          release_date: existing.release_date ?? null,
          publication_date: existing.publication_date ?? null,
          series_id: existing.series_id ?? series.id,
          title: normalizedTitle,
          author: existing.author ?? null,
          book_number: typeof existing.book_number === "number" ? existing.book_number : null,
          series_order: typeof existing.series_order === "number" ? existing.series_order : null,
          series_name: series.name,
        });
      }

      await refreshSeriesFromApi();
      setNormalizeTitlesDialogOpen(false);

      const summaryParts = [
        `Normalized ${updatedCount} title${updatedCount === 1 ? "" : "s"}`,
      ];
      if (consideredCount > 0) {
        summaryParts.push(`considered ${consideredCount}`);
      }
      if (unchangedCount > 0) {
        summaryParts.push(`unchanged ${unchangedCount}`);
      }
      if (skippedCount > 0) {
        summaryParts.push(`skipped upcoming ${skippedCount}`);
      }
      const summary = `${summaryParts.join("; ")}.`;
      flashAddedMessage(summary);
      toast({ title: "Title normalization applied", description: summary });
    } catch (error) {
      console.error(error);
      alert(error instanceof Error ? error.message : "Unable to apply title normalization right now.");
    } finally {
      setTitleNormalizeSaving(false);
    }
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
      await refreshSeriesFromApi();
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

        await refreshSeriesFromApi();
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
      flashAddedMessage(`Deleted book #${book.book_number ?? book.id}.`);
    } catch (error) {
      console.error(error);
      alert(error instanceof Error ? error.message : "Unable to delete book right now.");
    }
  }

  async function handleSetBookStatus(book: BookRecord) {
    const currentStatus = (getBookStatus(book) as "read" | "unread" | "upcoming" | "available") || "unread";
    setStatusTargetBook(book);
    setStatusAction(currentStatus);
    setStatusDate(toIsoDateString(currentStatus === "read" ? book.read_date : (book.release_date || book.publication_date)) || "");
    setStatusDialogOpen(true);
  }

  async function handleSaveBookStatus() {
    if (!statusTargetBook) return;

    setStatusSaving(true);
    try {
      const todayIso = new Date().toISOString().split("T")[0];
      const normalizedDate = statusDate.trim() ? toIsoDateString(statusDate) : null;
      if (statusDate.trim() && !normalizedDate) {
        alert("Use a valid date format, such as YYYY-MM-DD.");
        return;
      }

      const payload: Record<string, unknown> = {
        is_read: statusAction === "read",
        read_status: statusAction,
      };

      if (statusAction === "read") {
        payload.read_date = normalizedDate || todayIso;
        payload.release_date = null;
      } else if (statusAction === "unread") {
        payload.read_date = null;
      } else if (statusAction === "upcoming") {
        payload.read_date = null;
        payload.release_date = normalizedDate || toIsoDateString(statusTargetBook.release_date || statusTargetBook.publication_date) || null;
      } else if (statusAction === "available") {
        payload.read_date = null;
        const existingDate = toIsoDateString(statusTargetBook.release_date || statusTargetBook.publication_date);
        if (!existingDate || isPastOrTodayDate(existingDate)) {
          payload.release_date = null;
        }
      }

      const effectiveReleaseDate = String(payload.release_date || "").trim() || toIsoDateString(statusTargetBook.release_date || statusTargetBook.publication_date);
      if (!payload.read_date && effectiveReleaseDate) {
        if (isFutureDate(effectiveReleaseDate)) {
          payload.read_status = "upcoming";
          payload.is_read = false;
          payload.release_date = effectiveReleaseDate;
        } else if (isPastOrTodayDate(effectiveReleaseDate)) {
          payload.read_status = "available";
          payload.is_read = false;
          payload.release_date = null;
        }
      }

      const response = await fetchApiWithFallback(`/books/${statusTargetBook.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
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
      await refreshSeriesFromApi();
      setStatusDialogOpen(false);
      setStatusTargetBook(null);
      flashAddedMessage(`Status updated for ${updatedBook.title || "book"}.`);
    } catch (err) {
      console.error(err);
      alert("Unable to update status right now.");
    } finally {
      setStatusSaving(false);
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
      } else if (result?.is_finished) {
        flashAddedMessage("Series moved to finished.");
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
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => void handleDeleteGhostBooks()}
                disabled={deleteGhostSaving || ghostCount <= 0}
              >
                {deleteGhostSaving
                  ? "Deleting ghosts..."
                  : ghostCount > 0
                    ? `Delete Ghost Books (${ghostCount})`
                    : "Delete Ghost Books"}
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
                onClick={() => setReleaseIntelDialogOpen(true)}
              >
                Paste Series Intel
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => {
                  setNormalizeWizardMode(seriesNormalizationMode);
                  setNormalizeTitlesDialogOpen(true);
                }}
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
            <Button
              type="button"
              variant="destructive"
              onClick={() => void handleDeleteSeriesWithBooks()}
              disabled={deleteSeriesSaving}
            >
              {deleteSeriesSaving ? "Deleting series..." : "Delete series + books"}
            </Button>
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
                    size="sm"
                    onClick={() => handleSetBookStatus(book)}
                  >
                    Set Status
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => handleEditBookTitle(book)}
                  >
                    Edit book
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
                  onChange={(event) => setAddBookStatus(event.target.value as "upcoming" | "unread" | "available" | "read")}
                  className="h-9 w-full rounded border bg-white px-2 text-sm"
                >
                  <option value="upcoming">upcoming</option>
                  <option value="unread">unread</option>
                  <option value="available">available</option>
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

      <Dialog open={statusDialogOpen} onOpenChange={setStatusDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Set Status</DialogTitle>
            <DialogDescription>
              Update book state with automatic date-based inference.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-3">
            <div className="space-y-1">
              <Label htmlFor="series-status-action">Action</Label>
              <select
                id="series-status-action"
                value={statusAction}
                onChange={(event) =>
                  setStatusAction(event.target.value as "read" | "unread" | "upcoming" | "available")
                }
                className="h-9 w-full rounded border bg-white px-2 text-sm"
              >
                <option value="read">Mark as Read</option>
                <option value="unread">Mark as Unread</option>
                <option value="upcoming">Mark as Upcoming</option>
                <option value="available">Mark as Available</option>
              </select>
            </div>

            <div className="space-y-1">
              <Label htmlFor="series-status-date">
                {statusAction === "read" ? "Date Read" : "Publication Date (optional)"}
              </Label>
              <input
                id="series-status-date"
                value={statusDate}
                onChange={(event) => setStatusDate(event.target.value)}
                placeholder="YYYY-MM-DD"
                className="h-9 w-full rounded border bg-white px-2 text-sm"
              />
            </div>
          </div>

          <DialogFooter showCloseButton>
            <Button type="button" onClick={handleSaveBookStatus} disabled={statusSaving}>
              {statusSaving ? "Saving..." : "Save status"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={editBookDialogOpen}
        onOpenChange={setEditBookDialogOpen}
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Edit Book</DialogTitle>
            <DialogDescription>
              Update title, author, number, status, and date for this book.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-3">
            <div className="space-y-1">
              <Label htmlFor="edit-book-title">Title</Label>
              <input
                id="edit-book-title"
                value={editBookForm.title}
                onChange={(event) => setEditBookForm((prev) => ({ ...prev, title: event.target.value }))}
                placeholder="Book title"
                className="h-9 w-full rounded border bg-white px-2 text-sm"
              />
            </div>

            <div className="space-y-1">
              <Label htmlFor="edit-book-author">Author</Label>
              <input
                id="edit-book-author"
                value={editBookForm.author}
                onChange={(event) => setEditBookForm((prev) => ({ ...prev, author: event.target.value }))}
                placeholder="Author name"
                className="h-9 w-full rounded border bg-white px-2 text-sm"
              />
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <Label htmlFor="edit-book-number">Book #</Label>
                <input
                  id="edit-book-number"
                  value={editBookForm.bookNumber}
                  onChange={(event) => setEditBookForm((prev) => ({ ...prev, bookNumber: event.target.value }))}
                  placeholder="e.g. 24"
                  className="h-9 w-full rounded border bg-white px-2 text-sm"
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
                      status: event.target.value as "unread" | "upcoming" | "available" | "read",
                    }))
                  }
                  className="h-9 w-full rounded border bg-white px-2 text-sm"
                >
                  <option value="unread">unread</option>
                  <option value="upcoming">upcoming</option>
                  <option value="available">available</option>
                  <option value="read">read</option>
                </select>
              </div>
            </div>

            <div className="space-y-1">
              <Label htmlFor="edit-book-date">Date</Label>
              <input
                id="edit-book-date"
                value={editBookForm.date}
                onChange={(event) => setEditBookForm((prev) => ({ ...prev, date: event.target.value }))}
                placeholder={editBookForm.status === "read" ? "Read date (YYYY-MM-DD)" : "Release date (YYYY-MM-DD)"}
                className="h-9 w-full rounded border bg-white px-2 text-sm"
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
        onOpenChange={setNormalizeTitlesDialogOpen}
      >
        <DialogContent className="max-h-[92vh] overflow-y-auto sm:max-w-5xl">
          <DialogHeader>
            <DialogTitle>Title Normalization Wizard</DialogTitle>
            <DialogDescription>
              Pick a mode, review real examples from this series, then apply once with Accept Changes.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-3">
            <div className="grid gap-2 md:grid-cols-2">
              {titleNormalizationOptions.map((option) => {
                const selected = normalizeWizardMode === option.mode;
                const sampleRows = titleNormalizationExamplesByMode.get(option.mode) || [];
                return (
                  <button
                    key={option.mode}
                    type="button"
                    className={`rounded border p-3 text-left ${selected ? "border-emerald-500 bg-emerald-50" : "border-slate-200 bg-white hover:border-slate-300"}`}
                    onClick={() => setNormalizeWizardMode(option.mode)}
                  >
                    <div className="mb-1 flex items-center justify-between gap-2">
                      <p className="text-sm font-semibold text-foreground">{option.label}</p>
                      {selected ? <span className="text-xs font-semibold text-emerald-700">Selected</span> : null}
                    </div>
                    <p className="text-xs text-muted-foreground">{option.description}</p>
                    <p className="mt-1 text-xs text-muted-foreground">{option.note}</p>
                    <div className="mt-2 space-y-1 rounded border bg-slate-50 p-2">
                      {sampleRows.length > 0 ? (
                        sampleRows.map((row, index) => (
                          <div key={`${option.mode}-${index}`} className="grid grid-cols-[1fr_auto_1fr] gap-1 text-xs">
                            <span className="truncate text-slate-700">{row.before}</span>
                            <span className="text-slate-400" aria-hidden="true">-&gt;</span>
                            <span className="truncate text-emerald-700">{row.after}</span>
                          </div>
                        ))
                      ) : (
                        <p className="text-xs text-muted-foreground">No sample titles available.</p>
                      )}
                    </div>
                  </button>
                );
              })}
            </div>

            {normalizeWizardMode === "custom" ? (
              <div className="space-y-1 rounded border bg-slate-50 p-3">
                <Label htmlFor="normalize-custom-preset">Custom style preset</Label>
                <select
                  id="normalize-custom-preset"
                  value={normalizeCustomPreset}
                  onChange={(event) => {
                    const selectedPreset = CUSTOM_TITLE_PATTERN_PRESETS.find((preset) => preset.id === event.target.value);
                    if (!selectedPreset) return;
                    setNormalizeCustomPreset(selectedPreset.id);
                    setNormalizeCustomPattern(selectedPreset.pattern);
                  }}
                  className="h-9 w-full rounded border bg-white px-2 text-sm"
                >
                  {CUSTOM_TITLE_PATTERN_PRESETS.map((preset) => (
                    <option key={preset.id} value={preset.id}>{preset.label}</option>
                  ))}
                </select>
                <Label htmlFor="normalize-custom-pattern">Custom pattern</Label>
                <input
                  id="normalize-custom-pattern"
                  value={normalizeCustomPattern}
                  onChange={(event) => setNormalizeCustomPattern(event.target.value)}
                  className="h-9 w-full rounded border bg-white px-2 text-sm"
                  placeholder="{book_title} ({series_name} Book {book_number})"
                />
                <p className="text-xs text-muted-foreground">
                  Tokens: {"{book_title}"}, {"{book_subtitle}"}, {"{series_name}"}, {"{book_number}"}, {"{original_title}"}
                </p>
                <p className="text-xs text-muted-foreground">
                  Optional blocks: [[ ... ]] only render when all tokens inside have values. Multiple templates: separate with || and the first meaningful result is used.
                </p>
              </div>
            ) : null}

            <label className="flex items-start gap-2 rounded border bg-slate-50 px-3 py-2 text-xs text-muted-foreground">
              <input
                type="checkbox"
                checked={normalizeExcludeUpcoming}
                onChange={(event) => setNormalizeExcludeUpcoming(event.target.checked)}
                className="mt-0.5"
              />
              <span>Exclude UPCOMING books with publication_date in the future.</span>
            </label>
          </div>

          {titleNormalizationPreview.length > 0 ? (
            <div className="max-h-[38vh] overflow-auto rounded border bg-white text-xs sm:max-h-[30rem]">
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
                    {row.skipped ? (
                      <p className="truncate font-medium text-amber-700">Skipped (upcoming + future publication)</p>
                    ) : (
                      <p className="truncate font-medium text-emerald-700">{row.normalizedTitle}</p>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              No title normalization changes are needed for this selection.
            </p>
          )}

          <div className="rounded border bg-slate-50 px-3 py-2 text-xs text-muted-foreground">
            Ready to apply: {titleNormalizationApplicablePreview.length} change{titleNormalizationApplicablePreview.length === 1 ? "" : "s"}
            {skippedUpcomingCount > 0 ? ` • Skipped upcoming: ${skippedUpcomingCount}` : ""}
          </div>

          <DialogFooter showCloseButton>
            <Button
              type="button"
              variant="secondary"
              onClick={handleApplyTitleNormalization}
              disabled={titleNormalizeSaving || titleNormalizationApplicablePreview.length === 0}
            >
              {titleNormalizeSaving ? "Applying..." : `Accept Changes (${titleNormalizationApplicablePreview.length})`}
            </Button>
          </DialogFooter>
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

    </div>
  );
}
