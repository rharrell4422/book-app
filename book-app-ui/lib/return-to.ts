/** Safe in-app return paths for Add/Edit Book (Library and Series). */

export type SeriesBookSort = "series" | "az";

/**
 * The parts of the Series detail view that live in the URL rather than in
 * component state. Keeping sort/filter here (not useState) is what lets them
 * survive the Add/Edit round trip -- the Series page hands this state to
 * /add-book and /edit-book/[id] inside `returnTo`, so navigating back
 * restores the same ordering and filter the user left.
 */
export type SeriesViewState = {
  fromView?: string | null;
  sort?: SeriesBookSort | null;
  needsDates?: boolean;
};

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

export function parseSeriesBookSort(value: string | null | undefined): SeriesBookSort {
  return value === "az" ? "az" : "series";
}

export function parseNeedsDates(value: string | null | undefined): boolean {
  return value === "1";
}

/**
 * Only non-default view state is serialized, so the common case stays the
 * short `/series/12?fromView=ongoing` URL the app already produced.
 */
export function seriesDetailPath(seriesId: number, view?: SeriesViewState | null): string {
  const params = new URLSearchParams();

  const fromView = view?.fromView === "finished" ? "finished" : view?.fromView === "ongoing" ? "ongoing" : null;
  if (fromView) {
    params.set("fromView", fromView);
  }
  if (view?.sort === "az") {
    params.set("sort", "az");
  }
  if (view?.needsDates) {
    params.set("needsDates", "1");
  }

  const query = params.toString();
  return query ? `/series/${seriesId}?${query}` : `/series/${seriesId}`;
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

export function seriesAddBookHref(seriesId: number, view?: SeriesViewState | null): string {
  const returnTo = seriesDetailPath(seriesId, view);
  return `/add-book?seriesId=${seriesId}&returnTo=${encodeURIComponent(returnTo)}`;
}

export function seriesEditBookHref(bookId: number, seriesId: number, view?: SeriesViewState | null): string {
  const returnTo = seriesDetailPath(seriesId, view);
  return `/edit-book/${bookId}?returnTo=${encodeURIComponent(returnTo)}`;
}

export function parsePositiveId(value: string | null | undefined): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}
