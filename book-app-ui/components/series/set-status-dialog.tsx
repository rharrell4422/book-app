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

export type BookStatusAction = "read" | "unread" | "upcoming" | "available";

export function SetStatusDialog({
  open,
  onOpenChange,
  statusAction,
  onStatusActionChange,
  statusDate,
  onStatusDateChange,
  onSave,
  saving,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  statusAction: BookStatusAction;
  onStatusActionChange: (value: BookStatusAction) => void;
  statusDate: string;
  onStatusDateChange: (value: string) => void;
  onSave: () => void;
  saving: boolean;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Set Status</DialogTitle>
          <DialogDescription>
            Update book state with automatic date-based inference.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="series-status-action">Action</Label>
            <select
              id="series-status-action"
              value={statusAction}
              onChange={(event) => onStatusActionChange(event.target.value as BookStatusAction)}
              className="h-9 w-full rounded border bg-white px-2 text-sm"
            >
              <option value="read">Mark as Read</option>
              <option value="unread">Mark as Unread</option>
              <option value="upcoming">Mark as Upcoming</option>
              <option value="available">Mark as Available</option>
            </select>
          </div>

          <div className="space-y-1">
            <Label htmlFor="series-status-date">
              {statusAction === "read" ? "Date Read" : "Publication Date (optional)"}
            </Label>
            <input
              id="series-status-date"
              value={statusDate}
              onChange={(event) => onStatusDateChange(event.target.value)}
              placeholder="YYYY-MM-DD"
              className="h-9 w-full rounded border bg-white px-2 text-sm"
            />
          </div>
        </div>

        <DialogFooter showCloseButton>
          <Button type="button" onClick={onSave} disabled={saving}>
            {saving ? "Saving..." : "Save status"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
