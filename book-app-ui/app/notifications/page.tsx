"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { BellIcon, HelpCircleIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useToast } from "@/components/ui/use-toast";
import { fetchApiWithFallback } from "@/lib/api-client";
import { refreshNotificationsBadgeCount } from "@/lib/notifications-badge";

type NotificationItem = {
  id: number;
  series_id: number | null;
  series_name: string | null;
  count_new_books: number;
  created_at: string;
};

type CandidateReviewUrls = {
  amazon_ku_search: string;
  google_search: string;
  asin_lookup?: string | null;
};

type CandidateNotificationItem = {
  id: number;
  series_id: number | null;
  series_name: string | null;
  candidate_title: string;
  candidate_number: number | null;
  overall_confidence: string | null;
  provider_confidence: string | null;
  isbn13: string | null;
  publication_date: string | null;
  asin: string | null;
  author: string | null;
  source_url: string | null;
  provider: string | null;
  series_name_hint: string | null;
  reason_flags: string[];
  created_at: string;
  last_seen_at: string;
  review_urls: CandidateReviewUrls;
};

const REASON_FLAG_LABELS: Record<string, string> = {
  number_inferred_from_title: "Number guessed from title",
  missing_series_number: "No series number found",
};

function formatReasonFlag(flag: string): string {
  return REASON_FLAG_LABELS[flag] || flag.replace(/_/g, " ");
}

