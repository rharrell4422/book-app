"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { ExternalLinkIcon, SearchIcon, SparklesIcon, StarIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { fetchApiWithFallback } from "@/lib/api-client";
import { formatDate, getRatingsReviewLinks, getStatusChipClass } from "@/lib/book-format";

export type AuthorDiscoveryCandidate = {
  title: string;
  author: string;
  series_name: string | null;
  matched_series_id: number | null;
  matched_series_name: string | null;
  matched_series_is_finished: boolean | null;
  matched_series_total_books: number | null;
  series_number: number | null;
  status: "available" | "upcoming";
  release_date: string | null;
  source_url: string | null;
  isbn13: string | null;
  provider: string | null;
  description: string | null;
  series_total_books: number | null;
};

type AuthorDiscoveryResponse = {
  author: string;
  candidates: AuthorDiscoveryCandidate[];
  provider_failures: { provider: string; error: string }[];
  all_providers_failed: boolean;
};

type CandidateGroup = {
  key: string;
  heading: string;
  candidates: AuthorDiscoveryCandidate[];
};

/** Splits the flat candidate list into the three buckets a user actually
 * cares about: books in a series you already track, books in a series you
 * don't (grouped so a multi-book new series reads as one series, not N
 * unrelated rows), and books with no series signal at all. */
function groupCandidates(candidates: AuthorDiscoveryCandidate[]) {
  const trackedGroups = new Map<string, CandidateGroup>();
  const newSeriesGroups = new Map<string, CandidateGroup>();
  const standalone: AuthorDiscoveryCandidate[] = [];

  for (const candidate of candidates) {
    if (candidate.matched_series_id != null) {
      const key = String(candidate.matched_series_id);
      const heading = candidate.matched_series_name || candidate.series_name || "Tracked series";
      if (!trackedGroups.has(key)) trackedGroups.set(key, { key, heading, candidates: [] });
      trackedGroups.get(key)!.candidates.push(candidate);
    } else if (candidate.series_name) {
      const key = candidate.series_name.trim().toLowerCase();
      if (!newSeriesGroups.has(key)) newSeriesGroups.set(key, { key, heading: candidate.series_name, candidates: [] });
      newSeriesGroups.get(key)!.candidates.push(candidate);
    } else {
      standalone.push(candidate);
    }
  }

  const byNumber = (a: AuthorDiscoveryCandidate, b: AuthorDiscoveryCandidate) =>
    (a.series_number ?? Number.MAX_SAFE_INTEGER) - (b.series_number ?? Number.MAX_SAFE_INTEGER);
  const byHeading = (a: CandidateGroup, b: CandidateGroup) => a.heading.localeCompare(b.heading);

  trackedGroups.forEach((group) => group.candidates.sort(byNumber));
  newSeriesGroups.forEach((group) => group.candidates.sort(byNumber));

  return {
    tracked: Array.from(trackedGroups.values()).sort(byHeading),
    newSeries: Array.from(newSeriesGroups.values()).sort(byHeading),
    standalone: standalone.sort((a, b) => a.title.localeCompare(b.title)),
  };
}

type SeriesOverviewState = { loading: boolean; text: string | null; error: string | null };
type SeriesFillState = { loading: boolean; error: string | null; done: boolean };

/** Maturity indicators are computed entirely client-side from data this
 * component already has (no extra fetch, no button needed) -- only the
 * separate "Series Overview" LLM call is gated behind a click.
 *
 * Deliberately hedged wording throughout: this app's own discovery pass
 * (the same one populating this dialog) recently had a live bug where a
 * whole trilogy was scattered across unrelated "standalone" entries, so
 * "found N books" here is a floor, not a verified total, and completion
 * status is never asserted outright -- a confidently-wrong "Completed"
 * badge is worse than no badge, since it's the exact wrong-direction
 * mistake a reader is trying to avoid by checking series maturity at all.
 */
