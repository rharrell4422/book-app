import { describe, expect, it } from "vitest";

import {
  isSeriesReturnTo,
  parsePositiveId,
  safeReturnTo,
  seriesAddBookHref,
  seriesDetailPath,
  seriesEditBookHref,
  seriesIdFromReturnTo,
  withPin,
} from "./return-to";

describe("safeReturnTo", () => {
  it("accepts in-app paths including query strings", () => {
    expect(safeReturnTo("/series/12?fromView=ongoing")).toBe("/series/12?fromView=ongoing");
    expect(safeReturnTo("/books")).toBe("/books");
  });

  it("rejects protocol-relative and non-path values", () => {
    expect(safeReturnTo("//evil.example")).toBeNull();
    expect(safeReturnTo("https://evil.example")).toBeNull();
    expect(safeReturnTo(null)).toBeNull();
  });
});

describe("withPin", () => {
  it("appends pin to a path with an existing query string", () => {
    expect(withPin("/series/12?fromView=finished", 44)).toBe("/series/12?fromView=finished&pin=44");
  });

  it("replaces an existing pin", () => {
    expect(withPin("/series/12?fromView=ongoing&pin=1", 9)).toBe("/series/12?fromView=ongoing&pin=9");
  });

  it("adds pin to a bare path", () => {
    expect(withPin("/books", 7)).toBe("/books?pin=7");
  });
});

describe("series helpers", () => {
  it("builds a series detail path with fromView", () => {
    expect(seriesDetailPath(12, "finished")).toBe("/series/12?fromView=finished");
    expect(seriesDetailPath(12, "ongoing")).toBe("/series/12?fromView=ongoing");
  });

  it("encodes returnTo so fromView stays nested", () => {
    expect(seriesAddBookHref(12, "finished")).toBe(
      "/add-book?seriesId=12&returnTo=%2Fseries%2F12%3FfromView%3Dfinished",
    );
    expect(seriesEditBookHref(44, 12, "ongoing")).toBe(
      "/edit-book/44?returnTo=%2Fseries%2F12%3FfromView%3Dongoing",
    );
  });

  it("parses a series id from returnTo", () => {
    expect(seriesIdFromReturnTo("/series/12?fromView=ongoing")).toBe(12);
    expect(isSeriesReturnTo("/series/12?fromView=finished")).toBe(true);
    expect(isSeriesReturnTo("/books?pin=1")).toBe(false);
  });

  it("parses positive ids", () => {
    expect(parsePositiveId("44")).toBe(44);
    expect(parsePositiveId("0")).toBeNull();
    expect(parsePositiveId("abc")).toBeNull();
  });
});
