/**
 * Shared date-parsing and display-formatting helpers for book/series views.
 * Previously duplicated (with subtle drift) across BooksClient.tsx,
 * series/page.tsx, and series/[seriesId]/page.tsx -- consolidated here so
 * date parsing and status-chip styling stay consistent across every view.
 */

export type BookStatus = "unread" | "available" | "upcoming" | "read";

export function normalizeText(value: unknown): string {
  return String(value ?? "").trim().toLowerCase();
}

/** Parses a wide variety of date string shapes the backend/importer may
 * hand back (strict ISO, native-parseable, M/D/YY, Y/M/D) into a local Date,
 * or null if nothing matched. */
export function parseFlexibleDate(value?: string | null): Date | null {
  if (!value) return null;

  const raw = String(value).trim();
  if (!raw) return null;

  const isoDateOnlyMatch = raw.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if (isoDateOnlyMatch) {
    const year = Number(isoDateOnlyMatch[1]);
    const month = Number(isoDateOnlyMatch[2]);
    const day = Number(isoDateOnlyMatch[3]);
    const date = new Date(year, month - 1, day);
    if (!Number.isNaN(date.valueOf())) {
      return date;
    }
  }

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

export function formatDate(value?: string | null): string {
  if (!value) return "—";
  const date = parseFlexibleDate(value);
  return !date || Number.isNaN(date.valueOf()) ? value : date.toLocaleDateString();
}

export function toIsoDateString(value?: string | null): string | null {
  const parsed = parseFlexibleDate(value);
  if (!parsed) return null;
  const year = parsed.getFullYear();
  const month = String(parsed.getMonth() + 1).padStart(2, "0");
  const day = String(parsed.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function toDateValue(value?: string | null): number {
  return parseFlexibleDate(value)?.valueOf() ?? Number.NEGATIVE_INFINITY;
}

export function isFutureDate(value?: string | null): boolean {
  const parsedDate = parseFlexibleDate(value);
  if (!parsedDate) return false;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  parsedDate.setHours(0, 0, 0, 0);
  return parsedDate > today;
}

export function isPastOrTodayDate(value?: string | null): boolean {
  const parsedDate = parseFlexibleDate(value);
  if (!parsedDate) return false;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  parsedDate.setHours(0, 0, 0, 0);
  return parsedDate <= today;
}

/** Where to send a user to verify a book's details themselves (e.g. an
 * unconfirmed release date on a brand-new preorder) -- the app deliberately
 * doesn't scrape retailer pages to extract this automatically (fragile and
 * against most retailers' terms of service), so this just links to the
 * actual listing the book was discovered from, or falls back to a plain
 * Google search for it. */
export function getCheckOnlineUrl(book: { title?: string | null; author?: string | null; source_url?: string | null }): string {
  const sourceUrl = String(book.source_url || "").trim();
  if (sourceUrl) return sourceUrl;

  const query = [book.title, book.author, "release date"].filter(Boolean).join(" ");
  return `https://www.google.com/search?q=${encodeURIComponent(query)}`;
}

/** True when a book is flagged as upcoming/available but no release date
 * could be pinned down at all (e.g. a brand-new preorder listing whose
 * search snippet had no date -- see getCheckOnlineUrl). Surfaced in the UI
 * as a "verify this" flag next to the status, since an unconfirmed date is
 * meaningfully different from "no news yet" (unread/not tracked) or "we
 * know exactly when" (a real date is shown). */
export function hasUnconfirmedReleaseDate(
  status: BookStatus | string,
  book: { release_date?: string | null; publication_date?: string | null }
): boolean {
  if (status !== "upcoming" && status !== "available") return false;
  return !book.release_date && !book.publication_date;
}

/** Status pill styling shared by the Library and Series views. Includes all
 * four statuses -- some call sites previously only handled three, which
 * silently fell back to the "read" (rose/red) style for "unread" books.
 * `size: "compact"` matches the denser Library table; `"default"` (the
 * series views) uses slightly larger padding/text. */
export function getStatusChipClass(status: string, size: "default" | "compact" = "default"): string {
  const sizing = size === "compact" ? "px-1.5 py-0 text-[11px]" : "px-2 py-0.5 text-xs";
  const base = `inline-flex rounded-full border ${sizing} font-semibold uppercase tracking-wide`;

  if (status === "read") {
    return `${base} border-emerald-300 bg-emerald-100 text-emerald-800`;
  }
  if (status === "available") {
    return `${base} border-sky-300 bg-sky-100 text-sky-800`;
  }
  if (status === "unread") {
    return `${base} border-slate-300 bg-slate-100 text-slate-800`;
  }
  return `${base} border-rose-300 bg-rose-100 text-rose-800`;
}
