import { describe, expect, it, vi, beforeEach } from "vitest";

vi.mock("../api-client", () => ({
  fetchApiWithFallback: vi.fn(),
}));

import { fetchApiWithFallback } from "../api-client";
import {
  fetchBooks,
  fetchBooksLight,
  fetchSeries,
  fetchSeriesLight,
  libraryQueryKeys,
} from "./library-queries";

const mockedFetch = fetchApiWithFallback as unknown as ReturnType<typeof vi.fn>;

function jsonResponse(body: unknown) {
  return { json: async () => body } as Response;
}

beforeEach(() => {
  mockedFetch.mockReset();
});

describe("libraryQueryKeys", () => {
  it("scopes book keys by profile and series", () => {
    expect(libraryQueryKeys.books("robbie", null)).toEqual(["books", "robbie", "all"]);
    expect(libraryQueryKeys.books("robbie", 42)).toEqual(["books", "robbie", 42]);
    expect(libraryQueryKeys.books(null)).toEqual(["books", null, "all"]);
  });

  it("changes key when the profile changes, so a profile switch is a distinct cache entry", () => {
    const robbieKey = libraryQueryKeys.books("robbie");
    const daughterKey = libraryQueryKeys.books("daughter");
    expect(robbieKey).not.toEqual(daughterKey);
  });

  it("scopes light list keys by profile, limit, and offset", () => {
    expect(libraryQueryKeys.booksLight("robbie", 50, 0)).toEqual(["books-light", "robbie", 50, 0]);
    expect(libraryQueryKeys.seriesLight("robbie", 50, 100)).toEqual(["series-light", "robbie", 50, 100]);
  });

  it("scopes series keys by profile only (no pagination params)", () => {
    expect(libraryQueryKeys.series("robbie")).toEqual(["series", "robbie"]);
  });
});

describe("fetchBooks", () => {
  it("hits /books/ when no seriesId is given", async () => {
    mockedFetch.mockResolvedValue(jsonResponse([{ id: 1, title: "A", author: "B" }]));
    const result = await fetchBooks();
    expect(mockedFetch).toHaveBeenCalledWith("/books/", { cache: "no-store" });
    expect(result).toEqual([{ id: 1, title: "A", author: "B" }]);
  });

  it("hits /books/by_series/{id} when a seriesId is given", async () => {
    mockedFetch.mockResolvedValue(jsonResponse([]));
    await fetchBooks(42);
    expect(mockedFetch).toHaveBeenCalledWith("/books/by_series/42", { cache: "no-store" });
  });
});

describe("fetchBooksLight / fetchSeriesLight", () => {
  it("passes limit/offset as query params for books", async () => {
    mockedFetch.mockResolvedValue(jsonResponse([]));
    await fetchBooksLight(25, 50);
    expect(mockedFetch).toHaveBeenCalledWith("/books/light?limit=25&offset=50", { cache: "no-store" });
  });

  it("passes limit/offset as query params for series", async () => {
    mockedFetch.mockResolvedValue(jsonResponse([]));
    await fetchSeriesLight(10, 0);
    expect(mockedFetch).toHaveBeenCalledWith("/series/light?limit=10&offset=0", { cache: "no-store" });
  });
});

describe("fetchSeries", () => {
  it("hits /series/", async () => {
    mockedFetch.mockResolvedValue(jsonResponse([{ id: 1, name: "Mistborn" }]));
    const result = await fetchSeries();
    expect(mockedFetch).toHaveBeenCalledWith("/series/", { cache: "no-store" });
    expect(result).toEqual([{ id: 1, name: "Mistborn" }]);
  });
});
