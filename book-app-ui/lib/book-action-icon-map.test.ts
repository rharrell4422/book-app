import { describe, expect, it } from "vitest";
import {
  CheckIcon,
  ExternalLinkIcon,
  RotateCcwIcon,
  Trash2Icon,
} from "lucide-react";

import { getBookActionVisual, isDelayedBookAction, type BookActionState } from "./book-action-icon-map";

const ALL_STATES: BookActionState[] = [
  "read",
  "unread",
  "series",
  "findPublicationDate",
  "delete",
  "edit",
  "moreByAuthor",
  "summarySeries",
  "summaryStandaloneHasContent",
  "summaryStandaloneEmpty",
];

describe("getBookActionVisual", () => {
  it("defines a visual for every book action state with non-empty text fields", () => {
    for (const state of ALL_STATES) {
      const visual = getBookActionVisual(state);
      expect(visual.icon).toBeTypeOf("object");
      expect(visual.label.length).toBeGreaterThan(0);
      expect(visual.ariaLabel.length).toBeGreaterThan(0);
      expect(visual.tooltipLabel.length).toBeGreaterThan(0);
    }
  });

  it("maps the read/unread toggle to opposite icons, labels, and colors", () => {
    const unread = getBookActionVisual("unread");
    const read = getBookActionVisual("read");

    expect(unread.icon).toBe(CheckIcon);
    expect(unread.label).toBe("Mark read");
    expect(unread.colorVariant).toBe("positive");

    expect(read.icon).toBe(RotateCcwIcon);
    expect(read.label).toBe("Mark unread");
    expect(read.colorVariant).toBe("negative");
  });

  it("gives delete a destructive button variant distinct from the read-toggle's negative outline", () => {
    const del = getBookActionVisual("delete");
    const read = getBookActionVisual("read");

    expect(del.icon).toBe(Trash2Icon);
    expect(del.colorVariant).toBe("negative");
    expect(del.buttonVariant).toBe("destructive");
    // Both are "negative" semantically, but only delete is destructive-styled.
    expect(read.buttonVariant).toBe("outline");
  });

  it("gives the find-publication-date action a single consistent label regardless of book state", () => {
    const findDate = getBookActionVisual("findPublicationDate");
    expect(findDate.icon).toBe(ExternalLinkIcon);
    expect(findDate.label).toBe("Find publication date");
    expect(findDate.colorVariant).toBe("neutral");
  });

  it("distinguishes the standalone-summary has-content/empty split by label only", () => {
    const hasContent = getBookActionVisual("summaryStandaloneHasContent");
    const empty = getBookActionVisual("summaryStandaloneEmpty");

    expect(hasContent.icon).toBe(empty.icon);
    expect(hasContent.label).toBe("View/edit summary and notes");
    expect(empty.label).toBe("Fetch an AI summary for this book");
  });

  it("keeps the series-list summary label static regardless of content", () => {
    expect(getBookActionVisual("summarySeries").label).toBe("View/edit summary and notes");
  });
});

describe("isDelayedBookAction", () => {
  it("delays only the destructive/ambiguous actions", () => {
    expect(isDelayedBookAction("read")).toBe(true);
    expect(isDelayedBookAction("unread")).toBe(true);
    expect(isDelayedBookAction("delete")).toBe(true);
  });

  it("does not delay navigation actions", () => {
    const immediateStates: BookActionState[] = [
      "series",
      "findPublicationDate",
      "edit",
      "moreByAuthor",
      "summarySeries",
      "summaryStandaloneHasContent",
      "summaryStandaloneEmpty",
    ];
    for (const state of immediateStates) {
      expect(isDelayedBookAction(state)).toBe(false);
    }
  });
});
