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

export type AddBookStatus = "upcoming" | "unread" | "available" | "read";

export function AddBookDialog({
  open,
  onOpenChange,
  title,
  onTitleChange,
  bookNumber,
  onBookNumberChange,
  status,
  onStatusChange,
  date,
  onDateChange,
  onSave,
  saving,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  onTitleChange: (value: string) => void;
  bookNumber: string;
  onBookNumberChange: (value: string) => void;
  status: AddBookStatus;
  onStatusChange: (value: AddBookStatus) => void;
  date: string;
  onDateChange: (value: string) => void;
  onSave: () => void;
  saving: boolean;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Add Book</DialogTitle>
          <DialogDescription>
            Add a new book directly to this series while you review release intel.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="add-book-title">Title</Label>
            <input
              id="add-book-title"
              value={title}
              onChange={(event) => onTitleChange(event.target.value)}
              placeholder="Book title"
              className="h-9 w-full rounded border bg-white px-2 text-sm"
            />
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <Label htmlFor="add-book-number">Book #</Label>
              <input
                id="add-book-number"
                type="number"
                step="0.1"
                min="0"
                value={bookNumber}
                onChange={(event) => onBookNumberChange(event.target.value)}
                placeholder="e.g. 28"
                className="h-9 w-full rounded border bg-white px-2 text-sm"
              />
            </div>

            <div className="space-y-1">
              <Label htmlFor="add-book-status">Status</Label>
              <select
                id="add-book-status"
                value={status}
                onChange={(event) => onStatusChange(event.target.value as AddBookStatus)}
                className="h-9 w-full rounded border bg-white px-2 text-sm"
              >
                <option value="upcoming">upcoming</option>
                <option value="unread">unread</option>
                <option value="available">available</option>
                <option value="read">read</option>
              </select>
            </div>
          </div>

          <div className="space-y-1">
            <Label htmlFor="add-book-date">Date (optional)</Label>
            <input
              id="add-book-date"
              value={date}
              onChange={(event) => onDateChange(event.target.value)}
              placeholder={status === "read" ? "Read date (MM-DD-YYYY)" : "Release date (MM-DD-YYYY)"}
              className="h-9 w-full rounded border bg-white px-2 text-sm"
            />
          </div>
        </div>

        <DialogFooter showCloseButton>
          <Button type="button" variant="secondary" onClick={onSave} disabled={saving}>
            {saving ? "Adding..." : "Add Book"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
