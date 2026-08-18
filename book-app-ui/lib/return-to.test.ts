import { describe, expect, it } from "vitest";

import {
  isSeriesReturnTo,
  parseNeedsDates,
  parsePositiveId,
  parseSeriesBookSort,
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
    expect(seriesDetailPath(12, { fromView: "finished" })).toBe("/series/12?fromView=finished");
    expect(seriesDetailPath(12, { fromView: "ongoing" })).toBe("/series/12?fromView=ongoing");
  });

  it("omits default sort and filter state", () => {
    expect(seriesDetailPath(12, { fromView: "ongoing", sort: "series", needsDates: false })).toBe(
      "/series/12?fromView=ongoing",
    );
    expect(seriesDetailPath(12)).toBe("/series/12");
  });

  it("serializes non-default sort and filter state", () => {
    expect(seriesDetailPath(12, { fromView: "ongoing", sort: "az", needsDates: true })).toBe(
      "/series/12?fromView=ongoing&sort=az&needsDates=1",
    );
    expect(seriesDetailPath(12, { sort: "az" })).toBe("/series/12?sort=az");
  });

  it("encodes returnTo so view state stays nested", () => {
    expect(seriesAddBookHref(12, { fromView: "finished" })).toBe(
      "/add-book?seriesId=12&returnTo=%2Fseries%2F12%3FfromView%3Dfinished",
    );
    expect(seriesEditBookHref(44, 12, { fromView: "ongoing" })).toBe(
      "/edit-book/44?returnTo=%2Fseries%2F12%3FfromView%3Dongoing",
    );
  });

  it("carries sort and filter through the add/edit round trip", () => {
    expect(seriesEditBookHref(44, 12, { fromView: "ongoing", sort: "az", needsDates: true })).toBe(
      "/edit-book/44?returnTo=%2Fseries%2F12%3FfromView%3Dongoing%26sort%3Daz%26needsDates%3D1",
    );
  });

  it("parses sort and filter params, falling back to defaults", () => {
    expect(parseSeriesBookSort("az")).toBe("az");
    expect(parseSeriesBookSort("series")).toBe("series");
    expect(parseSeriesBookSort("nonsense")).toBe("series");
    expect(parseSeriesBookSort(null)).toBe("series");

    expect(parseNeedsDates("1")).toBe(true);
    expect(parseNeedsDates("0")).toBe(false);
    expect(parseNeedsDates(null)).toBe(false);
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