function computeNewSeriesMaturity(candidates: AuthorDiscoveryCandidate[]) {
  const totalHint = candidates.reduce<number | null>((max, candidate) => {
    if (candidate.series_total_books == null) return max;
    return max == null ? candidate.series_total_books : Math.max(max, candidate.series_total_books);
  }, null);
  const availableDates = candidates
    .filter((c) => c.status === "available" && c.release_date)
    .map((c) => c.release_date as string)
    .sort();
  const upcomingDates = candidates
    .filter((c) => c.status === "upcoming" && c.release_date)
    .map((c) => c.release_date as string)
    .sort();

  return {
    foundCount: candidates.length,
    totalHint,
    lastReleaseDate: availableDates.length ? availableDates[availableDates.length - 1] : null,
    nextReleaseDate: upcomingDates.length ? upcomingDates[0] : null,
    hasUpcoming: candidates.some((c) => c.status === "upcoming"),
  };
}

function NewSeriesMaturityBadge({ candidates }: { candidates: AuthorDiscoveryCandidate[] }) {
  const maturity = computeNewSeriesMaturity(candidates);
  const countLabel = maturity.totalHint
    ? `Found ${maturity.foundCount} of ~${maturity.totalHint}`
    : `Found ${maturity.foundCount} book${maturity.foundCount === 1 ? "" : "s"}`;
  const statusLabel = maturity.hasUpcoming
    ? maturity.nextReleaseDate
      ? `Next: ${formatDate(maturity.nextReleaseDate)}`
      : "Next book announced, date TBD"
    : "No upcoming release found";

  return (
    <span className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[11px] text-muted-foreground">
      <span>{countLabel}</span>
      <span aria-hidden>·</span>
      <span>{statusLabel}</span>
      {maturity.lastReleaseDate ? (
        <>
          <span aria-hidden>·</span>
          <span>Last: {formatDate(maturity.lastReleaseDate)}</span>
        </>
      ) : null}
    </span>
  );
}

/** Tracked series already have a real Series row in this app -- its own
 * is_finished flag (kept current by the normal Check Now flow) is far more
 * authoritative than anything re-derived from this one-off discovery
 * batch, so it's used as-is rather than recomputed. */
function TrackedSeriesMaturityBadge({ candidates }: { candidates: AuthorDiscoveryCandidate[] }) {
  const isFinished = candidates[0]?.matched_series_is_finished;
  if (isFinished == null) return null;
  return <span className="text-[11px] text-muted-foreground">{isFinished ? "Finished" : "Ongoing"}</span>;
}

/** "More by this author" -- an on-demand, author-wide discovery lookup
 * shared by every book-listing view (All Books, Standalone Books, Series
 * detail). Self-fetches from /books/discover_by_author whenever it's
 * opened for a given author, so each call site only needs to render a
 * trigger button and hold the currently-targeted author name. */
