"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { AlertTriangleIcon } from "lucide-react";
import { BookActionIcon } from "@/components/books/book-action-icon";
import { publishBookStatusUpdate, subscribeBookStatusUpdates } from "@/lib/book-status-sync";
import { scheduleSeriesCheckReset } from "@/lib/series-check-progress";
import { refreshNotificationsBadgeCount } from "@/lib/notifications-badge";
import { fetchApiWithFallback } from "@/lib/api-client";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useToast } from "@/components/ui/use-toast";
import { useAuth } from "@/lib/auth-context";
import {
  formatDate,
  getFindPublicationDateUrl,
  getStatusChipClass,
  getUnifiedBookStatus,
  hasUnconfirmedReleaseDate,
} from "@/lib/book-format";
import { ConfirmDialog, type ConfirmDialogState } from "@/components/confirm-dialog";
import { AddBookDialog } from "@/components/books/add-book-dialog";
import { EditBookDialog } from "@/components/books/edit-book-dialog";
import { BookSummaryDialog } from "@/components/series/book-summary-dialog";
import { NormalizeTitlesDialog } from "@/components/series/normalize-titles-dialog";
import { MoreByAuthorDialog } from "@/components/books/more-by-author-dialog";
import { MobileSeriesBookList } from "@/components/series/mobile-series-book-list";
import { SeriesDetailHeader } from "@/components/series/series-detail-header";
import { useDeviceClass } from "@/hooks/use-device-class";
import { getDeviceClass } from "@/lib/device-class";
import {
  parseNeedsDates,
  parsePositiveId,
  parseSeriesBookSort,
  seriesAddBookHref,
  seriesDetailPath,
  seriesEditBookHref,
  withPin,
  type SeriesBookSort,
  type SeriesViewState,
} from "@/lib/return-to";

 type TitleNormalizationMode = "keep_original" | "clean_up" | "new_clean_title" | "match_other_titles";
type TitleNormalizationWizardMode = TitleNormalizationMode | "custom";

type BookRecord = {
  id: number;
  title?: string | null;
  subtitle?: string | null;
  author?: string | null;
  read_status?: string | null;
  is_read?: boolean | null;
  availability_status?: string | null;
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
  source_url?: string | null;
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

type NormalizeTitlesResponse = {
  updated_count?: number;
  skipped_upcoming_count?: number;
  normalization_diagnostics?: {
    unchanged_count?: number;
    considered_count?: number;
  } | null;
  updated_books?: Array<{ id?: number; from?: string; to?: string }>;
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
  // New inserts (excluding upcoming-only ones) plus upcoming->available
  // transitions for this run -- the same number the durable per-series
  // notification row gets, so the ephemeral popup and the Notifications
  // view can never disagree. See services/series_check_engine.py.
  discovery_delta_count?: number;
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
/** How long a book stays visually highlighted after `pin` scrolls it into view. */
const PIN_HIGHLIGHT_DURATION_MS = 2600;
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
    id: "title_with_subtitle",
    label: "Title with Subtitle",
    pattern: "{book_title} - {book_subtitle}",
  },
] as const;
type CustomTitlePatternPresetId = (typeof CUSTOM_TITLE_PATTERN_PRESETS)[number]["id"];

function isTitleNormalizationMode(value: unknown): value is TitleNormalizationMode {
  return typeof value === "string" && TITLE_NORMALIZATION_MODES.includes(value as TitleNormalizationMode);
}

function isTitleNormalizationWizardMode(value: unknown): value is TitleNormalizationWizardMode {
  return typeof value === "string" && TITLE_NORMALIZATION_WIZARD_MODES.includes(value as TitleNormalizationWizardMode);
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

  // Collapse generic marketing-blurb subtitles that mention "LitRPG" with
  // filler descriptor words on either side (e.g. "An Epic Fantasy LitRPG
  // Adventure", "A LitRPG Apocalypse", "LitRPG Novel") down to ": A LitRPG".
  // Uses a lookahead for the trailing "(Series Name Book #)"/end-of-string
  // boundary instead of consuming it, so it still fires when that suffix
  // follows -- which is the common case for real titles, not the rare one.
  title = title
    .replace(
      /:\s*((?:a|an)\s+)?(?:(?:epic|fantasy|adventures?|novels?|sagas?|apocalyptic|apocalypse|progression(?:\s+fantasy)?)\s+)*litrpg(?:\s+(?:adventures?|novels?|sagas?|apocalyptic|apocalypse|epic|fantasy|progression(?:\s+fantasy)?))*:?(?=\s*(?:\([^)]*\))?\s*$)/i,
      (_match, article) => (article ? ": A LitRPG" : ": LitRPG")
    )
    .trim();

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

