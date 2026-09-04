import { SparklesIcon } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * Single consolidated "New Books" indicator, replacing the old three-icon
 * system (new-available/new-upcoming/unread-remains). Per the UI
 * Adjustments spec: unread, available, or upcoming books are all just
 * "new books" from the user's perspective, so they collapse into one
 * pill here rather than three separate tags.
 *
 * Always rendered as the FIRST element in its row/card, immediately to
 * the left of the series title, so its horizontal position never shifts
 * based on how much of a (possibly truncated) title is visible.
 */
export function NewBooksPill({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1 rounded-full bg-emerald-500 px-2.5 py-1 text-xs font-bold leading-none text-white shadow-sm",
        className,
      )}
    >
      <SparklesIcon className="h-3.5 w-3.5" aria-hidden="true" />
      New Books
    </span>
  );
}
