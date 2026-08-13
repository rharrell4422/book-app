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

export type EditBookFormState = {
  id: number | null;
  title: string;
  author: string;
  bookNumber: string;
  status: "unread" | "upcoming" | "available" | "read";
  date: string;
};

export function EditBookDialog({
  open,
  onOpenChange,
  form,
  onFormChange,
  onSave,
  saving,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  form: EditBookFormState;
  onFormChange: (updater: (prev: EditBookFormState) => EditBookFormState) => void;
  onSave: () => void;
  saving: boolean;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Edit Book</DialogTitle>
          <DialogDescription>
            Update title, author, number, status, and date for this book.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="edit-book-title">Title</Label>
            <input
              id="edit-book-title"
              value={form.title}
              onChange={(event) => onFormChange((prev) => ({ ...prev, title: event.target.value }))}
              placeholder="Book title"
              className="h-9 w-full rounded border bg-white px-2 text-sm"
            />
          </div>

          <div className="space-y-1">
            <Label htmlFor="edit-book-author">Author</Label>
            <input
              id="edit-book-author"
              value={form.author}
              onChange={(event) => onFormChange((prev) => ({ ...prev, author: event.target.value }))}
              placeholder="Author name"
              className="h-9 w-full rounded border bg-white px-2 text-sm"
            />
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <Label htmlFor="edit-book-number">Book #</Label>
              <input
                id="edit-book-number"
                value={form.bookNumber}
                onChange={(event) => onFormChange((prev) => ({ ...prev, bookNumber: event.target.value }))}
                placeholder="e.g. 24"
                className="h-9 w-full rounded border bg-white px-2 text-sm"
              />
            </div>

            <div className="space-y-1">
              <Label htmlFor="edit-book-status">Status</Label>
              <select
                id="edit-book-status"
                value={form.status}
                onChange={(event) =>
                  onFormChange((prev) => ({
                    ...prev,
                    status: event.target.value as EditBookFormState["status"],
                  }))
                }
                className="h-9 w-full rounded border bg-white px-2 text-sm"
              >
                <option value="unread">unread</option>
                <option value="upcoming">upcoming</option>
                <option value="available">available</option>
                <option value="read">read</option>
              </select>
            </div>
          </div>

          <div className="space-y-1">
            <Label htmlFor="edit-book-date">Date</Label>
            <input
              id="edit-book-date"
              value={form.date}
              onChange={(event) => onFormChange((prev) => ({ ...prev, date: event.target.value }))}
              placeholder={form.status === "read" ? "Read date (YYYY-MM-DD)" : "Release date (YYYY-MM-DD)"}
              className="h-9 w-full rounded border bg-white px-2 text-sm"
            />
          </div>
        </div>

        <DialogFooter showCloseButton>
          <Button type="button" onClick={onSave} disabled={saving}>
            {saving ? "Saving..." : "Save changes"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
