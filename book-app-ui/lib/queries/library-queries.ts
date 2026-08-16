"use client";

/**
 * Shared, profile-scoped data-fetching layer for books/series, built on
 * TanStack Query. This is the extraction point Phase 0 of the mobile
 * architecture plan calls for: today, BooksClient.tsx,
 * StandaloneBooksClient.tsx, and the series pages each duplicate their own
 * fetchBooks()/fetchSeriesList() + useState + useEffect fetch-on-mount
 * logic. Both the existing desktop table views and any future mobile
 * card/list views can consume these same hooks instead of re-fetching
 * independently.
 *
 * `profileId` is always part of the query key, so switching the active
 * profile is itself a cache-key change: TanStack Query treats it as a
 * distinct entry and fetches automatically. This is the mechanism meant to
 * eventually replace the `key={profileId}` full-tree remount in
 * AuthGate -- not wired into that yet (a follow-up step once pages actually
 * consume these hooks), but the query keys are already structured for it.
 *
 * Pure key-builders and fetchers are exported alongside the hooks so they
 * can be unit-tested without rendering a React tree.
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchApiWithFallback } from "../api-client";
import { useProfile } from "../profile-context";

export type LibrarySeriesState = {
  has_new_books: boolean;
  has_new_available_books: boolean;
  has_new_upcoming_books: boolean;
  has_unread_books: boolean;
  has_upcoming_books: boolean;
  is_caught_up: boolean;
};

// Deliberately not an exhaustive mirror of every BookResponse field --
// enough of the commonly-used ones for type safety, plus an index signature
// so callers reading additional fields (there are ~40 on the real API) still
// type-check instead of requiring this type to be kept in lockstep forever.
export type LibraryBook = {
  id: number;
  title: string;
  author: string;
  series_id?: number | null;
  series_name?: string | null;
  book_number?: number | null;
  read_status?: string | null;
  is_read?: boolean | null;
  is_upcoming_auto?: boolean | null;
  is_upcoming_final?: boolean | null;
  is_missing?: boolean | null;
  record_status?: string | null;
  release_date?: string | null;
  publication_date?: string | null;
  read_date?: string | null;
  rating?: number | null;
  [key: string]: unknown;
};

export type LibrarySeries = {
  id: number;
  name: string;
  author?: string | null;
  is_finished?: boolean | null;
  total_books?: number | null;
  next_unread_book_number?: number | null;
  next_upcoming_book_number?: number | null;
  has_new_books?: boolean;
  has_unread_books?: boolean;
  has_upcoming_books?: boolean;
  is_caught_up?: boolean;
  read_count?: number;
  unread_count?: number;
  series_state?: LibrarySeriesState | null;
  books?: LibraryBook[];
  [key: string]: unknown;
};

export type LibraryBookListItem = {
  id: number;
  title: string;
  author: string;
  series_id?: number | null;
  series_name?: string | null;
  book_number?: number | null;
  read_status?: string | null;
  is_read?: boolean | null;
  is_upcoming_final?: boolean | null;
  rating?: number | null;
};

export type LibrarySeriesListItem = {
  id: number;
  name: string;
  author?: string | null;
  total_books?: number | null;
  read_count?: number | null;
  unread_count?: number | null;
  is_finished?: boolean | null;
  has_new_books?: boolean | null;
  has_unread_books?: boolean | null;
  has_upcoming_books?: boolean | null;
  is_caught_up?: boolean | null;
};

export const libraryQueryKeys = {
  books: (profileId: string | null, seriesId?: number | string | null) =>
    ["books", profileId, seriesId ?? "all"] as const,
  booksLight: (profileId: string | null, limit: number, offset: number) =>
    ["books-light", profileId, limit, offset] as const,
  series: (profileId: string | null) => ["series", profileId] as const,
  seriesLight: (profileId: string | null, limit: number, offset: number) =>
    ["series-light", profileId, limit, offset] as const,
};

export async function fetchBooks(seriesId?: number | string | null): Promise<LibraryBook[]> {
  const path = seriesId ? `/books/by_series/${seriesId}` : "/books/";
  const response = await fetchApiWithFallback(path, { cache: "no-store" });
  return response.json();
}

export async function fetchBooksLight(limit: number, offset: number): Promise<LibraryBookListItem[]> {
  const response = await fetchApiWithFallback(`/books/light?limit=${limit}&offset=${offset}`, {
    cache: "no-store",
  });
  return response.json();
}

export async function fetchSeries(): Promise<LibrarySeries[]> {
  const response = await fetchApiWithFallback("/series/", { cache: "no-store" });
  return response.json();
}

export async function fetchSeriesLight(limit: number, offset: number): Promise<LibrarySeriesListItem[]> {
  const response = await fetchApiWithFallback(`/series/light?limit=${limit}&offset=${offset}`, {
    cache: "no-store",
  });
  return response.json();
}

/**
 * Full-fidelity books query, scoped by the active profile. Pass `seriesId`
 * to mirror the existing GET /books/by_series/{id} usage on the series
 * detail page.
 */
export function useBooksLibrary(seriesId?: number | string | null, options?: { enabled?: boolean }) {
  const { profileId, ready } = useProfile();
  return useQuery({
    queryKey: libraryQueryKeys.books(profileId, seriesId),
    queryFn: () => fetchBooks(seriesId),
    enabled: ready && Boolean(profileId) && (options?.enabled ?? true),
  });
}

/** Slim, paginated books query for future card/list views (mobile). */
export function useBooksLibraryLight(params?: { limit?: number; offset?: number; enabled?: boolean }) {
  const { profileId, ready } = useProfile();
  const limit = params?.limit ?? 50;
  const offset = params?.offset ?? 0;
  return useQuery({
    queryKey: libraryQueryKeys.booksLight(profileId, limit, offset),
    queryFn: () => fetchBooksLight(limit, offset),
    enabled: ready && Boolean(profileId) && (params?.enabled ?? true),
  });
}

/** Full-fidelity series query (including nested books[]), scoped by profile. */
export function useSeriesLibrary(options?: { enabled?: boolean }) {
  const { profileId, ready } = useProfile();
  return useQuery({
    queryKey: libraryQueryKeys.series(profileId),
    queryFn: fetchSeries,
    enabled: ready && Boolean(profileId) && (options?.enabled ?? true),
  });
}

/** Slim, paginated series query (no nested books[]) for future card views. */
export function useSeriesLibraryLight(params?: { limit?: number; offset?: number; enabled?: boolean }) {
  const { profileId, ready } = useProfile();
  const limit = params?.limit ?? 50;
  const offset = params?.offset ?? 0;
  return useQuery({
    queryKey: libraryQueryKeys.seriesLight(profileId, limit, offset),
    queryFn: () => fetchSeriesLight(limit, offset),
    enabled: ready && Boolean(profileId) && (params?.enabled ?? true),
  });
}

/**
 * Invalidate this profile's cached books/series after a mutation (add/edit/
 * delete a book, series check completing, import finishing, etc.) -- the
 * query-key-scoped replacement for calling fetchBooks()/fetchSeriesList()
 * again directly.
 */
export function useInvalidateLibrary() {
  const queryClient = useQueryClient();
  const { profileId } = useProfile();
  return {
    invalidateBooks: () => queryClient.invalidateQueries({ queryKey: ["books", profileId] }),
    invalidateSeries: () => queryClient.invalidateQueries({ queryKey: ["series", profileId] }),
    invalidateAll: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["books", profileId] }),
        queryClient.invalidateQueries({ queryKey: ["series", profileId] }),
      ]);
    },
  };
}
