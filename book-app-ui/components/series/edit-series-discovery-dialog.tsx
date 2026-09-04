"use client";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { CANONICAL_SOURCE_OPTIONS, getCanonicalPageSearchUrls, type CanonicalSource } from "@/lib/book-format";

export type EditSeriesDiscoveryDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  seriesName: string;
  seriesAuthor?: string | null;
  canonicalUrl: string;
  canonicalSource: CanonicalSource | "";
  verifiedVolumeCount: string;
  onCanonicalUrlChange: (value: string) => void;
  onCanonicalSourceChange: (value: CanonicalSource | "") => void;
  onVerifiedVolumeCountChange: (value: string) => void;
  onSave: () => void;
  onSaveAndCheckNow: () => void;
  saving: boolean;
  checking: boolean;
};

/**
 * Lets a user attach/update Guided Discovery's canonical fields
 * (canonical_url/canonical_source/verified_volume_count) on a series that
 * ALREADY exists -- 2026-09-03 fix for a real gap found live on Backyard
 * Starship: those fields could previously only ever be set at the moment
 * a brand-new series was first created (Add Book form's "new series"
 * section), with no path to add them to a series created before Guided
 * Discovery existed (or one where the user simply didn't have the URL
 * handy yet). The backend's PUT /series/{id} already supported updating
 * these fields (schemas.SeriesBase + crud.update_series's exclude_unset
 * partial-update pattern) -- this dialog is the missing UI in front of it.
 */
export function EditSeriesDiscoveryDialog({
  open,
  onOpenChange,
  seriesName,
  seriesAuthor,
  canonicalUrl,
  canonicalSource,
  verifiedVolumeCount,
  onCanonicalUrlChange,
  onCanonicalSourceChange,
  onVerifiedVolumeCountChange,
  onSave,
  onSaveAndCheckNow,
  saving,
  checking,
}: EditSeriesDiscoveryDialogProps) {
  const busy = saving || checking;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Guided Discovery Settings</DialogTitle>
          <DialogDescription>
            If you know the exact page you source &quot;{seriesName}&quot; from and how many volumes currently exist
            there, saving it here lets discovery find every volume -- including for a series you&apos;ve been
            tracking for a while.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1">
            <div className="flex items-center justify-between gap-2">
              <Label htmlFor="edit-series-canonical-url">Source URL</Label>
              <div className="flex items-center gap-1">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 text-[11px]"
                  onClick={() => {
                    // UI-only assist -- opens a search tab so the user can find and
                    // paste the series' real canonical page below. No scraping, no
                    // discovery/provider logic.
                    const { goodreads } = getCanonicalPageSearchUrls(seriesName, seriesAuthor);
                    window.open(goodreads, "_blank", "noopener,noreferrer");
                  }}
                >
                  Find on Goodreads
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 text-[11px]"
                  onClick={() => {
                    const { google } = getCanonicalPageSearchUrls(seriesName, seriesAuthor);
                    window.open(google, "_blank", "noopener,noreferrer");
                  }}
                >
                  Find on Google
                </Button>
              </div>
            </div>
            <Input
              id="edit-series-canonical-url"
              value={canonicalUrl}
              onChange={(event) => onCanonicalUrlChange(event.target.value)}
              placeholder="https://..."
            />
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <Label htmlFor="edit-series-canonical-source">Source</Label>
              <select
                id="edit-series-canonical-source"
                value={canonicalSource}
                onChange={(event) => onCanonicalSourceChange(event.target.value as CanonicalSource | "")}
                className="h-9 w-full rounded-md border bg-background px-2 text-sm"
              >
                <option value="">Select...</option>
                {CANONICAL_SOURCE_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
              {canonicalSource === "PublisherSite" ? (
                <p className="text-[11px] text-muted-foreground">
                  Good for an author&apos;s personal site/blog, which sometimes announces upcoming books before
                  Goodreads or Amazon do. Books found here will need your review before being added, since a personal
                  site&apos;s announcement isn&apos;t a vetted catalog listing.
                </p>
              ) : null}
            </div>
            <div className="space-y-1">
              <Label htmlFor="edit-series-verified-count">Verified volume count</Label>
              <Input
                id="edit-series-verified-count"
                value={verifiedVolumeCount}
                onChange={(event) => onVerifiedVolumeCountChange(event.target.value)}
                placeholder="e.g. 35"
              />
            </div>
          </div>
        </div>

        <DialogFooter showCloseButton>
          <Button type="button" variant="outline" onClick={onSave} disabled={busy}>
            {saving ? "Saving..." : "Save"}
          </Button>
          <Button type="button" variant="secondary" onClick={onSaveAndCheckNow} disabled={busy}>
            {saving ? "Saving..." : checking ? "Checking..." : "Save & Run Canonical Discovery Now"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