function delay(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

// Delegates to the shared is_read + availability_status derivation (see
// book-format.ts's getUnifiedBookStatus docstring) -- Phase 3 of the
// "Two-Axis Status Architecture" design removed this view's own fallback
// heuristics (release-date blind inference, the "tbr"/"to be read"
// read_status string-matching in the now-deleted hasUpcomingBookSignals)
// as the source for this badge, since read_status guessing is no longer
// the authoritative source of truth once availability_status is reliably
// populated on every row.
function getBookStatus(book: BookRecord) {
  return getUnifiedBookStatus(book);
}

function getBookDate(book: BookRecord) {
  const status = getBookStatus(book);
  // release_date and publication_date are two separate stored fields (e.g.
  // Hardcover-sourced candidates populate publication_date, not
  // release_date) -- getBookStatus already falls back to publication_date
  // when deciding upcoming vs. available, so the displayed date needs the
  // same fallback or a book can be correctly classified "available" from
  // its publication_date while still showing a blank date column.
  const releaseOrPublicationDate = book.release_date || book.publication_date;
  return status === "upcoming" ? releaseOrPublicationDate || book.read_date : book.read_date || releaseOrPublicationDate;
}

function escapeRegExp(value: string): string {
  return String(value || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
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

  let rendered = pattern;
  for (const [token, value] of Object.entries(replacements)) {
    rendered = rendered.split(token).join(value);
  }

  // Cleans up artifacts left behind when a token (most often
  // {book_subtitle} or {series_name}) substitutes to an empty string --
  // e.g. "Title - ", "Title ()", or "Title ( Book 2)" -- without requiring
  // conditional template syntax.
  rendered = rendered.replace(/\(\s+/g, "(");
  rendered = rendered.replace(/\(\s*\)/g, "");
  rendered = rendered.replace(/\s+([,;:.!?])/g, "$1");
  rendered = rendered.replace(/\s{2,}/g, " ");
  rendered = rendered.trim().replace(/^[\s\-,:;]+|[\s\-,:;]+$/g, "");

  return rendered || fallbackTitle;
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
  const { role } = useAuth();
  const canEdit = role === "owner";
  const deviceClass = useDeviceClass();
  // Phone and tablet share the compact card surface, matching how Add/Edit
  // Book already route by device class rather than by viewport width alone.
  const isCompact = deviceClass !== "desktop";
  const router = useRouter();
  const params = useParams();
  const searchParams = useSearchParams();
  const seriesId = params.seriesId as string;
  const fromView = searchParams.get("fromView") === "finished" ? "finished" : "ongoing";
  const pinnedBookId = parsePositiveId(searchParams.get("pin"));
  const bookSortMode = parseSeriesBookSort(searchParams.get("sort"));
  const needsVerificationOnly = parseNeedsDates(searchParams.get("needsDates"));
  const viewAllSeriesHref = `/series?view=${fromView}`;
  // Everything except `pin`, which is a one-shot reveal rather than durable
  // view state and so is deliberately excluded from the returnTo round trip.
  const viewState = useMemo<SeriesViewState>(
    () => ({ fromView, sort: bookSortMode, needsDates: needsVerificationOnly }),
    [fromView, bookSortMode, needsVerificationOnly],
  );
  const [series, setSeries] = useState<SeriesRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [finishedToggleSaving, setFinishedToggleSaving] = useState(false);
  const [confirmDialog, setConfirmDialog] = useState<ConfirmDialogState | null>(null);
  const [summaryEditorBook, setSummaryEditorBook] = useState<BookRecord | null>(null);
  const [summaryDraft, setSummaryDraft] = useState("");
  const [notesDraft, setNotesDraft] = useState("");
  const [summarySaving, setSummarySaving] = useState(false);
  const [moreByAuthorTarget, setMoreByAuthorTarget] = useState<string | null>(null);
  const [highlightedBookId, setHighlightedBookId] = useState<number | null>(null);
  const [recentAddMessage, setRecentAddMessage] = useState<string | null>(null);
  const [seriesCheckLoading, setSeriesCheckLoading] = useState(false);
  const [seriesCheckProgress, setSeriesCheckProgress] = useState(0);
  const [seriesCheckCurrentPass, setSeriesCheckCurrentPass] = useState<string | null>(null);
  const [seriesCheckStillChecking, setSeriesCheckStillChecking] = useState(false);
  const [addBookDialogOpen, setAddBookDialogOpen] = useState(false);
  const [recentUpcomingBookIds, setRecentUpcomingBookIds] = useState<number[]>([]);
  const [titleNormalizeSaving, setTitleNormalizeSaving] = useState(false);
  const [normalizeWizardMode, setNormalizeWizardMode] = useState<TitleNormalizationWizardMode>("clean_up");
  const [normalizeCustomPattern, setNormalizeCustomPattern] = useState("{book_title} ({series_name} Book {book_number})");
  const [normalizeCustomPreset, setNormalizeCustomPreset] = useState<CustomTitlePatternPresetId>("book_title_series_suffix");
  const [normalizeExcludeUpcoming, setNormalizeExcludeUpcoming] = useState(true);
  const [normalizeTitlesDialogOpen, setNormalizeTitlesDialogOpen] = useState(false);
  const [deleteSeriesSaving, setDeleteSeriesSaving] = useState(false);
  const [editBookDialogOpen, setEditBookDialogOpen] = useState(false);
  const [editBookId, setEditBookId] = useState<number | null>(null);
  const [columnWidths, setColumnWidths] = useState<Record<SeriesDetailColumnKey, number>>(DEFAULT_SERIES_DETAIL_COLUMN_WIDTHS);
  const { toast } = useToast();
  const addMessageTimeoutRef = useRef<number | null>(null);
  const seriesCheckResetTimeoutRef = useRef<number | null>(null);
  const booksTableWrapRef = useRef<HTMLDivElement | null>(null);
  const consumedPinRef = useRef<number | null>(null);
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
    const storedMode = series.title_normalization_mode_override;
    // Seeds the wizard's mode from the series' saved preference whenever a
    // different series loads, while still letting the user freely change
    // it afterward via the dialog -- can't be plain derived-during-render
    // state since it needs to stay overridable after this initial sync.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setNormalizeWizardMode(isTitleNormalizationMode(storedMode) ? storedMode : "keep_original");
    // Deliberately scoped to just id + the override field (not all of
    // `series`) so this doesn't re-run -- and stomp the user's in-progress
    // wizard selection -- on every unrelated series update.
    // eslint-disable-next-line react-hooks/exhaustive-deps
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

  // `pin` reveals the book the user just added or edited by scrolling to it
  // in its natural position, rather than hoisting it to the top of the list
  // and leaving the ordering scrambled. It is consumed exactly once and then
  // dropped from the URL so it can't resurface on a later back-navigation.
  useEffect(() => {
    if (!pinnedBookId) {
      consumedPinRef.current = null;
      return;
    }
    if (loading || consumedPinRef.current === pinnedBookId) {
      return;
    }

    consumedPinRef.current = pinnedBookId;

    const target = document.querySelector<HTMLElement>(`[data-book-id="${pinnedBookId}"]`);
    if (target) {
      const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      target.scrollIntoView({ block: "center", behavior: prefersReducedMotion ? "auto" : "smooth" });
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setHighlightedBookId(pinnedBookId);
    }

    router.replace(seriesDetailPath(Number(seriesId), viewState), { scroll: false });
  }, [loading, pinnedBookId, router, seriesId, viewState]);

  useEffect(() => {
    if (highlightedBookId === null) return;
    const timeoutId = window.setTimeout(() => setHighlightedBookId(null), PIN_HIGHLIGHT_DURATION_MS);
    return () => window.clearTimeout(timeoutId);
  }, [highlightedBookId]);

  if (loading) {
    return <div className="p-6">Loading series...</div>;
  }

  if (error) {
    return <div className="p-6 text-red-600">{error}</div>;
  }

  if (!series) {
    return <div className="p-6">Series not found.</div>;
  }

  // Survives `pin` being cleared from the URL, so a book revealed by the
  // add/edit round trip stays put for the length of its highlight.
  const revealedBookId = highlightedBookId ?? pinnedBookId;

  const orderedBooks = (() => {
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

  const displayedBooks = (() => {
    const visible = orderedBooks.filter(
      (book) => !needsVerificationOnly || hasUnconfirmedReleaseDate(getBookStatus(book), book),
    );
    if (!revealedBookId || visible.some((book) => Number(book?.id) === revealedBookId)) {
      return visible;
    }

    // An edit can push a book out of the active filter (e.g. confirming its
    // release date clears "needs verification"). Keeping it in its natural
    // position gives the reveal something to scroll to instead of the book
    // silently vanishing the moment it was saved.
    const visibleIds = new Set(visible.map((book) => Number(book?.id)));
    visibleIds.add(revealedBookId);
    return orderedBooks.filter((book) => visibleIds.has(Number(book?.id)));
  })();
  const missingOrders: string[] = Array.isArray(series.missing_books)
    ? series.missing_books
    : [];
  const totalBooks = series.total_books ?? books.length;
  const readCount = books.filter((book) => book.is_read).length;
  const upcomingCount = books.filter((book) => getBookStatus(book) === "upcoming").length;
  const unreadCount = books.filter((book) => !book.is_read).length;
  const needsVerificationCount = books.filter((book) => hasUnconfirmedReleaseDate(getBookStatus(book), book)).length;
  const ownedBookNumbers = books
    .filter((book) => String(book?.record_status || "active").toLowerCase() !== "deleted")
    .map((book) => Number(book.book_number ?? book.series_order ?? NaN))
    .filter((n) => Number.isFinite(n));
  const nextBookNumber = ownedBookNumbers.length > 0 ? Math.max(...ownedBookNumbers) + 1 : null;
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
          skipReason: "upcoming" as const,
        };
      }
      const resolvedBookNumber = Number(book?.book_number ?? book?.series_order ?? NaN);
      if (!Number.isFinite(resolvedBookNumber)) {
        // Books with no series number (novellas/short stories) are matched
        // against future discovery results by title text alone -- rewriting
        // their title here risks a later "Check Now" run treating them as
        // new and duplicating them, so leave them untouched.
        return {
          id: Number(book.id),
          currentTitle,
          normalizedTitle: currentTitle,
          skipped: true,
          skipReason: "unnumbered" as const,
        };
      }
      const normalizedTitle = normalizeWizardMode === "custom"
        ? normalizeBookTitleWithCustomPattern(
            currentTitle,
            normalizeCustomPattern,
            series?.name,
            resolvedBookNumber,
            String(book?.subtitle || "").trim(),
          )
        : normalizeBookTitleForMode(
            currentTitle,
            normalizeWizardMode,
            series?.name,
            resolvedBookNumber,
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
        skipReason: null,
      };
    })
    .filter(
      (value): value is {
        id: number;
        currentTitle: string;
        normalizedTitle: string;
        skipped: boolean;
        skipReason: "upcoming" | "unnumbered" | null;
      } => Boolean(value)
    );
  const titleNormalizationApplicablePreview = titleNormalizationPreview.filter((row) => !row.skipped);
  const skippedUpcomingCount = titleNormalizationPreview.filter((row) => row.skipReason === "upcoming").length;
  const skippedUnnumberedCount = titleNormalizationPreview.filter((row) => row.skipReason === "unnumbered").length;

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
      description: "Build your own title format using simple tokens.",
      note: "Pick a starting preset, then tweak it to fit.",
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

  const titleNormalizationOptionsWithExamples = titleNormalizationOptions.map((option) => ({
    ...option,
    sampleRows: titleNormalizationExamplesByMode.get(option.mode) || [],
  }));

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

  function requestConfirm(options: ConfirmDialogState) {
    setConfirmDialog(options);
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
      const missingList = Array.isArray(statusPayload.missing_books)
        ? statusPayload.missing_books
        : Array.isArray(data.missing_books)
          ? data.missing_books
          : [];
      const discoveryDeltaCount = Number(statusPayload.discovery_delta_count ?? 0);
      // Ephemeral popup: session-only, driven directly by this response --
      // never a query against the notifications table. Single-series
      // template, since Check Now always operates on exactly one series;
      // the durable per-series notification (written server-side in the
      // same run) uses this identical count, so the two can never disagree.
      const message = statusPayload.status === "success"
        ? discoveryDeltaCount > 0
          ? `${discoveryDeltaCount} new book${discoveryDeltaCount === 1 ? "" : "s"} added to ${series.name}.`
          : "NEW BOOKS found and added to library."
        : statusPayload.status === "no_new_books"
          ? "NO NEW BOOKS FOUND."
          : statusPayload.message || (missingList.length > 0 ? `Missing books: ${missingList.join(", ")}.` : "NO NEW BOOKS FOUND.");

      await refreshSeriesFromApi();
      flashAddedMessage(message);
      if (discoveryDeltaCount > 0) {
        refreshNotificationsBadgeCount();
      }
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
      toast({
        title: "Check failed",
        description: error instanceof Error ? error.message : "Unable to check for new books right now.",
      });
      resetSeriesCheckUiState();
    }
  }

  function handleSeriesRecap() {
    if (!series) return;

    const activeBooks = books.filter((book) => String(book?.record_status || "active").toLowerCase() !== "deleted");
    const readBooks = activeBooks
      .filter((book) => Boolean(book.is_read) || String(book.read_status || "").trim().toLowerCase() === "read")
      .map((book) => ({
        number: Number(book.book_number ?? book.series_order ?? NaN),
        title: String(book.title || "").trim(),
      }))
      .filter((book) => Number.isFinite(book.number) && book.title)
      .sort((a, b) => a.number - b.number);

    if (readBooks.length === 0) {
      toast({ title: "Nothing to recap", description: "No books marked as read yet in this series." });
      return;
    }

    const lastBook = readBooks[readBooks.length - 1];
    const earlierBooks = readBooks.slice(0, -1);
    const author = series.author ? ` by ${series.author}` : "";

    const promptParts = [
      `I'm reading the "${series.name}" series${author}. I just finished book ${lastBook.number}: "${lastBook.title}".`,
      "Give me a paragraph-length recap of that book covering the major plot events, character developments, " +
        "and any unresolved threads -- enough detail to remind me exactly where the story left off.",
    ];

    if (earlierBooks.length > 0) {
      const earlierList = earlierBooks.map((book) => `Book ${book.number}: "${book.title}"`).join("; ");
      promptParts.push(
        "Then give a brief, high-level summary (1-2 sentences each) of the earlier books in reading order, " +
          `just enough to jog my memory on major plot threads and character arcs: ${earlierList}.`
      );
    }

    const url = `https://chatgpt.com/?q=${encodeURIComponent(promptParts.join(" "))}`;
    window.open(url, "_blank", "noopener,noreferrer");
  }

  function handleSearchForNextBookOnline() {
    if (!series) return;

    // Complements automated "Check for New": that only surfaces a next book
    // if it's already indexed well enough by Google Books/Hardcover/web
    // search to be classified as belonging to this series (a generic or
    // common-word series title can get swamped by unrelated results and
    // miss a real release entirely). This gives a fast manual escape hatch
    // to check a retailer/Goodreads directly for "book <owned + 1>" instead
    // of waiting on -- or debugging -- the automated pipeline.
    const query = [series.name, series.author, nextBookNumber ? `book ${nextBookNumber}` : null, "release date"]
      .filter(Boolean)
      .join(" ");
    window.open(`https://www.google.com/search?q=${encodeURIComponent(query)}`, "_blank", "noopener,noreferrer");
  }

  function handleDeleteSeriesWithBooks() {
    if (!series) return;
    requestConfirm({
      title: `Delete series "${series.name}"?`,
      description: "This permanently removes the series and all its books from your library. This cannot be undone.",
      confirmLabel: "Delete series",
      destructive: true,
      onConfirm: () => void performDeleteSeriesWithBooks(),
    });
  }

  async function performDeleteSeriesWithBooks() {
    if (!series) return;

    const visibleBookCount = Array.isArray(series.books) ? series.books.length : 0;

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

      toast({
        title: "Series deleted",
        description: `Deleted series "${series.name}" and ${deletedBooks} book${deletedBooks === 1 ? "" : "s"}.`,
      });
      window.location.href = viewAllSeriesHref;
    } catch (error) {
      console.error(error);
      toast({
        title: "Delete failed",
        description: error instanceof Error ? error.message : "Unable to delete this series right now.",
      });
    } finally {
      setDeleteSeriesSaving(false);
    }
  }


  /** Sort/filter live in the URL so Add/Edit can carry them in `returnTo`. */
  function applyViewState(next: Partial<SeriesViewState>) {
    router.replace(seriesDetailPath(Number(seriesId), { ...viewState, ...next }), { scroll: false });
  }

  function setBookSortMode(sort: SeriesBookSort) {
    applyViewState({ sort });
  }

  function toggleNeedsVerificationOnly() {
    applyViewState({ needsDates: !needsVerificationOnly });
  }

  function startAddBook() {
    if (!series) return;
    if (getDeviceClass() === "desktop") {
      setAddBookDialogOpen(true);
      return;
    }
    router.push(seriesAddBookHref(Number(series.id), viewState));
  }

  function startEditBook(book: BookRecord) {
    if (!series) return;
    if (getDeviceClass() === "desktop") {
      setEditBookId(Number(book.id));
      setEditBookDialogOpen(true);
      return;
    }
    router.push(seriesEditBookHref(Number(book.id), Number(series.id), viewState));
  }

  function pinBookOnSeriesPage(bookId: number) {
    if (!series || !Number.isFinite(bookId) || bookId <= 0) return;
    router.replace(withPin(seriesDetailPath(Number(series.id), viewState), bookId), { scroll: false });
  }

  async function handleApplyTitleNormalization() {
    if (!series) {
      return;
    }

    if (!isTitleNormalizationWizardMode(normalizeWizardMode)) {
      toast({ title: "Select a mode", description: "Please select a normalization mode." });
      return;
    }

    if (normalizeWizardMode === "custom" && !String(normalizeCustomPattern || "").trim()) {
      toast({ title: "Pattern required", description: "Enter a custom pattern before applying." });
      return;
    }

    if (!titleNormalizationApplicablePreview.length) {
      toast({ title: "Nothing to apply", description: "No eligible title changes to apply for the selected mode." });
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

      const result = (await response.json()) as NormalizeTitlesResponse;
      const updatedCount = Number(result?.updated_count || 0);
      const skippedCount = Number(result?.skipped_upcoming_count || 0);
      const diagnostics = result?.normalization_diagnostics ?? null;
      const unchangedCount = Number(diagnostics?.unchanged_count ?? 0);
      const consideredCount = Number(diagnostics?.considered_count ?? 0);

      // Broadcast normalized title updates so the Main Library view stays in sync.
      const updatedBooks = Array.isArray(result?.updated_books) ? result.updated_books : [];
      const currentSeriesBooks = Array.isArray(series?.books) ? series.books : [];
      const booksById = new Map(currentSeriesBooks.map((book) => [Number(book.id), book]));

      for (const row of updatedBooks) {
        const bookId = Number(row?.id);
        const normalizedTitle = typeof row?.to === "string" ? row.to.trim() : "";
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
          // Carried forward unchanged -- this sync call only follows a
          // title rename, but omitting it would make normalizePayload emit
          // an explicit null, which subscribers' {...book, ...payload}
          // merge would use to clobber the real availability_status they
          // already have (see book-status-sync.ts's BookStatusSyncPayload
          // docstring).
          availability_status: existing.availability_status ?? null,
          read_date: existing.read_date ?? null,
          release_date: existing.release_date ?? null,
          publication_date: existing.publication_date ?? null,
          series_id: typeof existing.series_id === "number" ? existing.series_id : series.id,
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
      toast({
        title: "Normalization failed",
        description: error instanceof Error ? error.message : "Unable to apply title normalization right now.",
      });
    } finally {
      setTitleNormalizeSaving(false);
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
      toast({ title: "Save failed", description: "Unable to save summary or notes right now." });
    } finally {
      setSummarySaving(false);
    }
  }

  function handleDeleteBook(book: BookRecord) {
    requestConfirm({
      title: `Delete "${book.title || "this book"}"?`,
      description: "This cannot be undone.",
      confirmLabel: "Delete book",
      destructive: true,
      onConfirm: () => void performDeleteBook(book),
    });
  }

  async function performDeleteBook(book: BookRecord) {
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
      toast({
        title: "Delete failed",
        description: error instanceof Error ? error.message : "Unable to delete book right now.",
      });
    }
  }

  function handleDeleteBookBeingEdited() {
    const book = books.find((item) => item.id === editBookId);
    if (!book) return;
    setEditBookDialogOpen(false);
    setEditBookId(null);
    handleDeleteBook(book);
  }

  function handleToggleSeriesFinished() {
    if (!series) return;
    const movingToUnfinished = Boolean(series.is_finished);
    requestConfirm({
      title: movingToUnfinished ? "Move this series to unfinished?" : "Move this series to finished?",
      confirmLabel: movingToUnfinished ? "Move to unfinished" : "Move to finished",
      onConfirm: () => void performToggleSeriesFinished(),
    });
  }

  async function performToggleSeriesFinished() {
    if (!series) return;
    setFinishedToggleSaving(true);

    try {
      const movingToUnfinished = Boolean(series.is_finished);
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
      toast({ title: "Update failed", description: "Unable to update series finished state right now." });
    } finally {
      setFinishedToggleSaving(false);
    }
  }

  return (
    <div className="p-2 space-y-1.5">
      <SeriesDetailHeader
        compact={isCompact}
        canEdit={canEdit}
        series={series}
        stats={{
          unread: unreadCount,
          read: readCount,
          total: totalBooks,
          upcoming: upcomingCount,
          missing: missingOrders.length,
          needsVerification: needsVerificationCount,
        }}
        needsVerificationOnly={needsVerificationOnly}
        onToggleNeedsVerification={toggleNeedsVerificationOnly}
        nextBookNumber={nextBookNumber}
        viewAllSeriesHref={viewAllSeriesHref}
        check={{
          loading: seriesCheckLoading,
          progress: seriesCheckProgress,
          currentPass: seriesCheckCurrentPass,
          stillChecking: seriesCheckStillChecking,
        }}
        finishedToggleSaving={finishedToggleSaving}
        deleteSeriesSaving={deleteSeriesSaving}
        onAddBook={startAddBook}
        onCheckForNew={() => void handleCheckForNew()}
        onSearchNextBookOnline={handleSearchForNextBookOnline}
        onSeriesRecap={handleSeriesRecap}
        onNormalizeTitles={() => {
          setNormalizeWizardMode(seriesNormalizationMode);
          setNormalizeTitlesDialogOpen(true);
        }}
        onToggleFinished={handleToggleSeriesFinished}
        onDeleteSeries={() => void handleDeleteSeriesWithBooks()}
      />

      {recentAddMessage ? (
        // bottom-20 (not bottom-4) on mobile clears the fixed bottom nav
        // (BottomNav, ~4rem tall) added in auth-gate.tsx -- otherwise this
        // toast renders on top of the nav's tab labels.
        <div className="fixed bottom-20 right-4 z-50 max-w-md rounded-md border-2 border-emerald-900 bg-emerald-800 px-3 py-2 text-sm font-semibold text-white shadow-2xl md:bottom-4">
          {recentAddMessage}
        </div>
      ) : null}

      {isCompact ? (
        <MobileSeriesBookList
          items={displayedBooks.map((book) => {
            const status = getBookStatus(book);
            return {
              book,
              status,
              statusChipClass: getStatusChipClass(status),
              unconfirmedDate: hasUnconfirmedReleaseDate(status, book),
              displayDate: formatDate(getBookDate(book)),
            };
          })}
          canEdit={canEdit}
          highlightedBookId={revealedBookId}
          onEdit={startEditBook}
          onOpenSummary={openSummaryEditor}
          onMoreByAuthor={(author) => setMoreByAuthorTarget(author)}
          onFindPublicationDate={(book) => window.open(getFindPublicationDateUrl(book), "_blank", "noopener,noreferrer")}
        />
      ) : (
      <div ref={booksTableWrapRef} className="overflow-x-auto rounded-lg border bg-card/80">
      <Table className="w-full table-fixed">
        <TableHeader>
          <TableRow>
            <TableHead className="relative" style={{ width: `${columnWidths.title}%` }}>
              <button
                type="button"
                onClick={() => setBookSortMode("az")}
                className="cursor-pointer select-none hover:underline"
                title="Sort by title, A to Z"
              >
                Title{" "}
                <span className={bookSortMode === "az" ? "text-foreground" : "text-muted-foreground/40"}>
                  &#9650;
                </span>
              </button>
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
              <button
                type="button"
                onClick={() => setBookSortMode("series")}
                className="cursor-pointer select-none hover:underline"
                title="Sort by series order"
              >
                Book #{" "}
                <span className={bookSortMode === "series" ? "text-foreground" : "text-muted-foreground/40"}>
                  &#9650;
                </span>
              </button>
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
            const unconfirmedDate = hasUnconfirmedReleaseDate(status, book);
            return (
              <TableRow
                key={book.id}
                data-book-id={book.id}
                className={
                  Number(book.id) === revealedBookId
                    ? "bg-emerald-50/80 transition-colors duration-500"
                    : "transition-colors duration-500"
                }
              >
                <TableCell className="truncate" title={book.title ?? undefined}>
                  <div>{book.title || "—"}</div>
                </TableCell>
                <TableCell className="truncate" title={book.author || "—"}>{book.author || "—"}</TableCell>
                <TableCell>
                  <div className="flex items-center gap-1">
                    <span className={getStatusChipClass(status)}>{status}</span>
                    {unconfirmedDate ? (
                      <span title="No confirmed release date yet -- click Find publication date to look it up">
                        <AlertTriangleIcon className="h-3.5 w-3.5 shrink-0 text-amber-600" aria-label="Date unconfirmed" />
                      </span>
                    ) : null}
                  </div>
                </TableCell>
                <TableCell>{formatDate(displayDate)}</TableCell>
                <TableCell>{book.book_number ?? "—"}</TableCell>
                <TableCell className="whitespace-nowrap">
                  <div className="flex items-center gap-0.5">
                    <BookActionIcon state="summarySeries" onClick={() => openSummaryEditor(book)} />
                    <BookActionIcon
                      state="findPublicationDate"
                      onClick={() => window.open(getFindPublicationDateUrl(book), "_blank", "noopener,noreferrer")}
                    />
                    <BookActionIcon state="moreByAuthor" onClick={() => setMoreByAuthorTarget(String(book.author || ""))} />
                    {canEdit ? (
                      <BookActionIcon state="edit" onClick={() => startEditBook(book)} />
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

      <BookSummaryDialog
        open={Boolean(summaryEditorBook)}
        bookTitle={summaryEditorBook?.title}
        onOpenChange={(open) => {
          if (!open) {
            setSummaryEditorBook(null);
          }
        }}
        summaryDraft={summaryDraft}
        onSummaryDraftChange={setSummaryDraft}
        notesDraft={notesDraft}
        onNotesDraftChange={setNotesDraft}
        canEdit={canEdit}
        onSave={handleSaveSummaryEditor}
        saving={summarySaving}
      />

      <MoreByAuthorDialog
        open={Boolean(moreByAuthorTarget)}
        onOpenChange={(open) => {
          if (!open) setMoreByAuthorTarget(null);
        }}
        author={moreByAuthorTarget}
        canEdit={canEdit}
        onBookAdded={() => {
          refreshSeriesFromApi();
        }}
      />

      {deviceClass === "desktop" ? (
        <AddBookDialog
          open={addBookDialogOpen}
          onOpenChange={setAddBookDialogOpen}
          lockedSeriesId={Number(series.id)}
          initialValues={{
            seriesName: series.name,
            // No "Unknown author" placeholder -- `series.author` here
            // already comes from GET /series/:id, which falls back to one
            // of the series' own books' authors before returning empty (see
            // crud/series.py). If it's still blank, require the user to
            // type one instead of writing a placeholder value.
            author: String(series.author || "").trim(),
            status: "upcoming",
          }}
          onSuccess={async (createdBook) => {
            const createdId = Number(createdBook.id);
            if (String(createdBook.read_status || "").toLowerCase() === "upcoming" && createdId > 0) {
              setRecentUpcomingBookIds((prev) => [createdId, ...prev.filter((id) => id !== createdId)]);
            }
            pinBookOnSeriesPage(createdId);
            flashAddedMessage(`Added book: ${createdBook.title || "Untitled"}`);
            await refreshSeriesFromApi();
          }}
        />
      ) : null}

      {deviceClass === "desktop" ? (
        <EditBookDialog
          open={editBookDialogOpen}
          onOpenChange={(open) => {
            setEditBookDialogOpen(open);
            if (!open) setEditBookId(null);
          }}
          bookId={editBookId}
          lockSeriesId={Number(series.id)}
          onSuccess={async (updatedBook) => {
            pinBookOnSeriesPage(Number(updatedBook.id));
            flashAddedMessage(`Book updated: ${updatedBook.title || "Untitled"}`);
            await refreshSeriesFromApi();
          }}
          onDelete={handleDeleteBookBeingEdited}
        />
      ) : null}

      <NormalizeTitlesDialog
        open={normalizeTitlesDialogOpen}
        onOpenChange={setNormalizeTitlesDialogOpen}
        options={titleNormalizationOptionsWithExamples}
        wizardMode={normalizeWizardMode}
        onWizardModeChange={setNormalizeWizardMode}
        customPresets={CUSTOM_TITLE_PATTERN_PRESETS}
        customPreset={normalizeCustomPreset}
        onCustomPresetSelect={(preset) => {
          setNormalizeCustomPreset(preset.id as CustomTitlePatternPresetId);
          setNormalizeCustomPattern(preset.pattern);
        }}
        customPattern={normalizeCustomPattern}
        onCustomPatternChange={setNormalizeCustomPattern}
        excludeUpcoming={normalizeExcludeUpcoming}
        onExcludeUpcomingChange={setNormalizeExcludeUpcoming}
        previewRows={titleNormalizationPreview}
        applicableCount={titleNormalizationApplicablePreview.length}
        skippedUpcomingCount={skippedUpcomingCount}
        skippedUnnumberedCount={skippedUnnumberedCount}
        onApply={handleApplyTitleNormalization}
        applying={titleNormalizeSaving}
      />

      <ConfirmDialog
        state={confirmDialog}
        onOpenChange={(open) => {
          if (!open) setConfirmDialog(null);
        }}
      />

    </div>
  );
}
