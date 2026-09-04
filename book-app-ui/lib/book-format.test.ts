import { describe, expect, it } from "vitest";

import { getFindPublicationDateUrl, getUnifiedBookStatus } from "./book-format";

describe("getFindPublicationDateUrl", () => {
  // 2026-09-03: replaces the old getCheckOnlineUrl (source_url jump +
  // "release date" query). Deliberately unconditional now -- same query
  // shape for every book regardless of source_url presence or whether the
  // date is already confirmed, since a saved source_url could be Amazon,
  // Goodreads, or Google depending on the discovering provider with no
  // consistency across books, while the plain Google query reliably
  // surfaces the publication date directly.
  it("always builds a Google search for title + author + publication date, ignoring source_url", () => {
    const url = getFindPublicationDateUrl({
      title: "The Winter Siege: (Jonathan Hunt Thriller Book 11.0)",
      author: "Georgia Wagner; Scott Cook",
      // @ts-expect-error -- source_url isn't part of this function's input anymore; confirms it's ignored even if present on the object.
      source_url: "https://www.amazon.com/dp/whatever",
    });
    expect(url).toBe(
      "https://www.google.com/search?q=" +
        encodeURIComponent("The Winter Siege: (Jonathan Hunt Thriller Book 11.0) Georgia Wagner; Scott Cook publication date"),
    );
  });

  it("drops missing title/author cleanly", () => {
    const url = getFindPublicationDateUrl({ title: "Solo Title", author: null });
    expect(url).toBe("https://www.google.com/search?q=" + encodeURIComponent("Solo Title publication date"));
  });
});

describe("getUnifiedBookStatus", () => {
  // Phase 3 of the "Two-Axis Status Architecture" design chat's finalized
  // decision: the unified status badge is derived directly from is_read +
  // availability_status (the authoritative fields), not the legacy
  // read_status bridge string, and has no fallback heuristics for a
  // missing/unrecognized value the way the old per-view getBookStatus()
  // implementations did.

  it("is_read always wins, regardless of availability_status", () => {
    expect(getUnifiedBookStatus({ is_read: true, availability_status: "available" })).toBe("read");
    expect(getUnifiedBookStatus({ is_read: true, availability_status: "upcoming" })).toBe("read");
    expect(getUnifiedBookStatus({ is_read: true, availability_status: "owned" })).toBe("read");
  });

  it("maps owned + not read to unread", () => {
    expect(getUnifiedBookStatus({ is_read: false, availability_status: "owned" })).toBe("unread");
  });

  it("maps available + not read to available", () => {
    expect(getUnifiedBookStatus({ is_read: false, availability_status: "available" })).toBe("available");
  });

  it("maps upcoming + not read to upcoming when release date hasn't passed", () => {
    const future = new Date();
    future.setFullYear(future.getFullYear() + 1);
    expect(
      getUnifiedBookStatus({
        is_read: false,
        availability_status: "upcoming",
        release_date: future.toISOString().split("T")[0],
      }),
    ).toBe("upcoming");
  });

  it("maps upcoming + not read to upcoming when there is no date at all", () => {
    expect(getUnifiedBookStatus({ is_read: false, availability_status: "upcoming" })).toBe("upcoming");
  });

  it("stale-upcoming self-heals to available once the release date has passed (display-only)", () => {
    expect(
      getUnifiedBookStatus({
        is_read: false,
        availability_status: "upcoming",
        release_date: "2000-01-01",
      }),
    ).toBe("available");
  });

  it("stale-upcoming self-heal also honors publication_date when release_date is absent", () => {
    expect(
      getUnifiedBookStatus({
        is_read: false,
        availability_status: "upcoming",
        publication_date: "2000-01-01",
      }),
    ).toBe("available");
  });

  it("falls back to available for blank/unrecognized availability_status, mirroring normalize_availability_status's own default", () => {
    expect(getUnifiedBookStatus({ is_read: false, availability_status: null })).toBe("available");
    expect(getUnifiedBookStatus({ is_read: false, availability_status: "" })).toBe("available");
    expect(getUnifiedBookStatus({ is_read: false, availability_status: "garbage" })).toBe("available");
  });

  it("does not use release_date/series/book_number heuristics for available-status books, only is_read + availability_status", () => {
    // Even with a future release_date, an explicit availability_status of
    // "owned" (i.e. "unread") must not be reinterpreted as "upcoming" --
    // this is exactly the class of legacy-heuristic-override bug Phase 1
    // fixed and Phase 3 removes the possibility of entirely.
    const future = new Date();
    future.setFullYear(future.getFullYear() + 1);
    expect(
      getUnifiedBookStatus({
        is_read: false,
        availability_status: "owned",
        release_date: future.toISOString().split("T")[0],
      }),
    ).toBe("unread");
  });
});
