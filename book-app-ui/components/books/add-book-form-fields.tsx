"use client";

import { CircleHelpIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { BookStatus } from "@/lib/book-format";

export type BookClassification = "standalone" | "series";

export type AddBookFormState = {
  title: string;
  author: string;
  // UI-only -- never sent to the API directly. Drives whether seriesName/
  // bookNumber are shown+required or hidden+cleared; series_id itself
  // (derived from seriesName via the existing create-or-match-series
  // logic, or forced null for "standalone") stays the only thing actually
  // persisted, so there's no second, separately-stored classification
  // that could drift out of sync with it.
  classification: BookClassification;
  seriesName: string;
  bookNumber: string;
  status: BookStatus;
  releaseDate: string;
  publicationDate: string;
  readDate: string;
  autoSummary: string;
  // Guided Discovery (locked 2026-09-03, iterations 1-5): optional, only
  // ever sent to POST /series/ the moment a genuinely NEW series is being
  // created here (see use-add-book-form.ts's handleAddBook) -- an
  // existing/matched series never re-sends these, matching the locked
  // "applies only to newly created series" retroactive-gating decision.
  canonicalUrl: string;
  canonicalSource: CanonicalSource | "";
  verifiedVolumeCount: string;
};

// Mirrors schemas.CanonicalSource on the backend.
export type CanonicalSource = "KU" | "Nook" | "Kobo" | "GooglePlay" | "PublisherSite" | "Goodreads" | "Other";

export const CANONICAL_SOURCE_OPTIONS: { value: CanonicalSource; label: string }[] = [
  { value: "KU", label: "Amazon / Kindle Unlimited" },
  { value: "Nook", label: "Barnes & Noble / Nook" },
  { value: "Kobo", label: "Kobo" },
  { value: "GooglePlay", label: "Google Play Books" },
  { value: "PublisherSite", label: "Publisher site" },
  { value: "Goodreads", label: "Goodreads" },
  { value: "Other", label: "Other" },
];

// Add Book defaults to "series" since entering book #1 of a new series is
// at least as common a starting point as adding a standalone book, and
// this default only affects the initial toggle state -- Edit Book always
// overrides it from the loaded book's own series_id (see formFromBook in
// use-edit-book-form.ts).
export const EMPTY_ADD_BOOK_FORM: AddBookFormState = {
  title: "",
  author: "",
  classification: "series",
  seriesName: "",
  bookNumber: "",
  status: "unread",
  releaseDate: "",
  publicationDate: "",
  readDate: "",
  autoSummary: "",
  canonicalUrl: "",
  canonicalSource: "",
  verifiedVolumeCount: "",
};

export type LookupResultState = {
  found: boolean;
  summary: string | null;
  source_url: string | null;
  matched_title: string | null;
  matched_author: string | null;
};

// Mirrors services/find_engine.py's candidate shape -- see that module's
// docstring for the confidence-tier definitions (HIGH/MEDIUM/LOW).
export type FindConfidence = "high" | "medium" | "low";

export type FindCandidate = {
  candidate_id: string;
  title: string | null;
  author: string | null;
  authors: string[];
  isbn13: string | null;
  description: string | null;
  source_url: string | null;
  published_date: string | null;
  providers: string[];
  confidence: FindConfidence;
  signals: {
    author_match: boolean;
    isbn_present: boolean;
    strong_title_match: boolean;
  };
};

export type FindResultState = {
  query: { title: string; author: string | null; book_number: number | null; series_name: string | null };
  candidates: FindCandidate[];
  provider_failures: { provider: string; error: string }[];
};

export const FIND_CONFIDENCE_LABEL: Record<FindConfidence, string> = {
  high: "High confidence",
  medium: "Medium confidence",
  low: "Low confidence",
};

export const FIND_CONFIDENCE_BADGE_CLASS: Record<FindConfidence, string> = {
  high: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300",
  medium: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
  low: "bg-muted text-muted-foreground",
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
  onClassificationChange,
  onStatusChange,
  seriesList,
  lookingUpBook,
  lookupResult,
  showLookupSummary,
  onToggleLookupSummary,
  onFindDetails,
  findResult,
  selectedCandidateId,
  onSelectCandidate,
  onDeclineCandidates,
  fieldIdPrefix = "add-book",
  seriesLocked = false,
}: {
  form: AddBookFormState;
  onFieldChange: <K extends keyof AddBookFormState>(key: K, value: AddBookFormState[K]) => void;
  onClassificationChange: (classification: BookClassification) => void;
  onStatusChange: (status: BookStatus) => void;
  seriesList: AddBookSeriesOption[];
  lookingUpBook: boolean;
  lookupResult: LookupResultState | null;
  showLookupSummary: boolean;
  onToggleLookupSummary: () => void;
  onFindDetails: () => void;
  findResult?: FindResultState | null;
  selectedCandidateId?: string | null;
  onSelectCandidate?: (candidate: FindCandidate) => void;
  onDeclineCandidates?: () => void;
  fieldIdPrefix?: string;
  seriesLocked?: boolean;
}) {
  // Locked contexts (adding/editing a book from inside a specific series'
  // own page) always mean "this is a series book" -- the toggle would be
  // redundant at best and misleading at worst (a form claiming
  // "Standalone" while lockedSeriesId still forces a series_id under it),
  // so it's hidden entirely and treated as forced-Series, matching how the
  // series name field itself is already locked/read-only in that case.
  const isSeriesMode = seriesLocked || form.classification === "series";
  return (
    <>
      <div className="rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
        <div className="flex items-start gap-2">
          <CircleHelpIcon className="mt-0.5 h-4 w-4 shrink-0" />
          <div>
            <p className="font-medium text-foreground">Find details helper</p>
            <p>
              Minimum for search: book title. Best results: book title plus author.
              {onSelectCandidate ? " We'll show you matches to confirm -- nothing is added automatically." : ""}
            </p>
          </div>
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1 sm:col-span-2">
          <Label htmlFor={`${fieldIdPrefix}-title`}>Title</Label>
          <Input
            id={`${fieldIdPrefix}-title`}
            value={form.title}
            onChange={(event) => onFieldChange("title", event.target.value)}
            placeholder="Book title"
          />
        </div>

        <div className="space-y-1 sm:col-span-2">
          <Label htmlFor={`${fieldIdPrefix}-author`}>Author</Label>
          <Input
            id={`${fieldIdPrefix}-author`}
            value={form.author}
            onChange={(event) => onFieldChange("author", event.target.value)}
            placeholder="Author name"
          />
        </div>

        <div className="sm:col-span-2 flex flex-wrap items-center gap-2">
          <Button type="button" variant="secondary" onClick={onFindDetails} disabled={lookingUpBook}>
            {lookingUpBook ? "Finding..." : "Find details"}
          </Button>
          {onSelectCandidate ? (
            // Add Book's FIND-backed path (see use-add-book-form.ts):
            // candidates are rendered below as an explicit pick list, never
            // auto-applied -- selectedCandidateId is used there to show
            // which one (if any) is currently bound to the form.
            !findResult && selectedCandidateId ? (
              <span className="text-xs text-muted-foreground">Match applied below.</span>
            ) : null
          ) : lookupResult?.found ? (
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

        {onSelectCandidate && findResult ? (
          <div className="sm:col-span-2 space-y-2 rounded-md border p-2">
            {findResult.candidates.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                No matches found. You can dismiss this and add the book manually.
              </p>
            ) : (
              <>
                <p className="text-xs font-medium text-foreground">
                  Choose a match, or dismiss to keep what you typed:
                </p>
                <ul className="space-y-2">
                  {findResult.candidates.map((candidate) => (
                    <li
                      key={candidate.candidate_id}
                      className="flex flex-wrap items-start justify-between gap-2 rounded-md border bg-background px-2.5 py-2"
                    >
                      <div className="min-w-0 space-y-0.5">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="text-sm font-medium text-foreground">
                            {normalizeLookupMatchedTitle(candidate.title) || "Untitled"}
                          </span>
                          <span
                            className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${FIND_CONFIDENCE_BADGE_CLASS[candidate.confidence]}`}
                          >
                            {FIND_CONFIDENCE_LABEL[candidate.confidence]}
                          </span>
                        </div>
                        <p className="text-xs text-muted-foreground">
                          {candidate.author ? `by ${candidate.author}` : "Author unknown"}
                          {candidate.isbn13 ? ` · ISBN ${candidate.isbn13}` : ""}
                        </p>
                      </div>
                      <Button
                        type="button"
                        size="sm"
                        variant={selectedCandidateId === candidate.candidate_id ? "secondary" : "outline"}
                        className="h-7 shrink-0 px-2 text-[11px]"
                        onClick={() => onSelectCandidate(candidate)}
                      >
                        {selectedCandidateId === candidate.candidate_id ? "Applied" : "Use this match"}
                      </Button>
                    </li>
                  ))}
                </ul>
              </>
            )}
            <Button type="button" variant="ghost" size="sm" className="h-6 px-2 text-[11px]" onClick={onDeclineCandidates}>
              Dismiss
            </Button>
          </div>
        ) : null}

        {seriesLocked ? null : (
          <div className="space-y-1 sm:col-span-2">
            <Label>Is this a standalone book, or part of a series?</Label>
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                size="sm"
                variant={form.classification === "standalone" ? "secondary" : "outline"}
                onClick={() => onClassificationChange("standalone")}
              >
                Standalone
              </Button>
              <Button
                type="button"
                size="sm"
                variant={form.classification === "series" ? "secondary" : "outline"}
                onClick={() => onClassificationChange("series")}
              >
                Series
              </Button>
            </div>
          </div>
        )}

        {isSeriesMode ? (
          <>
            <div className="space-y-1">
              <Label htmlFor={`${fieldIdPrefix}-series`}>Series name{seriesLocked ? "" : " (required)"}</Label>
              <Input
                id={`${fieldIdPrefix}-series`}
                list={seriesLocked ? undefined : `${fieldIdPrefix}-series-options`}
                value={form.seriesName}
                onChange={(event) => onFieldChange("seriesName", event.target.value)}
                placeholder="Series name"
                disabled={seriesLocked}
                readOnly={seriesLocked}
              />
              {seriesLocked ? (
                <p className="text-[11px] text-muted-foreground">Locked to this series.</p>
              ) : null}
              {seriesLocked ? null : (
                <datalist id={`${fieldIdPrefix}-series-options`}>
                  {seriesList.map((series) => (
                    <option key={series.id} value={series.name} />
                  ))}
                </datalist>
              )}
            </div>

            <div className="space-y-1">
              <Label htmlFor={`${fieldIdPrefix}-number`}>Book number{seriesLocked ? "" : " (required)"}</Label>
              <Input
                id={`${fieldIdPrefix}-number`}
                value={form.bookNumber}
                onChange={(event) => onFieldChange("bookNumber", event.target.value)}
                placeholder="e.g. 1"
              />
            </div>

            {seriesLocked ? null : (
              <div className="space-y-2 rounded-md border bg-muted/30 px-3 py-2 sm:col-span-2">
                <p className="text-xs font-medium text-foreground">
                  Guided Discovery (optional -- only used if this is a brand new series)
                </p>
                <p className="text-[11px] text-muted-foreground">
                  If you know the exact page you source this series from and how many books
                  currently exist there, adding it here helps discovery find every volume.
                  Ignored if this series already exists.
                </p>
                <div className="grid gap-2 sm:grid-cols-2">
                  <div className="space-y-1 sm:col-span-2">
                    <Label htmlFor={`${fieldIdPrefix}-canonical-url`}>Source URL</Label>
                    <Input
                      id={`${fieldIdPrefix}-canonical-url`}
                      value={form.canonicalUrl}
                      onChange={(event) => onFieldChange("canonicalUrl", event.target.value)}
                      placeholder="https://..."
                    />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor={`${fieldIdPrefix}-canonical-source`}>Source</Label>
                    <select
                      id={`${fieldIdPrefix}-canonical-source`}
                      value={form.canonicalSource}
                      onChange={(event) => onFieldChange("canonicalSource", event.target.value as CanonicalSource | "")}
                      className="h-9 w-full rounded-md border bg-background px-2 text-sm"
                    >
                      <option value="">Select...</option>
                      {CANONICAL_SOURCE_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor={`${fieldIdPrefix}-verified-count`}>Verified volume count</Label>
                    <Input
                      id={`${fieldIdPrefix}-verified-count`}
                      value={form.verifiedVolumeCount}
                      onChange={(event) => onFieldChange("verifiedVolumeCount", event.target.value)}
                      placeholder="e.g. 12"
                    />
                  </div>
                </div>
              </div>
            )}
          </>
        ) : null}

        <div className="space-y-1">
          <Label htmlFor={`${fieldIdPrefix}-status`}>Status</Label>
          <select
            id={`${fieldIdPrefix}-status`}
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
          <Label htmlFor={`${fieldIdPrefix}-release-date`}>Date (planned/release)</Label>
          <Input
            id={`${fieldIdPrefix}-release-date`}
            type="date"
            value={form.releaseDate}
            onChange={(event) => onFieldChange("releaseDate", event.target.value)}
            disabled={form.status === "read"}
          />
        </div>

        <div className="space-y-1">
          <Label htmlFor={`${fieldIdPrefix}-publication-date`}>Publication date</Label>
          <Input
            id={`${fieldIdPrefix}-publication-date`}
            type="date"
            value={form.publicationDate}
            onChange={(event) => onFieldChange("publicationDate", event.target.value)}
          />
        </div>

        <div className="space-y-1">
          <Label htmlFor={`${fieldIdPrefix}-read-date`}>Read date</Label>
          <Input
            id={`${fieldIdPrefix}-read-date`}
            type="date"
            value={form.readDate}
            onChange={(event) => onFieldChange("readDate", event.target.value)}
            disabled={form.status !== "read"}
          />
        </div>

        {showLookupSummary && (lookupResult?.summary || form.autoSummary) ? (
          <div className="space-y-1 sm:col-span-2">
            <Label htmlFor={`${fieldIdPrefix}-summary`}>Summary</Label>
            <textarea
              id={`${fieldIdPrefix}-summary`}
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
