/** Safe in-app return paths for Add/Edit Book (Library and Series). */

export function safeReturnTo(value: string | null | undefined): string | null {
  if (!value || !value.startsWith("/") || value.startsWith("//")) {
    return null;
  }
  return value;
}

/** Append or replace `pin=<id>` on a path that may already include a query string. */
export function withPin(pathWithQuery: string, bookId: number): string {
  if (!Number.isFinite(bookId) || bookId <= 0) {
    return pathWithQuery;
  }

  try {
    const url = new URL(pathWithQuery, "http://local.invalid");
    url.searchParams.set("pin", String(bookId));
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    const separator = pathWithQuery.includes("?") ? "&" : "?";
    return `${pathWithQuery}${separator}pin=${bookId}`;
  }
}

export function seriesDetailPath(seriesId: number, fromView?: string | null): string {
  const view = fromView === "finished" ? "finished" : fromView === "ongoing" ? "ongoing" : null;
  if (!view) {
    return `/series/${seriesId}`;
  }
  return `/series/${seriesId}?fromView=${view}`;
}

export function seriesIdFromReturnTo(value: string | null | undefined): number | null {
  const safe = safeReturnTo(value);
  if (!safe) return null;
  const match = safe.match(/^\/series\/(\d+)(?:\?|$)/);
  if (!match) return null;
  const seriesId = Number(match[1]);
  return Number.isFinite(seriesId) && seriesId > 0 ? seriesId : null;
}

export function isSeriesReturnTo(value: string | null | undefined): boolean {
  return seriesIdFromReturnTo(value) !== null;
}

export function seriesAddBookHref(seriesId: number, fromView: string): string {
  const returnTo = seriesDetailPath(seriesId, fromView);
  return `/add-book?seriesId=${seriesId}&returnTo=${encodeURIComponent(returnTo)}`;
}

export function seriesEditBookHref(bookId: number, seriesId: number, fromView: string): string {
  const returnTo = seriesDetailPath(seriesId, fromView);
  return `/edit-book/${bookId}?returnTo=${encodeURIComponent(returnTo)}`;
}

export function parsePositiveId(value: string | null | undefined): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}
