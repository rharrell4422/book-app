"use client";

import { CircleHelpIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { BookStatus } from "@/lib/book-format";

export type AddBookFormState = {
  title: string;
  author: string;
  seriesName: string;
  bookNumber: string;
  status: BookStatus;
  releaseDate: string;
  publicationDate: string;
  readDate: string;
  autoSummary: string;
};

export const EMPTY_ADD_BOOK_FORM: AddBookFormState = {
  title: "",
  author: "",
  seriesName: "",
  bookNumber: "",
  status: "unread",
  releaseDate: "",
  publicationDate: "",
  readDate: "",
  autoSummary: "",
};

export type LookupResultState = {
  found: boolean;
  summary: string | null;
  source_url: string | null;
  matched_title: string | null;
  matched_author: string | null;
};

export function normalizeLookupMatchedTitle(value: string | null | undefined) {
  const raw = String(value || "").trim();
  if (!raw) return "";

  return raw
    .replace(/\s+ebook\s*$/i, "")
    .replace(/\s+kindle\s+edition\s*$/i, "")
    .trim();
}

export type AddBookSeriesOption = {
  id: number;
  name: string;
  author?: string | null;
};

export function AddBookFormFields({
  form,
  onFieldChange,
  onStatusChange,
  seriesList,
  lookingUpBook,
  lookupResult,
  showLookupSummary,
  onToggleLookupSummary,
  onFindDetails,
}: {
  form: AddBookFormState;
  onFieldChange: <K extends keyof AddBookFormState>(key: K, value: AddBookFormState[K]) => void;
  onStatusChange: (status: BookStatus) => void;
  seriesList: AddBookSeriesOption[];
  lookingUpBook: boolean;
  lookupResult: LookupResultState | null;
  showLookupSummary: boolean;
  onToggleLookupSummary: () => void;
  onFindDetails: () => void;
}) {
  return (
    <>
      <div className="rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
        <div className="flex items-start gap-2">
          <CircleHelpIcon className="mt-0.5 h-4 w-4 shrink-0" />
          <div>
            <p className="font-medium text-foreground">Find details helper</p>
            <p>Minimum for search: book title. Best results: book title plus author.</p>
          </div>
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1 sm:col-span-2">
          <Label htmlFor="add-book-title">Title</Label>
          <Input
            id="add-book-title"
            value={form.title}
            onChange={(event) => onFieldChange("title", event.target.value)}
            placeholder="Book title"
          />
        </div>

        <div className="space-y-1 sm:col-span-2">
          <Label htmlFor="add-book-author">Author</Label>
          <Input
            id="add-book-author"
            value={form.author}
            onChange={(event) => onFieldChange("author", event.target.value)}
            placeholder="Author name"
          />
        </div>

        <div className="sm:col-span-2 flex flex-wrap items-center gap-2">
          <Button type="button" variant="secondary" onClick={onFindDetails} disabled={lookingUpBook}>
            {lookingUpBook ? "Finding..." : "Find details"}
          </Button>
          {lookupResult?.found ? (
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span>
                Matched {normalizeLookupMatchedTitle(lookupResult.matched_title) || "title"}
                {lookupResult.matched_author ? ` by ${lookupResult.matched_author}` : ""}.
              </span>
              {lookupResult.summary ? (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 text-[11px]"
                  onClick={onToggleLookupSummary}
                >
                  {showLookupSummary ? "Hide summary" : "Show summary"}
                </Button>
              ) : null}
              {lookupResult.source_url ? (
                <a
                  href={lookupResult.source_url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-[11px] text-blue-600 underline"
                >
                  Source
                </a>
              ) : null}
            </div>
          ) : lookupResult ? (
            <span className="text-xs text-muted-foreground">No external match found. Manual add still works.</span>
          ) : null}
        </div>

        <div className="space-y-1">
          <Label htmlFor="add-book-series">Series name</Label>
          <Input
            id="add-book-series"
            list="series-options"
            value={form.seriesName}
            onChange={(event) => onFieldChange("seriesName", event.target.value)}
            placeholder="Optional series"
          />
          <datalist id="series-options">
            {seriesList.map((series) => (
              <option key={series.id} value={series.name} />
            ))}
          </datalist>
        </div>

        <div className="space-y-1">
          <Label htmlFor="add-book-number">Book number</Label>
          <Input
            id="add-book-number"
            value={form.bookNumber}
            onChange={(event) => onFieldChange("bookNumber", event.target.value)}
            placeholder="Optional number"
          />
        </div>

        <div className="space-y-1">
          <Label htmlFor="add-book-status">Status</Label>
          <select
            id="add-book-status"
            value={form.status}
            onChange={(event) => onStatusChange(event.target.value as BookStatus)}
            className="h-9 w-full rounded-md border bg-background px-2 text-sm"
          >
            <option value="unread">Unread</option>
            <option value="available">Available</option>
            <option value="upcoming">Upcoming</option>
            <option value="read">Read</option>
          </select>
        </div>

        <div className="space-y-1">
          <Label htmlFor="add-book-release-date">Date (planned/release)</Label>
          <Input
            id="add-book-release-date"
            type="date"
            value={form.releaseDate}
            onChange={(event) => onFieldChange("releaseDate", event.target.value)}
            disabled={form.status === "read"}
          />
        </div>

        <div className="space-y-1">
          <Label htmlFor="add-book-publication-date">Publication date</Label>
          <Input
            id="add-book-publication-date"
            type="date"
            value={form.publicationDate}
            onChange={(event) => onFieldChange("publicationDate", event.target.value)}
          />
        </div>

        <div className="space-y-1">
          <Label htmlFor="add-book-read-date">Read date</Label>
          <Input
            id="add-book-read-date"
            type="date"
            value={form.readDate}
            onChange={(event) => onFieldChange("readDate", event.target.value)}
            disabled={form.status !== "read"}
          />
        </div>

        {lookupResult?.summary && showLookupSummary ? (
          <div className="space-y-1 sm:col-span-2">
            <Label htmlFor="add-book-summary">Found summary</Label>
            <textarea
              id="add-book-summary"
              value={form.autoSummary}
              onChange={(event) => onFieldChange("autoSummary", event.target.value)}
              className="min-h-16 w-full rounded-lg border border-input bg-transparent px-2.5 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
            />
          </div>
        ) : null}
      </div>
    </>
  );
}
