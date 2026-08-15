"use client";

import { useCallback, useEffect, useState } from "react";
import { ExternalLinkIcon, StarIcon } from "lucide-react";

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
  series_number: number | null;
  status: "available" | "upcoming";
  release_date: string | null;
  source_url: string | null;
  isbn13: string | null;
  provider: string | null;
};

type AuthorDiscoveryResponse = {
  author: string;
  candidates: AuthorDiscoveryCandidate[];
  provider_failures: { provider: string; error: string }[];
  all_providers_failed: boolean;
};

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
      runDiscovery();
    }
  }, [open, author, runDiscovery]);

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
          <div className="max-h-[60vh] overflow-y-auto rounded-lg border">
            <Table className="text-sm">
              <TableHeader>
                <TableRow>
                  <TableHead>Title</TableHead>
                  <TableHead>Series</TableHead>
                  <TableHead>#</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Date</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {candidates.map((candidate) => {
                  const key = candidateKey(candidate);
                  const alreadyAdded = addedKeys.has(key);
                  return (
                    <TableRow key={key}>
                      <TableCell className="max-w-[220px] truncate" title={candidate.title}>
                        {candidate.title}
                      </TableCell>
                      <TableCell className="max-w-[160px] truncate" title={candidate.series_name || undefined}>
                        {candidate.series_name ? (
                          candidate.series_name
                        ) : (
                          <span className="text-muted-foreground">Standalone</span>
                        )}
                        {candidate.series_name && !candidate.matched_series_id ? (
                          <span className="block text-[11px] text-muted-foreground">not yet tracked</span>
                        ) : null}
                      </TableCell>
                      <TableCell>{candidate.series_number ?? "—"}</TableCell>
                      <TableCell>
                        <span className={getStatusChipClass(candidate.status, "compact")}>{candidate.status}</span>
                      </TableCell>
                      <TableCell>{formatDate(candidate.release_date)}</TableCell>
                      <TableCell className="text-right">
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
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        )}
        {error && candidates.length > 0 ? (
          <p className="text-xs text-destructive">{error}</p>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