function formatNotificationDate(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return parsed.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

/**
 * Durable series-level discovery notifications (see the "Durable
 * Series-Level Discovery Notifications" design chat's finalized spec) --
 * a persistent, browsable history that survives long absences, entirely
 * separate from the ephemeral in-session popup shown by the series/
 * settings pages right after a Check Now / Full Auto Discovery run.
 * Nothing here depends on that popup, and nothing there depends on this.
 */
export default function NotificationsPage() {
  const { toast } = useToast();
  const [items, setItems] = useState<NotificationItem[] | null>(null);
  const [dismissingId, setDismissingId] = useState<number | null>(null);
  const [dismissingAll, setDismissingAll] = useState(false);
  const [candidates, setCandidates] = useState<CandidateNotificationItem[] | null>(null);
  const [candidateActionId, setCandidateActionId] = useState<number | null>(null);

  const loadNotifications = useCallback(async () => {
    try {
      const response = await fetchApiWithFallback("/notifications/unseen", { cache: "no-store" });
      const data = await response.json();
      setItems(Array.isArray(data) ? data : []);
    } catch {
      setItems([]);
      toast({
        title: "Couldn't load notifications",
        description: "Please try again.",
      });
    }
  }, [toast]);

  const loadCandidates = useCallback(async () => {
    try {
      const response = await fetchApiWithFallback("/notifications/candidates", { cache: "no-store" });
      const data = await response.json();
      setCandidates(Array.isArray(data) ? data : []);
    } catch {
      setCandidates([]);
      toast({
        title: "Couldn't load candidate books",
        description: "Please try again.",
      });
    }
  }, [toast]);

  useEffect(() => {
    // loadNotifications/loadCandidates set state before their first await --
    // standard "fetch on mount" pattern, not derived state.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadNotifications();
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadCandidates();
  }, [loadNotifications, loadCandidates]);

  async function handleDismiss(id: number) {
    setDismissingId(id);
    try {
      await fetchApiWithFallback(`/notifications/${id}/dismiss`, { method: "POST" });
      setItems((current) => (current ? current.filter((item) => item.id !== id) : current));
      refreshNotificationsBadgeCount();
    } catch {
      toast({
        title: "Couldn't dismiss notification",
        description: "Please try again.",
      });
    } finally {
      setDismissingId(null);
    }
  }

  async function handleDismissAll() {
    setDismissingAll(true);
    try {
      await fetchApiWithFallback("/notifications/dismiss", { method: "POST" });
      setItems([]);
      refreshNotificationsBadgeCount();
    } catch {
      toast({
        title: "Couldn't dismiss all notifications",
        description: "Please try again.",
      });
    } finally {
      setDismissingAll(false);
    }
  }

  async function handleAddCandidateToSeries(id: number) {
    setCandidateActionId(id);
    try {
      const response = await fetchApiWithFallback(`/notifications/candidates/${id}/add`, { method: "POST" });
      const data = await response.json();
      setCandidates((current) => (current ? current.filter((item) => item.id !== id) : current));
      toast({
        title: "Added to series",
        description: data?.title ? `"${data.title}" was added to your library.` : undefined,
      });
    } catch {
      toast({
        title: "Couldn't add book",
        description: "Please try again.",
      });
    } finally {
      setCandidateActionId(null);
    }
  }

  function handleReviewCandidate(candidate: CandidateNotificationItem) {
    const urls = [
      candidate.review_urls.amazon_ku_search,
      candidate.review_urls.google_search,
      candidate.review_urls.asin_lookup,
    ].filter((url): url is string => Boolean(url));
    for (const url of urls) {
      window.open(url, "_blank", "noopener,noreferrer");
    }
  }

  async function handleIgnoreCandidate(id: number) {
    setCandidateActionId(id);
    try {
      await fetchApiWithFallback(`/notifications/candidates/${id}/ignore`, { method: "POST" });
      setCandidates((current) => (current ? current.filter((item) => item.id !== id) : current));
    } catch {
      toast({
        title: "Couldn't dismiss candidate",
        description: "Please try again.",
      });
    } finally {
      setCandidateActionId(null);
    }
  }

  return (
    <div className="mx-auto max-w-2xl space-y-4 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-muted-foreground">Library</p>
          <h1 className="flex items-center gap-2 text-2xl font-bold">
            <BellIcon className="h-6 w-6" />
            Notifications
          </h1>
        </div>
        <div className="flex items-center gap-2">
          {items && items.length > 0 ? (
            <Button variant="outline" size="sm" onClick={handleDismissAll} disabled={dismissingAll}>
              {dismissingAll ? "Dismissing..." : "Dismiss all"}
            </Button>
          ) : null}
          <Link href="/books">
            <Button variant="outline" size="sm">
              Back to Library
            </Button>
          </Link>
        </div>
      </div>

      {candidates && candidates.length > 0 ? (
        <section className="space-y-2">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-muted-foreground">
            <HelpCircleIcon className="h-4 w-4" />
            Review candidate books
          </h2>
          <ul className="flex flex-col gap-2">
            {candidates.map((candidate) => (
              <li key={candidate.id}>
                <Card>
                  <CardContent className="flex flex-col gap-2 py-3">
                    <div>
                      <p className="text-sm font-medium">
                        {candidate.candidate_title}
                        {candidate.candidate_number != null ? ` (#${candidate.candidate_number})` : ""}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {candidate.series_id ? (
                          <Link
                            href={`/series/${candidate.series_id}`}
                            className="underline underline-offset-2"
                          >
                            {candidate.series_name || "a series"}
                          </Link>
                        ) : (
                          candidate.series_name || candidate.series_name_hint || "Unlinked series"
                        )}
                        {candidate.author ? ` \u2022 ${candidate.author}` : ""}
                      </p>
                      {candidate.reason_flags.length > 0 ? (
                        <p className="mt-1 text-xs text-muted-foreground">
                          {candidate.reason_flags.map(formatReasonFlag).join(" \u2022 ")}
                        </p>
                      ) : null}
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Button
                        variant="default"
                        size="sm"
                        onClick={() => handleAddCandidateToSeries(candidate.id)}
                        disabled={candidateActionId === candidate.id}
                      >
                        Add to Series
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => handleReviewCandidate(candidate)}
                        disabled={candidateActionId === candidate.id}
                      >
                        Review
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleIgnoreCandidate(candidate.id)}
                        disabled={candidateActionId === candidate.id}
                      >
                        Do Not Add
                      </Button>
                    </div>
                  </CardContent>
                </Card>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {items === null ? (
        <p className="text-sm text-muted-foreground">Loading notifications...</p>
      ) : items.length === 0 ? (
        <Card>
          <CardContent className="py-8 text-center text-sm text-muted-foreground">
            No discovery notifications yet. When Check Now or Full Auto Discovery finds new books for a series, it
            will show up here -- even if you&rsquo;re not around when it happens.
          </CardContent>
        </Card>
      ) : (
        <ul className="flex flex-col gap-2">
          {items.map((item) => (
            <li key={item.id}>
              <Card>
                <CardContent className="flex items-center justify-between gap-3 py-3">
                  <p className="text-sm">
                    <span className="font-medium">
                      {item.count_new_books} new book{item.count_new_books === 1 ? "" : "s"}
                    </span>{" "}
                    added to{" "}
                    {item.series_id ? (
                      <Link href={`/series/${item.series_id}`} className="font-medium underline underline-offset-2">
                        {item.series_name || "a series"}
                      </Link>
                    ) : (
                      <span className="font-medium">{item.series_name || "a series"}</span>
                    )}{" "}
                    on {formatNotificationDate(item.created_at)}.
                  </p>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => handleDismiss(item.id)}
                    disabled={dismissingId === item.id}
                  >
                    {dismissingId === item.id ? "Dismissing..." : "Dismiss"}
                  </Button>
                </CardContent>
              </Card>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
