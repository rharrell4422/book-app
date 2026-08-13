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
import { Label } from "@/components/ui/label";

export function BookSummaryDialog({
  open,
  bookTitle,
  onOpenChange,
  summaryDraft,
  onSummaryDraftChange,
  notesDraft,
  onNotesDraftChange,
  canEdit,
  onSave,
  saving,
}: {
  open: boolean;
  bookTitle: string | null | undefined;
  onOpenChange: (open: boolean) => void;
  summaryDraft: string;
  onSummaryDraftChange: (value: string) => void;
  notesDraft: string;
  onNotesDraftChange: (value: string) => void;
  canEdit: boolean;
  onSave: () => void;
  saving: boolean;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{bookTitle || "Book summary"}</DialogTitle>
          <DialogDescription>
            Review the fetched summary and add your own notes without stretching the table rows.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="series-book-summary">Summary</Label>
            <textarea
              id="series-book-summary"
              value={summaryDraft}
              onChange={(event) => onSummaryDraftChange(event.target.value)}
              readOnly={!canEdit}
              className="min-h-32 w-full rounded-lg border border-input bg-transparent px-3 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="series-book-notes">My notes</Label>
            <textarea
              id="series-book-notes"
              value={notesDraft}
              onChange={(event) => onNotesDraftChange(event.target.value)}
              readOnly={!canEdit}
              className="min-h-28 w-full rounded-lg border border-input bg-transparent px-3 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
            />
          </div>
        </div>

        <DialogFooter showCloseButton>
          {canEdit ? (
            <Button type="button" onClick={onSave} disabled={saving}>
              {saving ? "Saving..." : "Save changes"}
            </Button>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
