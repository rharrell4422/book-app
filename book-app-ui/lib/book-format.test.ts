import { describe, expect, it } from "vitest";

import { getUnifiedBookStatus } from "./book-format";

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
