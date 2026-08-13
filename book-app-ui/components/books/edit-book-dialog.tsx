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
import type { BookStatus } from "@/lib/book-format";

export type EditBookFormState = {
  id: number | null;
  title: string;
  author: string;
  seriesName: string;
  bookNumber: string;
  status: BookStatus;
  date: string;
};

export const EMPTY_EDIT_BOOK_FORM: EditBookFormState = {
  id: null,
  title: "",
  author: "",
  seriesName: "",
  bookNumber: "",
  status: "unread",
  date: "",
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
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Edit Book</DialogTitle>
          <DialogDescription>
            Update core book metadata from the library without leaving this page.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1 sm:col-span-2">
            <Label htmlFor="edit-book-title">Title</Label>
            <Input
              id="edit-book-title"
              value={form.title}
              onChange={(event) => onFormChange((prev) => ({ ...prev, title: event.target.value }))}
            />
          </div>

          <div className="space-y-1 sm:col-span-2">
            <Label htmlFor="edit-book-author">Author</Label>
            <Input
              id="edit-book-author"
              value={form.author}
              onChange={(event) => onFormChange((prev) => ({ ...prev, author: event.target.value }))}
            />
          </div>

          <div className="space-y-1 sm:col-span-2">
            <Label htmlFor="edit-book-series">Series (optional)</Label>
            <Input
              id="edit-book-series"
              value={form.seriesName}
              onChange={(event) => onFormChange((prev) => ({ ...prev, seriesName: event.target.value }))}
              placeholder="Series name"
            />
          </div>

          <div className="space-y-1">
            <Label htmlFor="edit-book-number">Book #</Label>
            <Input
              id="edit-book-number"
              value={form.bookNumber}
              onChange={(event) => onFormChange((prev) => ({ ...prev, bookNumber: event.target.value }))}
              placeholder="e.g. 24"
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
                  status: event.target.value as BookStatus,
                }))
              }
              className="h-9 w-full rounded border bg-background px-2 text-sm"
            >
              <option value="unread">unread</option>
              <option value="available">available</option>
              <option value="upcoming">upcoming</option>
              <option value="read">read</option>
            </select>
          </div>

          <div className="space-y-1 sm:col-span-2">
            <Label htmlFor="edit-book-date">Date</Label>
            <Input
              id="edit-book-date"
              value={form.date}
              onChange={(event) => onFormChange((prev) => ({ ...prev, date: event.target.value }))}
              placeholder={form.status === "read" ? "Read date" : "Release date"}
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
