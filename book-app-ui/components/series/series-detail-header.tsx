"use client";

import { useState } from "react";
import Link from "next/link";
import { AlertTriangleIcon, MoreHorizontalIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import Spinner from "@/components/ui/spinner";

export type SeriesDetailHeaderSeries = {
  name: string;
  author?: string | null;
  description?: string | null;
  is_finished?: boolean;
  series_status?: string | null;
  next_unread_book_number?: number | null;
  next_upcoming_book_number?: number | null;
};

export type SeriesDetailHeaderStats = {
  unread: number;
  read: number;
  total: number;
  upcoming: number;
  missing: number;
  needsVerification: number;
};

export type SeriesDetailCheckState = {
  loading: boolean;
  progress: number;
  currentPass: string | null;
  stillChecking: boolean;
};

export type SeriesDetailHeaderProps = {
  /** Phone/tablet layout: condensed stats and an overflow menu for secondary actions. */
  compact: boolean;
  canEdit: boolean;
  series: SeriesDetailHeaderSeries;
  stats: SeriesDetailHeaderStats;
  needsVerificationOnly: boolean;
  onToggleNeedsVerification: () => void;
  nextBookNumber: number | null;
  viewAllSeriesHref: string;
  check: SeriesDetailCheckState;
  finishedToggleSaving: boolean;
  deleteSeriesSaving: boolean;
  onAddBook: () => void;
  onCheckForNew: () => void;
  onSearchNextBookOnline: () => void;
  onSeriesRecap: () => void;
  onNormalizeTitles: () => void;
  onToggleFinished: () => void;
  onDeleteSeries: () => void;
};

const NEEDS_VERIFICATION_HINT =
  "Books flagged as upcoming/available with no confirmed release date -- click to filter to just these";

function NeedsVerificationChip({
  count,
  active,
  onToggle,
  className,
}: {
  count: number;
  active: boolean;
  onToggle: () => void;
  className?: string;
}) {
  return (
    <button
      type="button"
      title={NEEDS_VERIFICATION_HINT}
      aria-pressed={active}
      onClick={onToggle}
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-semibold transition-colors ${
        active
          ? "border-amber-400 bg-amber-200 text-amber-900"
          : "border-amber-300 bg-amber-100 text-amber-800 hover:bg-amber-200"
      }${className ? ` ${className}` : ""}`}
    >
      <AlertTriangleIcon className="h-3 w-3" />
      Needs date verification {count}
    </button>
  );
}

function CheckProgress({ check }: { check: SeriesDetailCheckState }) {
  return (
    <div className="flex min-w-[240px] items-center gap-2 rounded border bg-background px-2 py-1 text-xs">
      <Spinner />
      <div className="w-32 overflow-hidden rounded-full bg-slate-200">
        <div
          className="h-1.5 bg-slate-700 transition-all duration-500"
          style={{ width: `${Math.max(4, check.progress)}%` }}
        />
      </div>
      <span className={check.stillChecking ? "animate-pulse text-muted-foreground" : "text-muted-foreground"}>
        {check.stillChecking ? "Still checking..." : `${check.progress}%`}
      </span>
      {check.currentPass ? <span className="text-muted-foreground">{check.currentPass}</span> : null}
    </div>
  );
}

/**
 * Title, counters, and the series-level action bar for /series/[seriesId].
 *
 * The action set is identical on every device -- only its shape changes. On
 * desktop the nine actions sit in a single wrapping row; on phone/tablet
 * that row would stack four or five deep above the first book, so only Add
 * Book and Check for New stay inline and the rest (including the
 * destructive delete, kept visually separated at the bottom) move into an
 * overflow popover.
 */
export function SeriesDetailHeader({
  compact,
  canEdit,
  series,
  stats,
  needsVerificationOnly,
  onToggleNeedsVerification,
  nextBookNumber,
  viewAllSeriesHref,
  check,
  finishedToggleSaving,
  deleteSeriesSaving,
  onAddBook,
  onCheckForNew,
  onSearchNextBookOnline,
  onSeriesRecap,
  onNormalizeTitles,
  onToggleFinished,
  onDeleteSeries,
}: SeriesDetailHeaderProps) {
  const [overflowOpen, setOverflowOpen] = useState(false);

  const searchOnlineLabel = `Search Book ${nextBookNumber ?? "?"} Online`;
  const searchOnlineHint = nextBookNumber
    ? `Search online for Book ${nextBookNumber} -- use this if "Check for New" doesn't find a book you know exists`
    : "Search online for the next book in this series -- use this if \"Check for New\" doesn't find a book you know exists";
  const finishedLabel = finishedToggleSaving
    ? "Saving..."
    : series.is_finished
      ? "Move to unfinished"
      : "Move to finished";

  function runFromOverflow(action: () => void) {
    setOverflowOpen(false);
    action();
  }

  return (
    <div className="space-y-1.5 rounded-lg border bg-card/60 px-3 py-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <h1 className="text-xl font-bold leading-tight">{series.name}</h1>
          <span className="text-sm text-muted-foreground">{series.author || "Unknown author"}</span>
        </div>

        {compact ? null : (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
            <span>Unread <span className="font-semibold text-foreground">{stats.unread}</span></span>
            <span>Read <span className="font-semibold text-foreground">{stats.read}</span></span>
            <span>Total <span className="font-semibold text-foreground">{stats.total}</span></span>
            <span>Upcoming <span className="font-semibold text-foreground">{stats.upcoming}</span></span>
            {stats.needsVerification > 0 ? (
              <NeedsVerificationChip
                count={stats.needsVerification}
                active={needsVerificationOnly}
                onToggle={onToggleNeedsVerification}
              />
            ) : null}
            <span className="text-muted-foreground/50">|</span>
            <span>Status <span className="font-semibold text-foreground">{series.series_status || "Unknown"}</span></span>
            <span>Next unread <span className="font-semibold text-foreground">{series.next_unread_book_number ?? "—"}</span></span>
            <span>Next upcoming <span className="font-semibold text-foreground">{series.next_upcoming_book_number ?? "—"}</span></span>
            <span>Missing <span className="font-semibold text-foreground">{stats.missing}</span></span>
          </div>
        )}
      </div>

      {compact ? (
        <div className="space-y-1">
          <div className="grid grid-cols-4 gap-1 text-center text-[11px] text-muted-foreground">
            {[
              { label: "Unread", value: stats.unread },
              { label: "Read", value: stats.read },
              { label: "Total", value: stats.total },
              { label: "Upcoming", value: stats.upcoming },
            ].map((counter) => (
              <div key={counter.label} className="rounded border bg-background/60 px-1 py-0.5">
                <div className="text-sm font-semibold text-foreground">{counter.value}</div>
                {counter.label}
              </div>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-muted-foreground">
            <span>Status <span className="font-semibold text-foreground">{series.series_status || "Unknown"}</span></span>
            <span>Next unread <span className="font-semibold text-foreground">{series.next_unread_book_number ?? "—"}</span></span>
            <span>Next upcoming <span className="font-semibold text-foreground">{series.next_upcoming_book_number ?? "—"}</span></span>
            <span>Missing <span className="font-semibold text-foreground">{stats.missing}</span></span>
          </div>
          {stats.needsVerification > 0 ? (
            <NeedsVerificationChip
              count={stats.needsVerification}
              active={needsVerificationOnly}
              onToggle={onToggleNeedsVerification}
              className="text-[11px]"
            />
          ) : null}
        </div>
      ) : null}

      {compact ? (
        <div className="space-y-1.5">
          <div className="flex items-center gap-2">
            {canEdit ? (
              <Button type="button" variant="outline" size="sm" className="flex-1" onClick={onAddBook}>
                Add Book
              </Button>
            ) : null}
            {canEdit ? (
              <Button
                type="button"
                variant="secondary"
                size="sm"
                className="flex-1"
                onClick={onCheckForNew}
                disabled={check.loading}
              >
                {check.loading ? "Checking…" : "Check for New"}
              </Button>
            ) : null}

            <Popover open={overflowOpen} onOpenChange={setOverflowOpen}>
              <PopoverTrigger asChild>
                <Button type="button" variant="outline" size="icon-sm" aria-label="More series actions" title="More series actions">
                  <MoreHorizontalIcon />
                </Button>
              </PopoverTrigger>
              <PopoverContent align="end" className="w-60 p-1">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="w-full justify-start"
                  title={searchOnlineHint}
                  onClick={() => runFromOverflow(onSearchNextBookOnline)}
                >
                  {searchOnlineLabel}
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="w-full justify-start"
                  title="Opens ChatGPT in a new tab with a pre-filled recap prompt for this series"
                  onClick={() => runFromOverflow(onSeriesRecap)}
                >
                  Series Recap
                </Button>
                {canEdit ? (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="w-full justify-start"
                    onClick={() => runFromOverflow(onNormalizeTitles)}
                  >
                    Optional Title Normalization
                  </Button>
                ) : null}
                {canEdit ? (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="w-full justify-start"
                    disabled={finishedToggleSaving}
                    onClick={() => runFromOverflow(onToggleFinished)}
                  >
                    {finishedLabel}
                  </Button>
                ) : null}

                <div className="my-1 border-t" />
                <Button type="button" variant="ghost" size="sm" className="w-full justify-start" asChild>
                  <Link href="/books" onClick={() => setOverflowOpen(false)}>Back to Library</Link>
                </Button>
                <Button type="button" variant="ghost" size="sm" className="w-full justify-start" asChild>
                  <Link href={viewAllSeriesHref} onClick={() => setOverflowOpen(false)}>View all series</Link>
                </Button>

                {canEdit ? (
                  <>
                    <div className="my-1 border-t" />
                    <Button
                      type="button"
                      variant="destructive"
                      size="sm"
                      className="w-full justify-start"
                      disabled={deleteSeriesSaving}
                      onClick={() => runFromOverflow(onDeleteSeries)}
                    >
                      {deleteSeriesSaving ? "Deleting series..." : "Delete series + books"}
                    </Button>
                  </>
                ) : null}
              </PopoverContent>
            </Popover>
          </div>

          {check.loading ? <CheckProgress check={check} /> : null}
        </div>
      ) : (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            {canEdit ? (
              <Button type="button" variant="outline" size="sm" onClick={onAddBook}>
                Add Book
              </Button>
            ) : null}
            {canEdit ? (
              <Button
                type="button"
                variant="secondary"
                size="sm"
                onClick={onCheckForNew}
                disabled={check.loading}
              >
                {check.loading ? `Checking ${series.name}…` : `Check ${series.name} for New`}
              </Button>
            ) : null}
            <Button type="button" variant="outline" size="sm" onClick={onSearchNextBookOnline} title={searchOnlineHint}>
              {searchOnlineLabel}
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={onSeriesRecap}
              title="Opens ChatGPT in a new tab with a pre-filled recap prompt for this series"
            >
              Series Recap
            </Button>
            {check.loading ? <CheckProgress check={check} /> : null}
            {canEdit ? (
              <Button type="button" variant="outline" size="sm" onClick={onNormalizeTitles}>
                Optional Title Normalization
              </Button>
            ) : null}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {canEdit ? (
              <Button variant="outline" size="sm" onClick={onToggleFinished} disabled={finishedToggleSaving}>
                {finishedLabel}
              </Button>
            ) : null}
            <Link href="/books">
              <Button variant="outline" size="sm">Back to Library</Button>
            </Link>
            <Link href={viewAllSeriesHref}>
              <Button variant="secondary" size="sm">View all series</Button>
            </Link>
            {canEdit ? (
              <Button
                type="button"
                variant="destructive"
                size="sm"
                onClick={onDeleteSeries}
                disabled={deleteSeriesSaving}
              >
                {deleteSeriesSaving ? "Deleting series..." : "Delete series + books"}
              </Button>
            ) : null}
          </div>
        </div>
      )}

      {series.description ? (
        <p className="line-clamp-2 max-w-4xl text-xs leading-5 text-muted-foreground">{series.description}</p>
      ) : null}
    </div>
  );
}