export function MoreByAuthorDialog({
  open,
  onOpenChange,
  author,
  canEdit,
  onBookAdded,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  author: string | null;
  canEdit: boolean;
  onBookAdded?: () => void;
}) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<AuthorDiscoveryCandidate[]>([]);
  const [addedKeys, setAddedKeys] = useState<Set<string>>(new Set());
  const [addingKey, setAddingKey] = useState<string | null>(null);
  const [overviews, setOverviews] = useState<Record<string, SeriesOverviewState>>({});
  const [seriesFill, setSeriesFill] = useState<Record<string, SeriesFillState>>({});

  const groups = useMemo(() => groupCandidates(candidates), [candidates]);

  const candidateKey = (candidate: AuthorDiscoveryCandidate) => `${candidate.title}|${candidate.series_number ?? ""}`;

  const runDiscovery = useCallback(async () => {
    if (!author) return;
    setLoading(true);
    setError(null);
    try {
      const response = await fetchApiWithFallback(`/books/discover_by_author?author=${encodeURIComponent(author)}`, {
        cache: "no-store",
      });
      if (!response.ok) {
        throw new Error(`Lookup failed (${response.status})`);
      }
      const data: AuthorDiscoveryResponse = await response.json();
      setCandidates(Array.isArray(data.candidates) ? data.candidates : []);
      if (data.all_providers_failed) {
        setError("All discovery sources failed -- try again in a bit.");
      }
    } catch (err) {
      console.error("Error discovering more by author:", err);
      setError(err instanceof Error ? err.message : "Unable to search for more books by this author.");
      setCandidates([]);
    } finally {
      setLoading(false);
    }
  }, [author]);

  useEffect(() => {
    if (open && author) {
      setAddedKeys(new Set());
      setOverviews({});
      setSeriesFill({});
      runDiscovery();
    }
  }, [open, author, runDiscovery]);

  // The broad author-wide sweep above is deliberately shallow (one query
  // per catalog API) -- for a prolific author it can easily surface only
  // part of a series (regression: Glynn Stewart's "Scattered Stars:
  // Conviction" showed "Found 1 of ~6" from Hardcover's own book count,
  // but the broad pass only ever turned up book 1). This runs the same
  // deeper, targeted per-series search a tracked series' own Check Now
  // uses, scoped to just this series name, and merges anything new it
  // finds into the group already on screen -- on demand only, since it
  // costs more than the broad pass and most groups won't need it.
  async function handleFindRestOfSeries(group: CandidateGroup) {
    const seriesName = group.heading;
    const candidateAuthor = group.candidates[0]?.author || author || "";
    if (!seriesName || !candidateAuthor) return;
    setSeriesFill((prev) => ({ ...prev, [group.key]: { loading: true, error: null, done: false } }));
    try {
      const response = await fetchApiWithFallback(
        `/books/discover_series_by_name?series_name=${encodeURIComponent(seriesName)}&author=${encodeURIComponent(candidateAuthor)}`,
        { cache: "no-store" },
      );
      if (!response.ok) {
        throw new Error(`Lookup failed (${response.status})`);
      }
      const data: { candidates: AuthorDiscoveryCandidate[] } = await response.json();
      const found = Array.isArray(data.candidates) ? data.candidates : [];
      setCandidates((prev) => {
        const existingKeys = new Set(prev.map(candidateKey));
        const additions = found.filter((candidate) => !existingKeys.has(candidateKey(candidate)));
        return additions.length ? [...prev, ...additions] : prev;
      });
      setSeriesFill((prev) => ({
        ...prev,
        [group.key]: {
          loading: false,
          error: found.length ? null : "No additional books found for this series.",
          done: true,
        },
      }));
    } catch (err) {
      console.error("Error searching for the rest of this series:", err);
      setSeriesFill((prev) => ({
        ...prev,
        [group.key]: {
          loading: false,
          error: err instanceof Error ? err.message : "Unable to search for more books in this series.",
          done: true,
        },
      }));
    }
  }

  // On-demand only, per group -- never fetched automatically alongside
  // discovery. Not persisted anywhere: closing the dialog and reopening it
  // re-fetches, which is an acceptable tradeoff given how infrequently this
  // gets clicked versus adding a database table just to cache it.
  async function handleFetchSeriesOverview(group: CandidateGroup) {
    setOverviews((prev) => ({ ...prev, [group.key]: { loading: true, text: null, error: null } }));
    try {
      const response = await fetchApiWithFallback("/books/series_overview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          series_name: group.heading,
          author: group.candidates[0]?.author || author || "",
          books: group.candidates.map((c) => ({ title: c.title, description: c.description })),
        }),
      });
      if (!response.ok) {
        throw new Error(`Lookup failed (${response.status})`);
      }
      const data: { overview: string | null } = await response.json();
      setOverviews((prev) => ({
        ...prev,
        [group.key]: {
          loading: false,
          text: data.overview,
          error: data.overview ? null : "No overview available for this series yet.",
        },
      }));
    } catch (err) {
      console.error("Error fetching series overview:", err);
      setOverviews((prev) => ({
        ...prev,
        [group.key]: {
          loading: false,
          text: null,
          error: err instanceof Error ? err.message : "Unable to fetch a series overview right now.",
        },
      }));
    }
  }

  function renderActions(candidate: AuthorDiscoveryCandidate) {
    const key = candidateKey(candidate);
    const alreadyAdded = addedKeys.has(key);
    return (
      <div className="flex items-center justify-end gap-1">
        <Popover>
          <PopoverTrigger asChild>
            <Button type="button" variant="ghost" size="icon-xs" title="Check ratings / reviews">
              <StarIcon />
            </Button>
          </PopoverTrigger>
          <PopoverContent align="end" className="w-48">
            <div className="flex flex-col gap-1">
              {getRatingsReviewLinks(candidate.title, candidate.author).map((link) => (
                <a
                  key={link.label}
                  href={link.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center justify-between rounded px-2 py-1 text-sm hover:bg-muted"
                >
                  {link.label}
                  <ExternalLinkIcon className="h-3.5 w-3.5" />
                </a>
              ))}
            </div>
          </PopoverContent>
        </Popover>
        {canEdit ? (
          <Button
            type="button"
            size="sm"
            variant={alreadyAdded ? "outline" : "default"}
            disabled={alreadyAdded || addingKey === key}
            onClick={() => handleAddToLibrary(candidate)}
          >
            {alreadyAdded ? "Added" : addingKey === key ? "Adding…" : "Add to Library"}
          </Button>
        ) : null}
      </div>
    );
  }

  /** showNumberColumn: omitted for the standalone bucket, where a series
   * position never applies. The series-name column is intentionally never
   * shown here -- each table only ever appears under a heading that already
   * names the (tracked or new) series, or under "New standalone books",
   * so repeating it per-row would be redundant. */
  function renderCandidateTable(list: AuthorDiscoveryCandidate[], { showNumberColumn }: { showNumberColumn: boolean }) {
    return (
      <Table className="text-sm">
        <TableHeader>
          <TableRow>
            <TableHead>Title</TableHead>
            {showNumberColumn ? <TableHead>#</TableHead> : null}
            <TableHead>Status</TableHead>
            <TableHead>Date</TableHead>
            <TableHead className="text-right">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {list.map((candidate) => (
            <TableRow key={candidateKey(candidate)}>
              <TableCell className="max-w-[260px] truncate" title={candidate.title}>
                {candidate.title}
              </TableCell>
              {showNumberColumn ? <TableCell>{candidate.series_number ?? "—"}</TableCell> : null}
              <TableCell>
                <span className={getStatusChipClass(candidate.status, "compact")}>{candidate.status}</span>
              </TableCell>
              <TableCell>{formatDate(candidate.release_date)}</TableCell>
              <TableCell className="text-right">{renderActions(candidate)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    );
  }

  async function handleAddToLibrary(candidate: AuthorDiscoveryCandidate) {
    setAddingKey(candidateKey(candidate));
    try {
      // Per the "no auto-created series from a guessed name" rule: only
      // link to a series that's already tracked in the library
      // (matched_series_id) -- an unmatched guess is added as standalone
      // (series_id: null) rather than creating a new series here.
      const response = await fetchApiWithFallback("/books/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: candidate.title,
          author: candidate.author,
          series_id: candidate.matched_series_id,
          series_order: candidate.series_number,
          book_number: candidate.series_number,
          read_status: candidate.status,
          is_read: false,
          release_date: candidate.status === "upcoming" ? candidate.release_date : undefined,
          publication_date: candidate.status === "available" ? candidate.release_date : undefined,
          source_url: candidate.source_url,
          isbn13: candidate.isbn13,
        }),
      });
      if (!response.ok) {
        throw new Error(`Failed to add book (${response.status})`);
      }
      setAddedKeys((prev) => new Set(prev).add(candidateKey(candidate)));
      onBookAdded?.();
    } catch (err) {
      console.error("Error adding discovered book:", err);
      setError(err instanceof Error ? err.message : "Unable to add this book right now.");
    } finally {
      setAddingKey(null);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>More by {author || "this author"}</DialogTitle>
          <DialogDescription>
            A live search across this author&apos;s full bibliography, including series you don&apos;t track yet.
          </DialogDescription>
        </DialogHeader>

        {loading ? (
          <p className="py-6 text-center text-sm text-muted-foreground">Searching…</p>
        ) : error && candidates.length === 0 ? (
          <p className="py-6 text-center text-sm text-destructive">{error}</p>
        ) : candidates.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">
            No new books found for this author right now.
          </p>
        ) : (
          <div className="flex max-h-[65vh] flex-col gap-5 overflow-y-auto pr-1">
            {groups.tracked.length > 0 ? (
              <section>
                <h3 className="mb-2 text-sm font-semibold">You have this series</h3>
                <div className="flex flex-col gap-4">
                  {groups.tracked.map((group) => (
                    <div key={group.key} className="rounded-lg border">
                      <div className="flex flex-col gap-0.5 border-b bg-muted/50 px-3 py-1.5">
                        <p className="text-xs font-medium">{group.heading}</p>
                        <TrackedSeriesMaturityBadge candidates={group.candidates} />
                      </div>
                      {renderCandidateTable(group.candidates, { showNumberColumn: true })}
                    </div>
                  ))}
                </div>
              </section>
            ) : null}

            {groups.newSeries.length > 0 ? (
              <section>
                <h3 className="mb-2 text-sm font-semibold">New series (not yet tracked)</h3>
                <div className="flex flex-col gap-4">
                  {groups.newSeries.map((group) => {
                    const overviewState = overviews[group.key];
                    const fillState = seriesFill[group.key];
                    const maturity = computeNewSeriesMaturity(group.candidates);
                    const looksIncomplete = maturity.totalHint != null && maturity.foundCount < maturity.totalHint;
                    return (
                      <div key={group.key} className="rounded-lg border">
                        <div className="flex flex-wrap items-center justify-between gap-2 border-b bg-muted/50 px-3 py-1.5">
                          <div className="flex flex-col gap-0.5">
                            <p className="text-xs font-medium">{group.heading}</p>
                            <NewSeriesMaturityBadge candidates={group.candidates} />
                          </div>
                          <div className="flex items-center gap-1">
                            {looksIncomplete ? (
                              <Button
                                type="button"
                                variant="ghost"
                                size="sm"
                                className="h-7 gap-1 px-2 text-xs"
                                disabled={fillState?.loading}
                                onClick={() => handleFindRestOfSeries(group)}
                              >
                                <SearchIcon className="h-3.5 w-3.5" />
                                {fillState?.loading ? "Searching…" : "Find the rest of this series"}
                              </Button>
                            ) : null}
                            <Button
                              type="button"
                              variant="ghost"
                              size="sm"
                              className="h-7 gap-1 px-2 text-xs"
                              disabled={overviewState?.loading}
                              onClick={() => handleFetchSeriesOverview(group)}
                            >
                              <SparklesIcon className="h-3.5 w-3.5" />
                              {overviewState?.loading
                                ? "Summarizing…"
                                : overviewState?.text
                                  ? "Refresh overview"
                                  : "Series Overview"}
                            </Button>
                          </div>
                        </div>
                        {fillState?.error ? (
                          <p className="border-b px-3 py-2 text-xs text-muted-foreground">{fillState.error}</p>
                        ) : null}
                        {overviewState?.text ? (
                          <p className="border-b bg-muted/20 px-3 py-2 text-xs text-muted-foreground">
                            {overviewState.text}
                          </p>
                        ) : overviewState?.error ? (
                          <p className="border-b px-3 py-2 text-xs text-destructive">{overviewState.error}</p>
                        ) : null}
                        {renderCandidateTable(group.candidates, { showNumberColumn: true })}
                      </div>
                    );
                  })}
                </div>
              </section>
            ) : null}

            {groups.standalone.length > 0 ? (
              <section>
                <h3 className="mb-2 text-sm font-semibold">New standalone book{groups.standalone.length > 1 ? "s" : ""}</h3>
                <div className="rounded-lg border">
                  {renderCandidateTable(groups.standalone, { showNumberColumn: false })}
                </div>
              </section>
            ) : null}
          </div>
        )}
        {error && candidates.length > 0 ? (
          <p className="text-xs text-destructive">{error}</p>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
